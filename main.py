# -*- coding: utf-8 -*-
"""DASH 协议视频下载器 —— 单文件程序，主要用于Bilibili/YouTube视频下载。

日常使用：pythonw main.py（无控制台窗口）
开发调试：python main.py

下载引擎为 yt-dlp（§4.1 决策 D-1）：站点适配、签名破解、分片下载全部交给它，
本项目只负责界面、任务队列与风控策略。
"""

import atexit
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import winreg
from dataclasses import dataclass, field
from tkinter import filedialog

import customtkinter as ctk

# ---- 高 DPI 清晰（§6.3） ----
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ---- 引擎可用性探测（缺失时界面给出明确提示，不崩溃） ----
try:
    import yt_dlp
    from yt_dlp.utils import DownloadError
    try:
        from yt_dlp.utils import DownloadCancelled
    except ImportError:                     # 旧版 yt-dlp 无此异常，自建一个
        class DownloadCancelled(Exception):
            pass
    YTDLP_AVAILABLE = True
    YTDLP_VERSION = yt_dlp.version.__version__
except ImportError:
    yt_dlp = None
    YTDLP_AVAILABLE = False
    YTDLP_VERSION = ""

    class DownloadError(Exception):
        pass

    class DownloadCancelled(Exception):
        pass


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "启动日志.txt")

# cookie 临时副本的命名与"陈旧"判定阈值（见 cleanup_stale_cookie_copies）
COOKIE_TMP_PREFIX = "dash_cookies_"
COOKIE_TMP_SUFFIX = ".txt"
# 只清理超过这个时间没被动过的副本。阈值取得很宽（一天）是刻意的：
# 正在跑的实例可能挂了很久，宁可留着也不能误删它正在用的那份。
STALE_COOKIE_MAX_AGE = 24 * 3600.0


def log_error(context: str, exc: BaseException) -> None:
    """把启动/运行期异常写入日志文件。

    pythonw.exe 没有控制台，未捕获的异常只会让进程静默消失——用户看到的就是
    "窗口一闪而过"，完全无从排查。因此任何致命异常都必须落盘（§7.5 场景 14）。
    """
    import traceback
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {context}\n")
            f.write("".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)))
            f.write(f"Python: {sys.version.split()[0]}  "
                    f"cwd: {os.getcwd()}\n")
    except Exception:
        pass


# 判定 cookies.txt 是否真的含登录态。**必须是"核心"登录 cookie**：
# 实测踩坑 B-12/B-13：登出状态下 YouTube 仍会留置 __Secure-3PSID、
# __Secure-3PAPISID、__Secure-3PSIDCC 等一串字段，它们名字极像登录凭据
# （`__Secure-3PSID` 与真凭据 `__Secure-1PSID` 只差一个数字），但服务端不认。
# 若把它们也算作登录标志，校验会漏报，用户会带着无效文件反复尝试。
# 因此这里只认真正决定身份的那几个：SID 系、__Secure-1PSID、bilibili 的 SESSDATA。
LOGIN_COOKIE_NAMES = {
    "SID",              # Google/YouTube 核心会话 ID
    "SAPISID",          # Google 账号 API 会话
    "__Secure-1PSID",   # 第一方安全会话（注意：不是 -3PSID）
    "HSID", "SSID",     # 配套的会话字段
    "LOGIN_INFO",       # YouTube 专用登录票据
    "SESSDATA",         # bilibili 登录态
    "bili_jct",         # bilibili 表单校验
}


def cookie_file_has_login(path: str) -> bool:
    """检查 cookies.txt 里是否存在**有效的**登录凭据字段。

    读不出来则返回 True——不轻易下否定结论，把最终判断交给 yt-dlp。
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 6 and parts[5] in LOGIN_COOKIE_NAMES:
                    return True
    except OSError:
        return True
    return False


def _safe_remove(path: str) -> bool:
    """删除文件，失败不抛异常。返回是否真的删掉了。"""
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def cleanup_stale_cookie_copies(tmpdir: str = "",
                                max_age_seconds: float = STALE_COOKIE_MAX_AGE) -> int:
    """清理上次异常退出遗留的 cookie 临时副本，返回删掉的个数。

    正常退出时 atexit 会删掉本次会话的副本；但被任务管理器强杀、pythonw 崩溃
    或断电时 atexit 根本不执行，那份**完整的登录凭据**就留在 %TEMP% 里了。
    所以每次启动顺手扫一遍收掉。

    两条自我约束，避免误伤：
    · 只动本程序自己命名的文件（前缀 + 后缀都对得上）；
    · 只删超过 max_age_seconds 没被动过的——正在运行的实例刚建出来的副本
      必须留着，否则它的下载会中途失去登录态。
    """
    directory = tmpdir or tempfile.gettempdir()
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for fn in names:
        if not (fn.startswith(COOKIE_TMP_PREFIX) and fn.endswith(COOKIE_TMP_SUFFIX)):
            continue
        path = os.path.join(directory, fn)
        try:
            if now - os.path.getmtime(path) < max_age_seconds:
                continue
        except OSError:
            continue
        removed += _safe_remove(path)
    return removed


def _install_excepthook() -> None:
    """让 tkinter 回调里未捕获的异常也进日志，而不是只打印到不存在的控制台。

    注意签名（实测踩坑 B-11）：tkinter 以
    `report_callback_exception(self, exc, val, tb)` 调用，作为实例方法时
    会自动带上 self，因此这里必须定义 4 个参数——只写 3 个会导致
    记录异常的钩子自身抛 TypeError，把真正的错误盖掉。
    """

    def _hook(self, exc_type, exc_value, exc_traceback):
        log_error("界面回调异常", exc_value)
    try:
        import tkinter
        tkinter.Tk.report_callback_exception = _hook
    except Exception:
        pass


# 站点域名特征，用于代理分流（§2.2）与错误提示
BILI_HOSTS = ("bilibili.com", "b23.tv", "biligame.com")
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "googlevideo.com")

# 风控冷却上限（§7.9 A-6）：命中限流后全局暂停的秒数
RISK_COOLDOWN_SECONDS = 60


# ============================================================
# §2 数据模型
# ============================================================

class TaskStatus:
    """任务状态（§6.2 状态机）。"""

    PARSING = "解析中"
    READY = "待下载"
    QUEUED = "排队中"
    COOLDOWN = "风控冷却中"
    DOWNLOADING = "下载中"
    MERGING = "合并中"
    DONE = "已完成"
    FAILED = "失败"
    CANCELLED = "已取消"


@dataclass(eq=False)   # eq=False 保持对象身份哈希：任务对象需作为 dict 键（§实施要点）
class FormatOption:
    """一个可下载的清晰度选项（§7.1）。"""

    label: str                    # 下拉框显示文本，如 "1080P · H.264 · 66.6 MB"
    video_id: str = ""            # 视频轨格式 id；空表示无视频（仅音频）
    audio_id: str = ""            # 音频轨格式 id
    height: int = 0               # 分辨率高度，用于排序与默认选择
    vcodec: str = ""              # 原始 vcodec 字符串
    filesize: int = 0             # 预估字节数（视频轨 + 音频轨），0 表示未知
    is_h264: bool = False         # 决策 D-5：优先挑选
    has_video: bool = True        # False = 仅音频选项
    audio_abr: float = 0.0        # 默认音频轨码率(kbps)，用于按质量筛选

    @property
    def selector(self) -> str:
        """拼成 yt-dlp 的 format 字符串（§7.3：DASH 必须分轨拼接）。"""
        if not self.has_video:
            return self.audio_id or "bestaudio"
        if self.audio_id:
            return f"{self.video_id}+{self.audio_id}"
        return self.video_id or "best"

    def selector_with_audio(self, audio_choice: str) -> str:
        """按用户的音频质量选择拼格式串（§5 FR-3）。

        「最高」直接用最优音频轨；其余用 yt-dlp 的 bestaudio 过滤器按码率封顶。
        保留音频过滤器而非直接换格式 id，是为了让 yt-dlp 在首选轨不可用时
        仍能自动回退到次优轨，而不是整体失败。
        """
        if not self.has_video:
            return self.audio_id or "bestaudio"
        if audio_choice and audio_choice != "最高" and self.audio_abr > 0:
            return f"{self.video_id}+bestaudio[abr<={audio_choice.rstrip('k')}]"
        if self.audio_id:
            return f"{self.video_id}+{self.audio_id}"
        return self.video_id or "best"


@dataclass
class SubtitleOption:
    """一个可选字幕（§5 FR-3）。"""

    code: str        # yt-dlp 的语言代码，如 "zh-CN"
    label: str       # 显示文本
    is_auto: bool    # True = 自动生成（AI）字幕
    is_manual: bool  # True = 人工字幕


@dataclass(eq=False)
class ProbeResult:
    """一次解析的产物（§7.1 Engine.probe 的返回值）。"""

    title: str = ""                        # 视频标题（分P时为分P标题）
    playlist_title: str = ""               # 合集标题（分P时非空）
    duration: float = 0.0
    formats: list = field(default_factory=list)       # [FormatOption]
    subtitles: list = field(default_factory=list)     # [SubtitleOption]
    entries: list = field(default_factory=list)       # 分P列表；空表示单视频
    error: str = ""


@dataclass(eq=False)
class DownloadTask:
    """一个下载任务。"""

    url: str
    title: str                    # 原始标题（用于生成文件名）
    save_dir: str
    probed: ProbeResult           # 解析结果（格式、字幕、分P信息）

    status: str = TaskStatus.READY
    error: str = ""
    filename: str = ""            # 最终文件名（下载后确定）
    final_path: str = ""          # 最终文件完整路径

    # 用户选择
    format_index: int = 0         # 选中的清晰度在 probed.formats 中的下标
    subtitle_code: str = ""       # 空 = 不下字幕
    audio_label: str = "最高"     # 音频质量显示名

    # 进度
    percent: float = 0.0
    speed: float = 0.0            # B/s
    elapsed: float = 0.0
    total_size: float = 0.0       # 0 表示未知
    downloaded: float = 0.0
    phase: str = ""               # "" / "下载中" / "合并中"
    _last_bytes: float = 0.0      # 引擎层测速用
    _last_time: float = 0.0
    # DASH 分轨下载：视频轨、音频轨是两个独立钩子序列，进度必须跨轨累加，
    # 否则视频轨下完时进度会冲到 100%，音频轨开始时又跳回 0（§7.6 多轨聚合）
    _base_done: float = 0.0       # 已完成的轨累计字节
    _est_total: float = 0.0       # 两轨预估总字节（来自格式元数据）
    _last_emit: float = 0.0       # 进度推送限流用

    lock: threading.Lock = field(default_factory=threading.Lock)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def selected_format(self) -> FormatOption:
        try:
            return self.probed.formats[self.format_index]
        except IndexError:
            return FormatOption(label="best")


# ============================================================
# §4 工具层：纯函数
# ============================================================

RESERVED_NAMES = ({f"COM{i}" for i in range(1, 10)}
                  | {f"LPT{i}" for i in range(1, 10)}
                  | {"CON", "PRN", "AUX", "NUL"})
MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".flv", ".mov", ".avi", ".m4a", ".mp3"}
ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f%]')


def sanitize_stem(raw: str) -> str:
    """§5 FR-2：清洗文件名主干（不含扩展名）。纯函数。

    比 HLS 版多过滤 '%'：该字符在 yt-dlp 的 outtmpl 模板里是转义符。
    """
    name = (raw or "").strip()
    stem, ext = os.path.splitext(name)
    if ext.lower() in MEDIA_EXTS:
        name = stem
    name = ILLEGAL_RE.sub("", name)
    name = name.rstrip(". ").strip()
    if len(name) > 150:
        name = name[:150]
    # 保留名判定必须放在去掉首尾点/空格**之后**：Windows 把 "CON." 与 "CON"
    # 当成同一个设备名，先判后去尾点会让 "CON." 逃过加前缀（实测踩坑）。
    # 而且要比对**第一个点之前**的那一段——"NUL.txt" 在 Windows 上同样等价于
    # NUL 设备，只比整串会漏掉。
    if name.split(".", 1)[0].strip().upper() in RESERVED_NAMES:
        name = "_" + name
    if not name:
        name = time.strftime("Dash_%Y%m%d_%H%M%S")
    return name


def resolve_output_path(save_dir: str, stem: str, occupied: set) -> str:
    """§5 FR-2 规则 7：与磁盘已有文件或会话内任务重名时追加 ' (n)'。

    返回不含扩展名的完整主干路径；扩展名由 yt-dlp 按实际容器决定。
    """
    base = os.path.join(save_dir, stem)
    candidate, n = base, 1
    while _path_taken(candidate, occupied):
        candidate = f"{base} ({n})"
        n += 1
    return candidate


def _path_taken(stem_path: str, occupied: set) -> bool:
    """主干路径是否已被占用：检查磁盘上任意扩展名的同名文件，及会话内占用集合。"""
    if stem_path.casefold() in occupied:
        return True
    if os.path.exists(stem_path):
        return True
    for ext in (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".part"):
        if os.path.exists(stem_path + ext):
            return True
    return False


def human_speed(bps: float) -> str:
    if bps <= 0:
        return ""
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 ** 2:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / 1024 ** 2:.2f} MB/s"


def human_size(n: float) -> str:
    if n <= 0:
        return ""
    # 字节这一档不能省：没有它，不足 1KB 的值会被 .0f 抹成 "0 KB"，
    # 界面上就成"约 0 KB"，看着像出错了（与 human_speed 的三段式保持一致）。
    if n < 1024:
        return f"{n:.0f} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.0f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def fmt_elapsed(sec: float) -> str:
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60:02d}:{sec % 60:02d}"


def default_save_dir() -> str:
    d = os.path.expanduser("~/Downloads")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def host_of(url: str) -> str:
    """提取小写主机名（剥掉 user:pass@ 前缀与端口）。§2.2 的代理分流依赖它。

    三个实测坑：re 的 scheme 匹配默认大小写敏感，从别处复制来的 "HTTPS://"
    会取不到主机；主机名可能带端口（youtube.com:443），不剥掉的话后面的域名
    判定必然失败——表现为"明明是 YouTube 却走直连"。
    """
    m = re.match(r"https?://([^/]+)", (url or "").strip(), re.IGNORECASE)
    if not m:
        return ""
    host = m.group(1).lower().rsplit("@", 1)[-1]     # 去掉可能的 user:pass@
    return host.split(":", 1)[0]                     # 去掉可能的端口


def _host_in(host: str, domains) -> bool:
    """主机名是否属于给定域名：完全相等，或者是它的子域名。

    必须带 '.' 边界。裸 endswith("youtube.com") 会把 "notyoutube.com" 也算命中，
    而代理分流正是靠这个判定——误判意味着本该走代理的站点去走直连（超时），
    或者 bilibili 被推去走代理。
    """
    return any(host == d or host.endswith("." + d) for d in domains)


def is_bilibili(url: str) -> bool:
    return _host_in(host_of(url), BILI_HOSTS)


def is_youtube(url: str) -> bool:
    return _host_in(host_of(url), YOUTUBE_HOSTS)


def system_proxy() -> str:
    """§2.2：读取 Windows 系统代理，返回 'http://host:port' 或空串。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            enabled, _ = winreg.QueryValueEx(k, "ProxyEnable")
            if not enabled:
                return ""
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
        server = (server or "").strip()
        if not server:
            return ""
        if "=" in server:               # 形如 "http=1.2.3.4:8080;https=..."
            for part in server.split(";"):
                if part.lower().startswith(("http=", "https=")):
                    server = part.split("=", 1)[1]
                    break
        if not server.startswith(("http://", "socks5://")):
            server = "http://" + server
        return server
    except Exception:
        return ""


# JS 运行时：YouTube 的 n 签名挑战必须靠它解算，否则只能拿到故事板图片、
# 拿不到任何音视频格式（实测：不指定时 0 个视频格式，指定后 25 个）。
# yt-dlp 不会自动搜索 PATH，必须显式声明；这里按 yt-dlp 官方优先级探测。
JS_RUNTIME_CANDIDATES = (
    ("deno", "deno"),
    ("node", "node"),
    ("bun", "bun"),
    ("quickjs", "qjs"),
)


def detect_js_runtime() -> dict:
    """探测可用的 JS 运行时，返回 yt-dlp 的 js_runtimes 选项；找不到返回 {}。

    yt-dlp 自带的运行时支持：deno / node / bun / quickjs（globals.supported_js_runtimes）。
    """
    for name, exe in JS_RUNTIME_CANDIDATES:
        path = shutil.which(exe)
        if path:
            return {name: {"path": path}}
    return {}


JS_RUNTIME = detect_js_runtime()


# 主要语言白名单：YouTube 的自动字幕（AI）会把内容机翻成上百种语言，
# 其中绝大多数没有实际价值。只保留这些主要语言，避免下拉菜单被淹没、
# 也避免"全部语言"下载上百个文件。
MAJOR_LANGS = {
    "zh", "zh-hans", "zh-hant", "zh-cn", "zh-tw", "zh-hk", "ai-zh",
    "en", "ja", "ko", "ru", "fr", "de", "es", "pt", "it", "ar",
}


def _is_major_lang(code: str) -> bool:
    low = (code or "").lower()
    if low in MAJOR_LANGS:
        return True
    return low.split("-")[0] in MAJOR_LANGS


def codec_label(vcodec: str) -> str:
    """§7.3：把 yt-dlp 的 vcodec 字符串归一成人类可读的编码名。"""
    v = (vcodec or "").lower()
    if v.startswith("avc1") or v.startswith("h264"):
        return "H.264"
    if v.startswith("hev1") or v.startswith("hvc1") or "hevc" in v:
        return "HEVC"
    if v.startswith("av01"):
        return "AV1"
    if v.startswith("vp9") or v.startswith("vp09"):
        return "VP9"
    if not v or v == "none":
        return ""
    return vcodec.split(".")[0].upper()


# ============================================================
# §5 配置层
# ============================================================

DEFAULTS = {
    "save_dir": "",          # 空 → 启动时回退到 default_save_dir()
    "concurrency": 2,
    "topmost": False,
    "cookie_file": "",
    "use_cookies": False,
    "subtitle_lang": "zh",
    "use_proxy": True,
}


def load_config() -> dict:
    """§7.7：逐字段校验回退，文件损坏/缺失一律回到默认值，绝不因此崩溃。"""
    cfg = dict(DEFAULTS)
    cfg["save_dir"] = default_save_dir()
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return cfg
    if not isinstance(data, dict):
        return cfg
    if isinstance(data.get("save_dir"), str) and data["save_dir"].strip():
        cfg["save_dir"] = data["save_dir"]
    if isinstance(data.get("cookie_file"), str):
        cfg["cookie_file"] = data["cookie_file"]
    if data.get("subtitle_lang") in ("zh", "all", "none"):
        cfg["subtitle_lang"] = data["subtitle_lang"]
    try:
        cfg["concurrency"] = min(3, max(1, int(data.get("concurrency", 2))))
    except (TypeError, ValueError):
        pass
    for key in ("topmost", "use_cookies", "use_proxy"):
        if key in data:
            cfg[key] = bool(data[key])
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ============================================================
# §3 引擎层：yt-dlp 的薄封装（§7.1）
# ============================================================

# 风控冷却：所有 Worker 共用（§7.9 A-6）
_cooldown_until = 0.0
_cooldown_lock = threading.Lock()
_cooldown_reason = ""


def note_risk_cooldown(reason: str) -> None:
    """命中限流：全局冷却，所有任务一并等待（§7.9 A-6）。"""
    global _cooldown_until, _cooldown_reason
    with _cooldown_lock:
        _cooldown_until = time.time() + RISK_COOLDOWN_SECONDS
        _cooldown_reason = reason


def cooldown_remaining() -> float:
    with _cooldown_lock:
        return max(0.0, _cooldown_until - time.time())


RISK_PATTERNS = ("429", "412", "too many requests", "rate limit",
                 "risk control", "请求过于频繁", "风控")


def looks_like_risk(msg: str) -> bool:
    low = (msg or "").lower()
    return any(p in low for p in RISK_PATTERNS)


class Engine:
    """yt-dlp 的薄封装。所有方法都在工作线程中调用（§7.1）。"""

    def __init__(self, cookie_file: str = "", use_cookies: bool = False,
                 use_proxy: bool = True, subtitle_lang: str = "zh"):
        self.cookie_file = cookie_file      # 用户选定的原始文件（只读）
        self.use_cookies = use_cookies and bool(cookie_file)
        self.use_proxy = use_proxy
        self.subtitle_lang = subtitle_lang
        self._watch_stop = threading.Event()
        self._cookie_temp = None            # 本次会话用的临时副本
        if self.use_cookies:
            self._cookie_temp = self._make_cookie_copy(cookie_file)

    @staticmethod
    def _make_cookie_copy(src: str):
        """把用户的 cookies.txt 复制成临时文件，交给 yt-dlp 使用（决策 D-11）。

        **绝对不能让 yt-dlp 直接读写用户的原始文件。** yt-dlp 的
        `YoutubeDL.__exit__` → `close()` → `save_cookies()` 会在每次使用后
        把整个 cookie 罐写回 `cookiefile` 指定的路径。若原文件被会话刷新或
        登录态失效影响，用户的登录凭据就会被匿名 cookie 覆盖而**永久丢失**
        （实测踩坑 B-12：开发期间原始文件就是这样被写坏的）。

        复制到临时文件后，随机化写入都只发生在副本上，原文件全程只读。
        返回副本路径；复制失败返回 None（此时降级为不使用登录态）。
        """
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=COOKIE_TMP_PREFIX,
                                       suffix=COOKIE_TMP_SUFFIX)
            os.close(fd)
            shutil.copy2(src, tmp)
            atexit.register(lambda: _safe_remove(tmp))
            return tmp
        except OSError:
            # 复制失败必须把刚建出来的临时文件删掉：mkstemp 已经建了实体文件，
            # 而 atexit 是在复制成功之后才登记的——此处不删就永远没人删。
            # 后果不只是留个垃圾文件：复制中途失败时里面已经写进了**真实
            # cookie 的内容**，而 cookie 等同账号凭据，不能让它在 %TEMP% 里
            # 长期躺着——决策 D-11 要保护的正是这个文件。
            if tmp:
                _safe_remove(tmp)
            return None

    def _cancel_watchdog(self, ydl, cancel_event: threading.Event) -> None:
        """取消看门狗（§5 FR-8）：轮询取消标志，命中即打断 yt-dlp 的下载循环。

        用 yt-dlp 自身的 cancel_download() 而非杀进程：它会抛出 DownloadCancelled
        让下载循环干净退出，不留半截文件。
        """
        while not self._watch_stop.is_set():
            if cancel_event.is_set():
                try:
                    ydl.cancel_download()
                except Exception:
                    pass
                return
            self._watch_stop.wait(0.25)

    # ---- 公共配置 ----

    def _base_opts(self) -> dict:
        """两个阶段共用的基础选项。"""
        opts = {
            "quiet": False,        # §实施要点：quiet=True 会屏蔽所有 progress_hooks
            "no_warnings": True,
            "noprogress": True,    # 关掉自带文本进度条，我们有自己的
            "ignoreerrors": False,
            "nocheckcertificate": False,
            # §7.9 A-3/A-4：请求间隔与抖动，刻意放慢以规避风控
            "sleep_interval_requests": 1,
            "sleep_interval": 0.5,
            "max_sleep_interval": 1.5,
        }
        if JS_RUNTIME:
            # YouTube 的 n 签名挑战（缺了它 YouTube 只能拿到故事板图片）
            opts["js_runtimes"] = JS_RUNTIME
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            opts["ffmpeg_location"] = os.path.dirname(ffmpeg)
        # 决策 D-11：只把临时副本交给 yt-dlp，绝不碰用户的原始文件
        if self.use_cookies and self._cookie_temp:
            opts["cookiefile"] = self._cookie_temp
        return opts

    def _proxy_for(self, url: str) -> str:
        """§2.2 代理分流：bilibili 直连（实测更快），其他站点走系统代理。"""
        if not self.use_proxy or is_bilibili(url):
            return ""
        return system_proxy()

    # ---- 阶段一：解析（§7.2）----

    def probe(self, url: str, timeout: float = 30.0) -> ProbeResult:
        """解析链接，返回标题、格式列表、分P列表、字幕列表。不下载。"""
        opts = self._base_opts()
        opts.update({
            "skip_download": True,
            "extract_flat": False,
            "socket_timeout": timeout,
        })
        proxy = self._proxy_for(url)
        if proxy:
            opts["proxy"] = proxy

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except DownloadError as e:
            return ProbeResult(error=friendly_error(str(e), url))
        except Exception as e:
            return ProbeResult(error=f"解析异常：{type(e).__name__}: {e}")

        if not info:
            return ProbeResult(error="未能获取视频信息")

        entries = info.get("entries")
        if entries is not None:
            # 分P / 播放列表：逐个取标题（§5 FR-4）
            lst = [e for e in entries if e]
            if not lst:
                return ProbeResult(error="该合集内没有可下载的视频")
            return ProbeResult(
                title=info.get("title") or "合集",
                playlist_title=info.get("title") or "合集",
                entries=lst,
            )

        return ProbeResult(
            title=info.get("title") or "",
            duration=float(info.get("duration") or 0.0),
            formats=self._build_formats(info),
            subtitles=self._build_subtitles(info),
        )

    def probe_entry(self, entry: dict) -> ProbeResult:
        """分P条目：多数情况下 entry 已含完整格式信息，直接复用不必再请求。"""
        title = entry.get("title") or ""
        if entry.get("formats"):
            return ProbeResult(title=title,
                               duration=float(entry.get("duration") or 0.0),
                               formats=self._build_formats(entry),
                               subtitles=self._build_subtitles(entry))
        url = entry.get("webpage_url") or entry.get("url") or ""
        result = self.probe(url)
        if not result.error and title:
            result.title = result.title or title
        return result

    # ---- 格式筛选（§7.3 / 决策 D-5）----

    @staticmethod
    def _codec_rank(vcodec: str) -> int:
        """编码优先级：H.264(0) > HEVC(1) > AV1(2) > 其他(3)。越小越优先。"""
        c = codec_label(vcodec)
        return {"H.264": 0, "HEVC": 1, "AV1": 2}.get(c, 3)

    def _build_formats(self, info: dict) -> list:
        """把 yt-dlp 的 formats 列表压成「每个分辨率一条」的下拉选项。"""
        raw = info.get("formats") or []
        videos, audios = [], []
        for f in raw:
            fid = f.get("format_id")
            if not fid:
                continue
            # 会员/付费格式不可用，直接剔除
            note = (f.get("format_note") or "").lower()
            if any(k in note for k in ("premium", "会员", "paid")):
                continue
            vcodec = f.get("vcodec") or "none"
            acodec = f.get("acodec") or "none"
            has_v = vcodec != "none"
            has_a = acodec != "none"
            if has_v:
                videos.append(f)
            if has_a and not has_v:
                audios.append(f)

        if not videos:
            # 纯音频内容（或只有合轨格式）：给一条"仅音频"
            best_a = self._best_audio(audios)
            if best_a:
                size = best_a.get("filesize") or best_a.get("filesize_approx") or 0
                return [FormatOption(
                    label=f"仅音频 · {human_size(size)}".strip(" ·"),
                    audio_id=str(best_a["format_id"]), has_video=False,
                    filesize=int(size or 0),
                    audio_abr=float(best_a.get("abr") or best_a.get("tbr") or 0))]
            return []

        # 最优音频轨（所有清晰度共用，除非用户另选，见 §5 FR-3）
        best_audio = self._best_audio(audios)
        audio_id = str(best_audio["format_id"]) if best_audio else ""
        audio_size = 0
        audio_abr = 0.0
        if best_audio:
            audio_size = int(best_audio.get("filesize")
                             or best_audio.get("filesize_approx") or 0)
            audio_abr = float(best_audio.get("abr") or best_audio.get("tbr") or 0)

        # 按 height 分组，每组挑一条（决策 D-5：优先 H.264）
        groups = {}
        for f in videos:
            h = int(f.get("height") or 0)
            if h <= 0:
                continue
            groups.setdefault(h, []).append(f)

        options = []
        for h in sorted(groups, reverse=True):
            cands = groups[h]
            # 排序键：编码优先级 → 帧率 → 码率（取更优者）
            cands.sort(key=lambda f: (
                self._codec_rank(f.get("vcodec")),
                -(f.get("fps") or 0),
                -(f.get("tbr") or 0),
            ))
            best = cands[0]
            size = int(best.get("filesize") or best.get("filesize_approx") or 0)
            total = (size + audio_size) if size else 0
            fps = best.get("fps") or 0
            codec = codec_label(best.get("vcodec"))
            parts = [f"{h}P" if h < 2160 else "4K"]
            if fps and fps >= 50:
                parts.append(f"{int(fps)}fps")
            parts.append(codec)
            if total:
                parts.append(human_size(total))
            options.append(FormatOption(
                label=" · ".join(p for p in parts if p),
                video_id=str(best["format_id"]),
                audio_id=audio_id,
                height=h,
                vcodec=best.get("vcodec") or "",
                filesize=total,
                is_h264=(codec == "H.264"),
                audio_abr=audio_abr,
            ))

        # 默认选中：最高清晰度里的 H.264（决策 D-5）
        if options:
            h264 = [o for o in options if o.is_h264]
            chosen = h264[0] if h264 else options[0]
            options.remove(chosen)
            options.insert(0, chosen)
        return options

    @staticmethod
    def _best_audio(audios: list):
        if not audios:
            return None
        return max(audios, key=lambda f: (f.get("abr") or f.get("tbr") or 0))

    def _build_subtitles(self, info: dict) -> list:
        """收集人工字幕与 AI 字幕（§5 FR-3）。

        YouTube 的 automatic_captions 会把字幕机翻成上百种语言（实测某视频 157 条），
        且元数据里没有字段能区分"原创"与"翻译"。若全部列出，下拉菜单不可用，
        "全部语言"更会下载上百个文件。因此对自动字幕只保留主要语言；
        人工字幕是编辑逐条上传的，不存在这个问题，全部保留。
        """
        out = []
        manual = info.get("subtitles") or {}
        for code in sorted(manual.keys()):
            out.append(SubtitleOption(code=code, is_auto=False, is_manual=True,
                                      label=lang_label(code) + "（人工）"))

        auto = info.get("automatic_captions") or {}
        for code in sorted(auto.keys()):
            if _is_major_lang(code) and not any(s.code == code for s in out):
                out.append(SubtitleOption(code=code, is_auto=True, is_manual=False,
                                          label=lang_label(code) + "（AI）"))

        # 人工字幕排前，中文最前
        out.sort(key=lambda s: (0 if s.is_manual else 1,
                                0 if s.code.lower().startswith(("zh", "ai-zh")) else 1,
                                s.code))
        return out

    # ---- 阶段二：下载（§7.2 / §7.6）----

    def download(self, task: DownloadTask, on_progress, cancel_event) -> str:
        """下载 + 合并 + 下字幕，返回最终文件完整路径。在工作线程中调用。"""
        self._watch_stop.clear()             # 上一轮看门狗的停止标志复位
        opt = task.selected_format()
        stem_path = task.final_path          # 不含扩展名的主干路径

        opts = self._base_opts()
        opts.update({
            "format": opt.selector_with_audio(task.audio_label),
            "merge_output_format": "mp4/mkv",   # 决策 D-4：优先 mp4，不兼容则 mkv，绝不转码
            "outtmpl": stem_path + ".%(ext)s",
            "paths": {"home": task.save_dir},
            # §7.9 A-2：分片并发取 3（低于默认 5），降低风控特征
            "concurrent_fragment_downloads": 3,
            # §7.9 A-5：重试退避，上限 90 秒。宁可等，不可刷。
            "retries": 5,
            "fragment_retries": 5,
            "retry_sleep_functions": {
                "http": lambda n: min(90, 5 * (2 ** n)),
                "fragment": lambda n: min(90, 5 * (2 ** n)),
                "file_access": lambda n: min(90, 5 * (2 ** n)),
            },
            "progress_hooks": [on_progress],
            "postprocessor_hooks": [self._make_pp_hook(on_progress)],
        })
        proxy = self._proxy_for(task.url)
        if proxy:
            opts["proxy"] = proxy
        self._apply_subtitles(opts, task)

        def _hook(d):
            # 取消检查（§5 FR-8）：在每个分片之间生效
            if cancel_event.is_set():
                raise DownloadCancelled("用户取消")
            # 风控冷却：下载中命中限流也要让路（§7.9 A-6）
            if d.get("status") == "downloading" and cooldown_remaining() > 0:
                time.sleep(min(cooldown_remaining(), 5))

        hooks = opts["progress_hooks"]
        opts["progress_hooks"] = [_hook] + hooks

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # 取消看门狗：progress_hook 只在分片边界触发，若某分片传输很慢，
                # 用户点取消后要等很久才响应。看门狗直接调用 yt-dlp 的
                # cancel_download()（它会 raise DownloadCancelled 打断下载循环），
                # 使取消在亚秒级生效（§5 FR-8）。
                watchdog = threading.Thread(
                    target=self._cancel_watchdog, args=(ydl, cancel_event),
                    daemon=True, name="DashCancelWatch")
                watchdog.start()
                try:
                    ydl.download([task.url])
                finally:
                    self._watch_stop.set()
        except DownloadCancelled:
            raise
        except DownloadError as e:
            msg = str(e)
            if looks_like_risk(msg):
                note_risk_cooldown("下载时命中站点限流")
            raise

        final = self._find_output(stem_path)
        if final is None:
            raise DownloadError("下载已结束但未找到输出文件")
        return final

    @staticmethod
    def _make_pp_hook(on_progress):
        """后处理钩子：合并阶段开始/结束时通知界面（§7.6）。"""
        def hook(d):
            if d.get("status") == "started":
                on_progress({"status": "merging_start"})
            elif d.get("status") == "finished":
                on_progress({"status": "merging_done"})
        return hook

    def _apply_subtitles(self, opts: dict, task: DownloadTask) -> None:
        """按用户选择配置字幕下载（§5 FR-3：同步下 srt，不内嵌）。

        "全部语言" 展开为该视频实际存在的主要语言，而非字面意义的全部——
        后者在 YouTube 上会产出上百个机翻文件（见 _build_subtitles 说明）。
        """
        code = task.subtitle_code
        if not code:
            return
        if code == SUB_ALL_CODE:
            langs = [s.code for s in task.probed.subtitles if _is_major_lang(s.code)]
            langs = langs or [s.code for s in task.probed.subtitles][:1]
        else:
            langs = [code]
        if not langs:
            return
        opts.update({
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": langs,
            "subtitlesformat": "srt",
        })

    @staticmethod
    def _find_output(stem_path: str):
        """下载完成后按主干路径找实际产物（扩展名由容器决定）。

        按扩展名优先级取——不能依赖 os.listdir 的顺序，否则同时存在
        .mkv 与 .mp4 时可能误报（§5 FR-7 产物保护）。
        """
        for ext in (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".mov"):
            p = stem_path + ext
            if os.path.exists(p):
                return p
        # 兜底：同前缀的任意文件
        d, base = os.path.split(stem_path)
        prefix = base + "."
        try:
            for fn in os.listdir(d or "."):
                if fn.startswith(prefix) and not fn.endswith((".part", ".ytdl")):
                    return os.path.join(d, fn)
        except OSError:
            pass
        return None

    def cleanup(self, task: DownloadTask) -> None:
        """§5 FR-7：清理该任务的全部残留中间文件，绝不留半成品。"""
        stem_path = task.final_path
        d, base = os.path.split(stem_path)
        if not d:
            return
        try:
            names = os.listdir(d)
        except OSError:
            return
        # 匹配 主干.*  以及 yt-dlp 的分轨残留 主干.f137.mp4
        pattern = re.compile(r"^" + re.escape(base) + r"\..+$")
        for fn in names:
            if fn == os.path.basename(task.final_path):
                continue
            if not pattern.match(fn):
                continue
            # 已确认的最终产物不删（重试场景下可能已存在）
            if task.status == TaskStatus.DONE and fn == task.filename:
                continue
            if not fn.endswith((".part", ".ytdl", ".temp", ".frag")) \
                    and not re.search(r"\.f\d+\.", fn):
                continue
            try:
                os.remove(os.path.join(d, fn))
            except OSError:
                pass

    def cleanup_all_outputs(self, task: DownloadTask) -> None:
        """取消场景：删除该任务产生的一切文件（含已下载的分轨）。"""
        stem_path = task.final_path
        d, base = os.path.split(stem_path)
        if not d:
            return
        try:
            names = os.listdir(d)
        except OSError:
            return
        pattern = re.compile(r"^" + re.escape(base) + r"(\..+)?$")
        for fn in names:
            if not pattern.match(fn):
                continue
            if d == task.save_dir and fn in (".", ".."):
                continue
            try:
                os.remove(os.path.join(d, fn))
            except OSError:
                pass


def lang_label(code: str) -> str:
    """把字幕语言代码翻成中文名（未知的保留原代码）。"""
    base = code.lower()
    table = {
        "zh-hans": "中文（简体）", "zh-cn": "中文（简体）", "zh": "中文",
        "zh-hant": "中文（繁体）", "zh-tw": "中文（繁体）", "zh-hk": "中文（繁体·香港）",
        "ai-zh": "中文", "en": "英语", "en-us": "英语（美国）", "ja": "日语",
        "ko": "韩语", "ru": "俄语", "fr": "法语", "de": "德语", "es": "西班牙语",
    }
    if base in table:
        return table[base]
    for key, val in table.items():          # 形如 "zh-Hans-xxx"
        if base.startswith(key + "-"):
            return val
    return code


def friendly_error(msg: str, url: str = "") -> str:
    """§7.5：把 yt-dlp 的英文报错翻译成用户能看懂的中文原因。

    分支自上而下先匹配先返回，**顺序即优先级**。下面两处顺序是刻意排的，
    不能随意挪动：

    · **地区限制排在"不可用"之前**。站点对地区限制的原话就是
      "This video is not available in your region"，里面含 "not available"；
      若把地区判断放在后面，这句会被误翻成"视频不存在或已被删除"，
      用户会以为片子被删了，而实际上换个节点就能看。
    · **代理排在通用网络错误之前**。否则 "connection timed out to proxy"
      会被翻成"网络连接失败"，看不出问题其实出在代理上。
    """
    msg = msg or ""              # 统一归一，下面几处会对原始 msg 做 in 运算
    m = msg.lower()
    if "sign in to confirm" in m or "not a bot" in m:
        return "YouTube 要求登录验证。请在“登录态”中指定 cookies.txt（见帮助）"
    if "login" in m or "sign in" in m or "需要登录" in msg:
        return "该视频需要登录，请启用“登录态”"
    if "members only" in m or "premium" in m or "会员" in msg:
        return "该视频为会员专享，当前账号无权访问"
    if "private" in m:
        return "该视频为私享视频，无法访问"
    if "geo" in m or "region" in m or "地区" in msg:
        return "该视频在当前地区不可观看"
    if "unavailable" in m or "not available" in m:
        return "视频不存在或已被删除"
    if "proxy" in m:
        return "代理不可用，请检查代理设置"
    if "timed out" in m or "timeout" in m or "connection" in m or "network" in m:
        return "网络连接失败，请检查网络或代理设置"
    if "no space" in m or "disk" in m:
        return "磁盘空间不足"
    if "unsupported url" in m or "not a valid url" in m:
        return "链接无法识别，请检查"
    if "cookie" in m:
        return "登录态文件读取失败，请重新导出 cookies.txt"
    if "ffmpeg" in m:
        return "未找到 ffmpeg，无法合并音视频"
    first = (msg or "").strip().splitlines()
    first = first[-1] if first else "未知错误"
    return first[:200]


# ============================================================
# §6 工作线程（§4.4 线程模型）
# ============================================================

class WorkerPool:
    """固定数量的 Worker 线程，支持运行期动态增减（§7.9 A-1 会用到）。"""

    def __init__(self, task_queue, handler, size: int = 2):
        self.task_queue = task_queue
        self.handler = handler
        self._workers = []
        self._lock = threading.Lock()
        self.set_size(size)

    def set_size(self, n: int) -> None:
        n = max(1, min(3, int(n)))
        with self._lock:
            current = len(self._workers)
            if n > current:
                for _ in range(n - current):
                    t = threading.Thread(target=self._loop, daemon=True,
                                         name="DashWorker")
                    t._retire = threading.Event()
                    self._workers.append(t)
                    t.start()
            elif n < current:
                for t in self._workers[n:]:
                    t._retire.set()
                self._workers = self._workers[:n]

    def _loop(self) -> None:
        # 退休标志按线程各存一份，挂在**线程对象自己**身上（见 set_size 的
        # t._retire = ... 与 shutdown 的 t._retire.set()）。所以这里必须取
        # "我自己"：早先写的是 getattr(self, "_retire", ...)，而 self 是
        # WorkerPool 实例，永远取不到该属性，于是每次都回退成一个全新的、
        # 未置位的事件，退休检查恒为假——线程永不退出，缩容与 shutdown
        # 全部失效（实测 set_size(3→1) 后三个线程依旧在跑，等于"启用登录态
        # 时并发强制为 1"形同虚设，§7.9 A-1）。
        me = threading.current_thread()
        while True:
            try:
                task = self.task_queue.get(timeout=0.2)
            except queue.Empty:
                if getattr(me, "_retire", threading.Event()).is_set():
                    return
                continue
            if getattr(me, "_retire", threading.Event()).is_set():
                # 交还前先平账：这次 get() 对应的 task_done() 必须补上，
                # 否则 unfinished_tasks 只增不减，queue.join() 会永远等下去。
                self.task_queue.task_done()
                self.task_queue.put(task)       # 交还队列，由其他 Worker 接手
                return
            try:
                self.handler(task)
            except Exception:
                pass                            # 顶层兜底：绝不让 Worker 死掉
            finally:
                self.task_queue.task_done()

    def shutdown(self) -> None:
        with self._lock:
            for t in self._workers:
                t._retire.set()
            self._workers = []


def run_task(task: DownloadTask, engine: Engine, ui_queue) -> None:
    """Worker 单任务完整流程。只发事件，绝不直接操作控件（§4.4）。"""
    started = time.time()
    try:
        # 排队期间可能已被取消
        if task.cancel_event.is_set():
            _set_status(task, ui_queue, TaskStatus.CANCELLED)
            return

        # §7.9 A-6：全局风控冷却期内不发起任何请求
        remaining = cooldown_remaining()
        if remaining > 0:
            _set_status(task, ui_queue, TaskStatus.COOLDOWN)
            deadline = time.time() + remaining + 30
            while cooldown_remaining() > 0 and time.time() < deadline:
                if task.cancel_event.is_set():
                    _set_status(task, ui_queue, TaskStatus.CANCELLED)
                    return
                time.sleep(0.5)

        _set_status(task, ui_queue, TaskStatus.DOWNLOADING)
        with task.lock:
            task._last_bytes = 0.0
            task._last_time = time.time()
            task._base_done = 0.0            # 跨轨累加基数归零（§7.6 多轨聚合）
            task._est_total = 0.0
            task._last_emit = 0.0
            task.downloaded = 0.0
            task.percent = 0.0
            task.phase = "下载中"

        def on_progress(d: dict) -> None:
            _on_progress(task, d, ui_queue)

        final = engine.download(task, on_progress, task.cancel_event)

        with task.lock:
            task.final_path = final
            task.filename = os.path.basename(final)
            task.percent = 100.0
            task.speed = 0.0
            task.elapsed = time.time() - started
            task.phase = ""
        _set_status(task, ui_queue, TaskStatus.DONE)

    except DownloadCancelled:
        engine.cleanup_all_outputs(task)
        with task.lock:
            task.speed = 0.0
            task.phase = ""
        _set_status(task, ui_queue, TaskStatus.CANCELLED)

    except DownloadError as e:
        msg = str(e)
        if task.cancel_event.is_set():
            engine.cleanup_all_outputs(task)
            _set_status(task, ui_queue, TaskStatus.CANCELLED)
            return
        if looks_like_risk(msg):
            note_risk_cooldown("任务命中站点限流")
        engine.cleanup(task)
        with task.lock:
            task.error = friendly_error(msg, task.url)
            task.speed = 0.0
            task.phase = ""
        _set_status(task, ui_queue, TaskStatus.FAILED)

    except Exception as e:                       # 顶层兜底（§7.5 场景 14）
        try:
            engine.cleanup(task)
        except Exception:
            pass
        with task.lock:
            task.error = f"{type(e).__name__}: {str(e)[:180]}"
            task.speed = 0.0
            task.phase = ""
        _set_status(task, ui_queue, TaskStatus.FAILED)


def _set_status(task: DownloadTask, ui_queue, status: str) -> None:
    with task.lock:
        task.status = status
    ui_queue.put((task, "status"))


def _on_progress(task: DownloadTask, d: dict, ui_queue) -> None:
    """进度回调。yt-dlp 调用极频繁，必须限流（§7.1）。

    注意 DASH 分轨：视频轨、音频轨会各产生一串独立的 downloading/finished
    事件。进度必须跨轨累加（§7.6 多轨聚合），否则会来回横跳。
    """
    status = d.get("status")

    if status == "merging_start":
        with task.lock:
            task.phase = "合并中"
            task.speed = 0.0
            task.percent = 99.0
        _set_status(task, ui_queue, TaskStatus.MERGING)
        return
    if status == "merging_done":
        return

    if status == "finished":
        # 一轨下载完毕：把它的字节计入基数，下一轨从基数上继续
        with task.lock:
            got = float(d.get("total_bytes") or d.get("downloaded_bytes") or 0)
            task._base_done += got
            task.downloaded = task._base_done
            est = task.selected_format().filesize
            if est and task.percent < 99.0:
                task.percent = min(99.0, task._base_done * 100.0 / est)
        return

    if status != "downloading":
        return

    now = time.time()
    with task.lock:
        cur = float(d.get("downloaded_bytes") or 0)
        cur_total = float(d.get("total_bytes")
                          or d.get("total_bytes_estimate") or 0)
        overall = task._base_done + cur
        task.downloaded = overall

        # 总大小的估计优先级：格式元数据（两轨之和）→ 本次动态累加
        est = task.selected_format().filesize
        if est > 0:
            task._est_total = float(est)
            task.total_size = task._est_total
            task.percent = min(99.0, max(task.percent, overall * 100.0 / est))
        elif cur_total > 0:
            # 无预估：以当前轨的 total 为准，至少保证本轨进度可见
            task.total_size = task._base_done + cur_total
            task.percent = min(99.0, overall * 100.0 / task.total_size)

        # 测速：250ms 窗口内的字节增量（§7.6）
        dt = now - task._last_time
        if dt >= 0.25:
            delta = overall - task._last_bytes
            inst = delta / dt if dt > 0 else 0.0
            if inst > 0:
                task.speed = inst if task.speed <= 0 else task.speed * 0.6 + inst * 0.4
            elif task.speed > 0 and dt > 3:
                task.speed = 0.0
            task._last_bytes = overall
            task._last_time = now

    # 限流：每任务最多每 250ms 推一条（§7.1）
    if now - task._last_emit >= 0.25:
        task._last_emit = now
        ui_queue.put((task, "progress"))


# ============================================================
# §7 界面层（§6 界面设计）
# ============================================================

C_GRAY = "#8a8a8a"
C_DIM = "#6a6a6a"
C_BLUE = "#5aa7ff"
C_GREEN = "#3ecf6e"
C_RED = "#ff5f56"
C_AMBER = "#e8a33d"

STATUS_COLORS = {
    TaskStatus.PARSING: C_BLUE,
    TaskStatus.READY: C_GRAY,
    TaskStatus.QUEUED: C_GRAY,
    TaskStatus.COOLDOWN: C_AMBER,
    TaskStatus.DOWNLOADING: C_BLUE,
    TaskStatus.MERGING: C_BLUE,
    TaskStatus.DONE: C_GREEN,
    TaskStatus.FAILED: C_RED,
    TaskStatus.CANCELLED: C_GRAY,
}

STYLE_CANCEL = {"fg_color": "transparent", "border_width": 1,
                "border_color": "#7a3b3b", "text_color": C_RED,
                "hover_color": "#4a2b2b"}
STYLE_RETRY = {"fg_color": "transparent", "border_width": 1,
               "border_color": "#3b5d7a", "text_color": C_BLUE,
               "hover_color": "#2b3f4a"}
STYLE_OPEN = {"fg_color": "transparent", "border_width": 1,
              "border_color": "#3b7a4e", "text_color": C_GREEN,
              "hover_color": "#2b4a37"}
STYLE_START = {"fg_color": "#2b5c8a", "border_width": 0,
               "text_color": "#ffffff", "hover_color": "#356ea3"}

AUDIO_CHOICES = ["最高", "192k", "128k", "64k"]
SUB_NONE = "不下字幕"
SUB_ZH = "中文（人工 + AI）"
SUB_ALL = "全部语言"

# 「全部语言」存进任务里的值，引擎据此把它展开成「主要语言子集」（§9 风险 12）。
# **必须与显示文本 SUB_ALL 分开**：这两个值混用过一次——界面写 "all"、
# 引擎比 SUB_ALL，判断永远不成立，白名单整段失效，用户选「全部语言」
# 会真的下载上百个机翻字幕。改显示文本不该影响这个值，故单独定义。
SUB_ALL_CODE = "all"


def _font(size: int = 13, bold: bool = False):
    return ctk.CTkFont(family="Microsoft YaHei UI", size=size,
                       weight="bold" if bold else "normal")


class TaskCard(ctk.CTkFrame):
    """每任务一张卡片（§6.1 草图）。状态变化走 apply_status，连续值走 refresh。"""

    def __init__(self, master, task: DownloadTask, *, on_start, on_cancel,
                 on_retry, on_open):
        super().__init__(master, corner_radius=8)
        self.task = task
        # 用关键字参数而非字典：字典需要用 self.h["_start"] 取值，
        # 写成 self.h._start 会抛 AttributeError（实测踩坑 B-10，四个按钮全废）
        self._on_start = on_start
        self._on_cancel = on_cancel
        self._on_retry = on_retry
        self._on_open = on_open
        self._bar_indeterminate = False
        self._built_formats = False

        self.grid_columnconfigure(0, weight=1)

        # 第 0 行：标题 + 状态
        self.name_label = ctk.CTkLabel(self, text=task.title or task.url,
                                       anchor="w", font=_font(13, True),
                                       wraplength=420, justify="left")
        self.name_label.grid(row=0, column=0, sticky="w", padx=(14, 8),
                             pady=(12, 2))
        self.status_label = ctk.CTkLabel(self, text="", anchor="e",
                                         font=_font(12), wraplength=260)
        self.status_label.grid(row=0, column=1, sticky="e", padx=(8, 14),
                               pady=(12, 2))

        # 第 1 行：副信息（时长 / 编码提示）
        self.meta_label = ctk.CTkLabel(self, text="", anchor="w",
                                       font=_font(11), text_color=C_GRAY)
        self.meta_label.grid(row=1, column=0, columnspan=2, sticky="w",
                             padx=(14, 14), pady=(0, 6))

        # 第 2 行：选择区（清晰度 / 字幕 / 音频）+ 操作按钮
        self.select_row = ctk.CTkFrame(self, fg_color="transparent")
        self.select_row.grid(row=2, column=0, columnspan=2, sticky="ew",
                             padx=14, pady=(0, 8))
        self.select_row.grid_columnconfigure(3, weight=1)

        self.format_var = ctk.StringVar(value="")
        self.format_menu = ctk.CTkOptionMenu(
            self.select_row, values=["—"], variable=self.format_var, width=190,
            font=_font(12), command=self._on_format_change)
        self.format_menu.grid(row=0, column=0, sticky="w", padx=(0, 6))

        self.sub_var = ctk.StringVar(value=SUB_NONE)
        self.sub_menu = ctk.CTkOptionMenu(
            self.select_row, values=[SUB_NONE], variable=self.sub_var, width=150,
            font=_font(12), command=self._on_sub_change)
        self.sub_menu.grid(row=0, column=1, sticky="w", padx=(0, 6))

        self.audio_var = ctk.StringVar(value=AUDIO_CHOICES[0])
        self.audio_menu = ctk.CTkOptionMenu(
            self.select_row, values=AUDIO_CHOICES, variable=self.audio_var,
            width=90, font=_font(12), command=self._on_audio_change)
        self.audio_menu.grid(row=0, column=2, sticky="w")

        self.btn = ctk.CTkButton(self.select_row, text="开始下载", width=96,
                                 height=28, font=_font(12), command=self._primary)
        self.btn.grid(row=0, column=4, sticky="e", padx=(8, 0))

        # 第 3 行：进度条 + 数字
        self.prog_row = ctk.CTkFrame(self, fg_color="transparent")
        self.prog_row.grid(row=3, column=0, columnspan=2, sticky="ew",
                           padx=14, pady=(0, 12))
        self.prog_row.grid_columnconfigure(0, weight=1)
        self.bar = ctk.CTkProgressBar(self.prog_row, height=8, corner_radius=4)
        self.bar.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.bar.set(0)
        self.detail_label = ctk.CTkLabel(self.prog_row, text="", width=210,
                                         anchor="e", font=_font(11),
                                         text_color=C_GRAY)
        self.detail_label.grid(row=0, column=1, sticky="e")
        self.prog_row.grid_remove()             # 未开始下载时不显示

        self._fill_options()
        self.apply_status()

    # ---- 选项填充（§5 FR-3） ----

    def _fill_options(self) -> None:
        fmts = self.task.probed.formats
        if fmts:
            labels = [f.label for f in fmts]
            self.format_menu.configure(values=labels)
            self.format_var.set(labels[0])
            self.task.format_index = 0
        else:
            self.format_menu.configure(values=["默认"])
            self.format_var.set("默认")

        subs = self.task.probed.subtitles
        sub_labels = [SUB_NONE]
        zh = [s for s in subs if s.code.lower().startswith(("zh", "ai-zh"))]
        if zh:
            sub_labels.append(SUB_ZH)
        if subs:
            sub_labels.append(SUB_ALL)
            for s in subs[:12]:
                sub_labels.append(s.label)
        self.sub_menu.configure(values=sub_labels)
        self.sub_var.set(SUB_ZH if zh else SUB_NONE)
        self._on_sub_change(self.sub_var.get())
        self._update_meta()

    def _on_format_change(self, label: str) -> None:
        fmts = self.task.probed.formats
        for i, f in enumerate(fmts):
            if f.label == label:
                self.task.format_index = i
                break
        self._update_meta()

    def _on_sub_change(self, label: str) -> None:
        if label == SUB_NONE:
            self.task.subtitle_code = ""
        elif label == SUB_ZH:
            zh = [s for s in self.task.probed.subtitles
                  if s.code.lower().startswith(("zh", "ai-zh"))]
            self.task.subtitle_code = zh[0].code if zh else ""
        elif label == SUB_ALL:
            self.task.subtitle_code = SUB_ALL_CODE
        else:
            match = [s for s in self.task.probed.subtitles if s.label == label]
            self.task.subtitle_code = match[0].code if match else ""

    def _on_audio_change(self, label: str) -> None:
        self.task.audio_label = label

    def _update_meta(self) -> None:
        """副信息：时长 + 编码提示（决策 D-5 要求的知情提示）。"""
        opt = self.task.selected_format()
        bits = []
        dur = self.task.probed.duration
        if dur > 0:
            bits.append(fmt_elapsed(dur))
        if opt.filesize:
            bits.append(f"约 {human_size(opt.filesize)}")
        text = " · ".join(bits)
        hint = ""
        if opt.has_video and not opt.is_h264:
            codec = codec_label(opt.vcodec)
            if codec in ("HEVC", "AV1"):
                hint = f"⚠ {codec} 编码体积更小，但部分老设备可能无法播放"
        self.meta_label.configure(text=text)
        self.hint_label_configure(hint)

    def hint_label_configure(self, text: str) -> None:
        if not hasattr(self, "_hint"):
            self._hint = ctk.CTkLabel(self, text="", anchor="w", font=_font(11),
                                      text_color=C_AMBER)
            self._hint.grid(row=1, column=0, columnspan=2, sticky="e",
                            padx=(14, 14), pady=(0, 6))
        self._hint.configure(text=text)

    # ---- 按钮动作 ----

    def _primary(self) -> None:
        with self.task.lock:
            status = self.task.status
        if status == TaskStatus.DONE:
            self._on_open(self.task)
        elif status == TaskStatus.FAILED:
            self._on_retry(self.task)
        elif status in (TaskStatus.PARSING, TaskStatus.DOWNLOADING,
                        TaskStatus.MERGING, TaskStatus.QUEUED,
                        TaskStatus.COOLDOWN):
            self._on_cancel(self.task)
        else:
            self._on_start(self.task)

    # ---- 状态切换（离散事件） ----

    def apply_status(self) -> None:
        with self.task.lock:
            status = self.task.status
        self.status_label.configure(text_color=STATUS_COLORS.get(status, C_GRAY))

        running = status in (TaskStatus.DOWNLOADING, TaskStatus.MERGING,
                             TaskStatus.QUEUED, TaskStatus.COOLDOWN)
        # 选择区：仅在未开始时可编辑
        state = "disabled" if running else "normal"
        for w in (self.format_menu, self.sub_menu, self.audio_menu):
            try:
                w.configure(state=state)
            except Exception:
                pass

        if status == TaskStatus.PARSING:
            self.btn.configure(text="解析中", state="disabled", **STYLE_RETRY)
            self._set_bar_indeterminate(True)
        elif status in (TaskStatus.READY, TaskStatus.CANCELLED):
            self.btn.configure(text="开始下载", state="normal", **STYLE_START)
            self._set_bar_indeterminate(False)
            self.bar.set(0)
            self.prog_row.grid_remove()
        elif status in (TaskStatus.QUEUED, TaskStatus.COOLDOWN):
            self.btn.configure(text="取消", state="normal", **STYLE_CANCEL)
            self._set_bar_indeterminate(False)
            self.bar.set(0)
            self.prog_row.grid()
        elif status == TaskStatus.DOWNLOADING:
            self.btn.configure(text="取消", state="normal", **STYLE_CANCEL)
            self._set_bar_indeterminate(False)
            self.prog_row.grid()
        elif status == TaskStatus.MERGING:
            self.btn.configure(text="取消", state="normal", **STYLE_CANCEL)
            self._set_bar_indeterminate(True)
            self.prog_row.grid()
        elif status == TaskStatus.DONE:
            self.btn.configure(text="打开目录", state="normal", **STYLE_OPEN)
            self._set_bar_indeterminate(False)
            self.bar.set(1.0)
            self.prog_row.grid()
        else:   # FAILED
            self.btn.configure(text="重试", state="normal", **STYLE_RETRY)
            self._set_bar_indeterminate(False)
            self.prog_row.grid()

    def _set_bar_indeterminate(self, on: bool) -> None:
        if on and not self._bar_indeterminate:
            self.bar.configure(mode="indeterminate")
            self.bar.start()
            self._bar_indeterminate = True
        elif not on and self._bar_indeterminate:
            self.bar.stop()
            self.bar.configure(mode="determinate")
            self._bar_indeterminate = False

    # ---- 连续值刷新（每 100ms） ----

    def refresh(self) -> None:
        with self.task.lock:
            status = self.task.status
            percent = self.task.percent
            speed = self.task.speed
            error = self.task.error
            elapsed = self.task.elapsed
            phase = self.task.phase
        self.status_label.configure(text_color=STATUS_COLORS.get(status, C_GRAY))

        if status == TaskStatus.PARSING:
            self.status_label.configure(text="解析中…")
            self.detail_label.configure(text="")
        elif status == TaskStatus.READY:
            self.status_label.configure(text="待下载")
            self.detail_label.configure(text="")
        elif status == TaskStatus.QUEUED:
            self.status_label.configure(text="排队中")
            self.detail_label.configure(text="")
        elif status == TaskStatus.COOLDOWN:
            left = cooldown_remaining()
            self.status_label.configure(
                text=f"风控冷却中 · 还需 {int(left) + 1} 秒")
            self.detail_label.configure(text="站点限流，为保护账号暂停请求")
        elif status == TaskStatus.DOWNLOADING:
            self.status_label.configure(text=f"下载中 · {percent:.0f}%")
            self.detail_label.configure(
                text=f"{human_speed(speed)}　{human_size(self.task.downloaded)}"
                     f"　{fmt_elapsed(elapsed)}")
        elif status == TaskStatus.MERGING:
            self.status_label.configure(text="合并音视频中…")
            self.detail_label.configure(text="ffmpeg 正在封装，请稍候")
        elif status == TaskStatus.DONE:
            self.status_label.configure(
                text=f"已完成 ✓ · 用时 {fmt_elapsed(elapsed)}")
            self.detail_label.configure(text=self.task.filename)
        elif status == TaskStatus.FAILED:
            self.status_label.configure(text=f"失败：{error}")
            self.detail_label.configure(text="")
        else:
            self.status_label.configure(text="已取消")
            self.detail_label.configure(text="")

        if (status == TaskStatus.DOWNLOADING and not self._bar_indeterminate
                and self.task.total_size > 0):
            self.bar.set(min(1.0, percent / 100.0))
        if phase == "合并中" and status != TaskStatus.MERGING:
            self.status_label.configure(text="合并中…")


class PartSelectDialog(ctk.CTkToplevel):
    """分P / 播放列表多选对话框（§5 FR-4，全程序唯一的模态弹窗）。"""

    def __init__(self, master, playlist_title: str, entries: list,
                 save_dir: str, default_index: int = -1):
        super().__init__(master)
        self.title("选择要下载的视频")
        self.geometry("620x520")
        self.minsize(520, 420)
        self.entries = entries
        self.result = None
        self.vars = []

        self.transient(master)
        self.grab_set()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(self, text=playlist_title, anchor="w",
                     font=_font(14, True), wraplength=580,
                     justify="left").grid(row=0, column=0, sticky="ew",
                                          padx=16, pady=(14, 2))
        ctk.CTkLabel(self, text=f"共 {len(entries)} 个 · 保存到 {save_dir}",
                     anchor="w", font=_font(11), text_color=C_GRAY).grid(
            row=1, column=0, sticky="ew", padx=16, pady=(0, 8))

        self.scroll = ctk.CTkScrollableFrame(self, label_text="")
        self.scroll.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 8))
        self.scroll.grid_columnconfigure(0, weight=1)

        for i, e in enumerate(entries):
            title = e.get("title") or f"P{i + 1}"
            dur = float(e.get("duration") or 0)
            label = f"{i + 1:02d}  {title}"
            if dur > 0:
                label += f"   ({fmt_elapsed(dur)})"
            var = ctk.BooleanVar(value=(i == default_index))
            # 注意：CTkCheckBox 不支持 anchor 参数（实测会抛 ValueError），
            # 文字在控件内居中，靠 sticky="w" 整体左对齐即可。
            cb = ctk.CTkCheckBox(self.scroll, text=label, variable=var,
                                 font=_font(12), checkbox_width=18,
                                 checkbox_height=18)
            cb.grid(row=i, column=0, sticky="w", padx=6, pady=3)
            self.vars.append(var)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 14))
        btns.grid_columnconfigure(3, weight=1)
        ctk.CTkButton(btns, text="全选", width=70, font=_font(12),
                      command=lambda: self._set_all(True)).grid(row=0, column=0,
                                                                padx=(0, 6))
        ctk.CTkButton(btns, text="全不选", width=78, font=_font(12),
                      command=lambda: self._set_all(False)).grid(row=0, column=1,
                                                                 padx=(0, 6))
        ctk.CTkButton(btns, text="反选", width=70, font=_font(12),
                      command=self._invert).grid(row=0, column=2)
        ctk.CTkButton(btns, text="取消", width=80, font=_font(12),
                      **STYLE_RETRY, command=self._cancel).grid(
            row=0, column=4, padx=(6, 6))
        ctk.CTkButton(btns, text="加入队列", width=100, font=_font(12, True),
                      command=self._ok).grid(row=0, column=5)

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.after(100, self._center)

    def _center(self) -> None:
        try:
            self.update_idletasks()
            px = self.master.winfo_rootx() + (self.master.winfo_width() - 620) // 2
            py = self.master.winfo_rooty() + 60
            self.geometry(f"+{max(0, px)}+{max(0, py)}")
        except Exception:
            pass
        try:
            self.focus_force()
        except Exception:
            pass

    def _set_all(self, value: bool) -> None:
        for v in self.vars:
            v.set(value)

    def _invert(self) -> None:
        for v in self.vars:
            v.set(not v.get())

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _ok(self) -> None:
        self.result = [i for i, v in enumerate(self.vars) if v.get()]
        self.destroy()


class App(ctk.CTk):
    def __init__(self, cfg: dict, status_text: str, enabled: bool):
        super().__init__()
        self.title("DASH 视频下载器")
        self.geometry("860x640")
        self.minsize(700, 520)

        self.cfg = cfg
        self.enabled = enabled
        self.status_text = status_text
        self.tasks = []
        self.cards = {}
        self.occupied = set()
        self.ui_queue = queue.Queue()
        self.task_queue = queue.Queue()
        self._pending = {}                  # 解析占位卡片：task -> card
        self._msg_after_id = None

        self.engine = self._make_engine()
        self.pool = WorkerPool(self.task_queue,
                               lambda t: run_task(t, self.engine, self.ui_queue),
                               cfg["concurrency"])

        self._build_ui()
        self.attributes("-topmost", bool(cfg["topmost"]))
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._tick)

    def _make_engine(self) -> Engine:
        return Engine(cookie_file=self.cfg.get("cookie_file", ""),
                      use_cookies=self.cfg.get("use_cookies", False),
                      use_proxy=self.cfg.get("use_proxy", True),
                      subtitle_lang=self.cfg.get("subtitle_lang", "zh"))

    # ---- 界面搭建（§6.1） ----

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # 顶栏
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        top.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(top, text="DASH 视频下载器", font=_font(18, True)).grid(
            row=0, column=0, sticky="w")
        self.topmost_var = ctk.BooleanVar(value=bool(self.cfg["topmost"]))
        ctk.CTkSwitch(top, text="📌 置顶", variable=self.topmost_var,
                      font=_font(13), command=self._toggle_topmost).grid(
            row=0, column=2, sticky="e")

        # 输入区
        form = ctk.CTkFrame(self)
        form.grid(row=1, column=0, sticky="ew", padx=16, pady=6)
        form.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(form, text="视频链接", width=80, anchor="w",
                     font=_font()).grid(row=0, column=0, sticky="w",
                                        padx=(14, 4), pady=(14, 6))
        self.url_entry = ctk.CTkEntry(
            form, placeholder_text="粘贴 bilibili 或 YouTube 链接后按回车",
            font=_font())
        self.url_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(14, 6))
        self.parse_btn = ctk.CTkButton(form, text="解析", width=90, height=30,
                                       font=_font(13, True), command=self._parse)
        self.parse_btn.grid(row=0, column=2, sticky="e", padx=(0, 14), pady=(14, 6))

        ctk.CTkLabel(form, text="保存目录", width=80, anchor="w",
                     font=_font()).grid(row=1, column=0, sticky="w",
                                        padx=(14, 4), pady=6)
        self.dir_entry = ctk.CTkEntry(form, font=_font())
        self.dir_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=6)
        self.dir_entry.insert(0, self.cfg["save_dir"])
        ctk.CTkButton(form, text="浏览…", width=90, font=_font(),
                      command=self._browse).grid(row=1, column=2, sticky="e",
                                                 padx=(0, 14), pady=6)

        # 设置行：并发 / 登录态 / 代理 / 置顶
        row2 = ctk.CTkFrame(form, fg_color="transparent")
        row2.grid(row=2, column=0, columnspan=3, sticky="ew", padx=(14, 14),
                  pady=(0, 14))
        row2.grid_columnconfigure(5, weight=1)

        ctk.CTkLabel(row2, text="并发数", anchor="w", font=_font(12)).grid(
            row=0, column=0, sticky="w", padx=(0, 4))
        self.concurrency_var = ctk.StringVar(value=str(self.cfg["concurrency"]))
        self.concurrency_menu = ctk.CTkOptionMenu(
            row2, values=["1", "2", "3"], variable=self.concurrency_var,
            width=64, font=_font(12), command=self._set_concurrency)
        self.concurrency_menu.grid(row=0, column=1, sticky="w", padx=(0, 16))

        self.cookie_var = ctk.BooleanVar(value=bool(self.cfg["use_cookies"]))
        self.cookie_switch = ctk.CTkSwitch(
            row2, text="登录态", variable=self.cookie_var, font=_font(12),
            command=self._toggle_cookies)
        self.cookie_switch.grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.cookie_btn = ctk.CTkButton(
            row2, text=self._cookie_btn_text(), width=130, font=_font(11),
            fg_color="transparent", border_width=1, command=self._choose_cookie)
        self.cookie_btn.grid(row=0, column=3, sticky="w", padx=(0, 8))

        ctk.CTkButton(row2, text="?", width=26, font=_font(11),
                      fg_color="transparent", border_width=1,
                      command=self._show_cookie_help).grid(row=0, column=4,
                                                           sticky="w")

        self.proxy_var = ctk.BooleanVar(value=bool(self.cfg["use_proxy"]))
        ctk.CTkSwitch(row2, text="代理", variable=self.proxy_var,
                      font=_font(12), command=self._toggle_proxy).grid(
            row=0, column=6, sticky="e")

        # 任务区
        self.task_frame = ctk.CTkScrollableFrame(
            self, label_text="任务列表", label_font=_font(12))
        self.task_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=6)
        self.task_frame.grid_columnconfigure(0, weight=1)

        # 状态栏
        bar = ctk.CTkFrame(self)
        bar.grid(row=3, column=0, sticky="ew", padx=16, pady=(6, 12))
        bar.grid_columnconfigure(1, weight=1)
        # 第 0 行：环境自检（ffmpeg / yt-dlp / JS 运行时 / 代理）+ 任务计数
        self.status_env_label = ctk.CTkLabel(bar, text="", font=_font(12),
                                             anchor="w")
        self.status_env_label.grid(row=0, column=0, columnspan=2, sticky="w",
                                   padx=(14, 8), pady=(8, 2))
        self.counts_label = ctk.CTkLabel(bar, text="", font=_font(12),
                                         text_color=C_GRAY)
        self.counts_label.grid(row=0, column=2, sticky="e", padx=(8, 14),
                               pady=(8, 2))
        # 第 1 行：临时消息（单独一行，否则被长环境串挤没）
        self.msg_label = ctk.CTkLabel(bar, text="", anchor="w", font=_font(12),
                                      wraplength=800, justify="left")
        self.msg_label.grid(row=1, column=0, columnspan=3, sticky="ew",
                            padx=14, pady=(0, 8))

        self._refresh_env_label()
        if not self.enabled:
            self.parse_btn.configure(state="disabled")

        self.url_entry.bind("<Return>", lambda e: self._parse())

    def _refresh_env_label(self) -> None:
        proxy = system_proxy()
        bits = [self.status_text]
        if proxy:
            bits.append(f"代理 {proxy} · {'启用' if self.proxy_var.get() else '已关闭'}")
        else:
            bits.append("未检测到系统代理")
        if not YTDLP_AVAILABLE:
            bits.append("✗ yt-dlp 未安装")
        if not JS_RUNTIME:
            # YouTube 的 n 签名挑战需要 JS 运行时，缺了它 YouTube 完全不可用
            bits.append("⚠ 未检测到 JS 运行时，YouTube 将不可用")
        # 登录态校验：放进环境栏（常驻），避免被临时消息覆盖或 6 秒后消失
        if self.cookie_var.get():
            bits.append(self._cookie_health())
        self.status_env_label.configure(
            text="　".join(bits),
            text_color=(C_GREEN if self.enabled else C_RED))

    def _cookie_health(self) -> str:
        """检查已配置的登录态文件，返回一句可直接显示的状态描述。"""
        path = self.cfg.get("cookie_file") or ""
        if not path:
            return "⚠ 登录态已开启但未选择文件"
        if not os.path.exists(path):
            return "⚠ 登录态文件已不存在，请重新选择"
        if not cookie_file_has_login(path):
            return "⚠ 登录态文件不含登录凭据，YouTube 会失败（请重新导出）"
        return f"登录态 {os.path.basename(path)}"

    def _cookie_btn_text(self) -> str:
        path = self.cfg.get("cookie_file", "")
        if not path:
            return "选择 cookies.txt"
        return os.path.basename(path)

    # ---- 解析（阶段一） ----

    def _parse(self) -> None:
        if not self.enabled:
            return
        url = self.url_entry.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            self._set_msg("链接必须以 http:// 或 https:// 开头", C_RED)
            return
        save_dir = self.dir_entry.get().strip() or default_save_dir()
        save_dir = os.path.expanduser(save_dir)
        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError as e:
            self._set_msg(f"无法创建保存目录：{e}", C_RED)
            return

        self.parse_btn.configure(state="disabled", text="解析中")
        self._set_msg("正在解析，请稍候…", C_GRAY)
        engine = self._make_engine()
        threading.Thread(target=self._probe_worker, args=(url, save_dir, engine),
                         daemon=True, name="DashProbe").start()

    def _probe_worker(self, url: str, save_dir: str, engine: Engine) -> None:
        try:
            result = engine.probe(url)
        except Exception as e:
            result = ProbeResult(error=f"{type(e).__name__}: {str(e)[:180]}")
        self.ui_queue.put(("probe_done", url, save_dir, result))

    def _handle_probe_done(self, url: str, save_dir: str, result: ProbeResult) -> None:
        self.parse_btn.configure(state="normal", text="解析")
        if result.error:
            self._set_msg(f"解析失败：{result.error}", C_RED)
            return

        if result.entries:
            # 分P / 播放列表 → 弹多选框（§5 FR-4）
            default_index = self._p_index_from_url(url)
            dlg = PartSelectDialog(self, result.title, result.entries, save_dir,
                                   default_index)
            self.wait_window(dlg)
            picked = dlg.result
            if not picked:
                self._set_msg("已取消选择", C_GRAY)
                return
            self._create_from_parts(result, picked, save_dir, url)
            return

        self._create_task(url, result.title, save_dir, result, batch_dir=None)
        self._set_msg("解析完成，可在卡片中选择清晰度后开始下载", C_GREEN)
        self.url_entry.delete(0, "end")

    @staticmethod
    def _p_index_from_url(url: str) -> int:
        """§5 FR-4：链接带 ?p=N 时默认只勾选第 N 个。"""
        m = re.search(r"[?&]p=(\d+)", url)
        if m:
            return max(0, int(m.group(1)) - 1)
        return -1

    def _create_from_parts(self, result: ProbeResult, picked: list,
                           save_dir: str, url: str) -> None:
        """批量创建任务（决策 D-8：≥2 个时建子目录）。"""
        entries = result.entries
        sub_dir = save_dir
        if len(picked) >= 2:
            sub_dir = os.path.join(save_dir, sanitize_stem(result.title))
            try:
                os.makedirs(sub_dir, exist_ok=True)
            except OSError:
                sub_dir = save_dir

        engine = self._make_engine()
        done = 0
        for i in picked:
            e = entries[i]
            title = e.get("title") or f"{result.title} P{i + 1}"
            # 分P 的格式信息多数已在 entries 里，缺失的再单独解析
            pr = engine.probe_entry(e)
            if pr.error:
                self._set_msg(f"第 {i + 1} 个解析失败：{pr.error}", C_RED)
                continue
            self._create_task(e.get("webpage_url") or url, title, sub_dir,
                              pr, batch_dir=sub_dir)
            done += 1
        if done:
            self._set_msg(f"已添加 {done} 个任务到队列（保存于 {sub_dir}）", C_GREEN)
            self.url_entry.delete(0, "end")

    def _create_task(self, url: str, title: str, save_dir: str,
                     result: ProbeResult, batch_dir) -> None:
        """建任务 + 建卡片。列表倒序：新卡片插到最上方（§5 FR-5）。"""
        stem = sanitize_stem(title)
        stem_path = resolve_output_path(save_dir, stem, self.occupied)
        self.occupied.add(stem_path.casefold())

        task = DownloadTask(url=url, title=title, save_dir=save_dir,
                            probed=result, final_path=stem_path)
        self.tasks.append(task)

        card = TaskCard(self.task_frame, task,
                        on_start=self._start,
                        on_cancel=self._cancel,
                        on_retry=self._retry,
                        on_open=self._open_dir)
        self.cards[task] = card
        self._reindex_cards()               # 统一重排，保证倒序（§5 FR-5）
        return task

    def _reindex_cards(self) -> None:
        """按添加顺序倒序重排：新任务在最上方（§5 FR-5）。

        必须整体重排而非只处理新卡片——否则先创建的卡片会在后续插入时
        被重复下移，顺序错乱（实测踩过）。
        """
        ordered = [self.cards[t] for t in reversed(self.tasks) if t in self.cards]
        for idx, c in enumerate(ordered):
            c.grid(row=idx, column=0, sticky="ew", padx=4, pady=4)

    # ---- 任务操作 ----

    def _start(self, task: DownloadTask) -> None:
        with task.lock:
            if task.status not in (TaskStatus.READY, TaskStatus.CANCELLED):
                return
            task.status = TaskStatus.QUEUED
            task.error = ""
            task.percent = 0.0
            task.speed = 0.0
            task.downloaded = 0.0
            task.total_size = 0.0
            task.elapsed = 0.0
            task.cancel_event = threading.Event()
        self.ui_queue.put((task, "status"))
        self.task_queue.put(task)

    def _cancel(self, task: DownloadTask) -> None:
        with task.lock:
            status = task.status
        if status in (TaskStatus.QUEUED, TaskStatus.COOLDOWN):
            with task.lock:
                task.status = TaskStatus.CANCELLED
            task.cancel_event.set()
            self.ui_queue.put((task, "status"))
            return
        if status in (TaskStatus.DOWNLOADING, TaskStatus.MERGING):
            task.cancel_event.set()

    def _retry(self, task: DownloadTask) -> None:
        """§5 FR-8：重试保留原有清晰度与字幕选择，无需重新解析。"""
        self._start(task)

    def _open_dir(self, task: DownloadTask) -> None:
        target = task.final_path or task.save_dir
        try:
            if os.path.exists(target):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
            else:
                os.startfile(task.save_dir)
        except OSError:
            try:
                os.startfile(task.save_dir)
            except OSError as e:
                self._set_msg(f"无法打开目录：{e}", C_RED)

    # ---- 设置项 ----

    def _toggle_topmost(self) -> None:
        self.attributes("-topmost", self.topmost_var.get())
        self._save_cfg()

    def _set_concurrency(self, value: str) -> None:
        if self.cookie_var.get():
            self._set_msg("启用登录态时并发固定为 1，以降低账号风控风险", C_AMBER)
            self.concurrency_var.set("1")
            self.pool.set_size(1)
            self._save_cfg()
            return
        try:
            n = int(value)
        except ValueError:
            return
        self.pool.set_size(n)
        self._save_cfg()

    def _toggle_cookies(self) -> None:
        """§7.9 A-1：启用登录态时并发强制为 1。"""
        enabled = self.cookie_var.get()
        if enabled and not self.cfg.get("cookie_file"):
            self._choose_cookie()
            if not self.cfg.get("cookie_file"):
                self.cookie_var.set(False)
                return
        if enabled:
            self.concurrency_var.set("1")
            self.concurrency_menu.configure(state="disabled")
            self.pool.set_size(1)
            self._set_msg("已启用登录态：并发固定为 1、请求间隔 1 秒（账号安全优先）",
                          C_GREEN)
        else:
            self.concurrency_menu.configure(state="normal")
            self.concurrency_var.set(str(self.cfg.get("concurrency", 2)))
            self.pool.set_size(self.cfg.get("concurrency", 2))
            self._set_msg("已关闭登录态，改为免登录模式", C_GRAY)
        self.engine = self._make_engine()
        self._save_cfg()
        self._refresh_env_label()       # 开关状态变了，环境栏的登录态段需同步

    def _choose_cookie(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择 cookies.txt",
            filetypes=[("Cookie 文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        self.cfg["cookie_file"] = path
        self.cookie_btn.configure(text=os.path.basename(path))
        self.engine = self._make_engine()
        self._save_cfg()
        self._refresh_env_label()       # 必须刷新：否则旧的"不含登录凭据"提示
        # 会一直挂在环境栏上，直到重启软件
        # 当场校验：文件里没有登录字段就直接警告，避免带着无效文件去解析
        if not cookie_file_has_login(path):
            self._set_msg(
                f"⚠ 该文件不含登录凭据（只有匿名 cookie），YouTube 会解析失败。"
                f"请确认在【已登录】的 YouTube 页面导出", C_AMBER)
        else:
            self._set_msg(f"已选择登录态文件：{os.path.basename(path)}", C_GREEN)

    def _toggle_proxy(self) -> None:
        self.engine = self._make_engine()
        self._refresh_env_label()
        self._save_cfg()

    def _show_cookie_help(self) -> None:
        """导出 cookies.txt 的步骤（§5 FR-9b）。"""
        top = ctk.CTkToplevel(self)
        top.title("如何提供登录态")
        top.geometry("560x360")
        top.transient(self)
        top.grab_set()
        text = (
            "为什么需要它？\n"
            "  · YouTube 会拦截未登录的请求（机器人验证），没有登录态完全无法下载。\n"
            "  · bilibili 免登录可下 1080P；登录后可用更高清晰度。\n\n"
            "为什么不能直接读浏览器的 cookie？\n"
            "  · 本机 Chrome 152 启用了 App-Bound 加密，实测 575 条 cookie 全部\n"
            "    无法被外部程序解密（这是 Chrome 的刻意设计）。\n"
            "  · 本软件不绕过该安全机制。\n\n"
            "如何导出：\n"
            "  1. Chrome 安装扩展 “Get cookies.txt LOCALLY”（开源、纯本地）。\n"
            "  2. 打开已登录的 bilibili 或 YouTube 页面。\n"
            "  3. 点扩展图标 → Export → 保存为 cookies.txt。\n"
            "  4. 回到本软件，打开“登录态”开关并选择该文件。\n\n"
            "注意：该文件等同于账号凭据，请仅存本地，不要分享或上传网盘。\n"
            "失效时（提示需要登录）重新导出一次即可。"
        )
        ctk.CTkLabel(top, text=text, justify="left", anchor="nw",
                     font=_font(12), wraplength=520).grid(
            row=0, column=0, sticky="nsew", padx=18, pady=18)
        top.grid_columnconfigure(0, weight=1)
        top.grid_rowconfigure(0, weight=1)
        ctk.CTkButton(top, text="知道了", width=90, command=top.destroy).grid(
            row=1, column=0, pady=(0, 16))

    def _browse(self) -> None:
        initial = self.dir_entry.get().strip() or default_save_dir()
        chosen = filedialog.askdirectory(
            parent=self, initialdir=initial if os.path.isdir(initial) else None)
        if chosen:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, os.path.normpath(chosen))
            self._save_cfg()

    def _set_msg(self, text: str, color=C_GRAY) -> None:
        self.msg_label.configure(text=text, text_color=color)
        if self._msg_after_id:
            self.after_cancel(self._msg_after_id)
        self._msg_after_id = self.after(
            6000, lambda: self.msg_label.configure(text=""))

    def _save_cfg(self) -> None:
        self.cfg.update({
            "save_dir": self.dir_entry.get().strip() or default_save_dir(),
            "concurrency": int(self.concurrency_var.get() or 2),
            "topmost": bool(self.topmost_var.get()),
            "use_cookies": bool(self.cookie_var.get()),
            "use_proxy": bool(self.proxy_var.get()),
        })
        save_config(self.cfg)

    # ---- 事件轮询（§4.4） ----

    def _tick(self) -> None:
        try:
            self._tick_once()
        except Exception as e:
            # pythonw 下异常不可见，必须自带兜底，否则界面冻结（实施要点）
            self._set_msg(f"界面刷新异常：{e}", C_RED)
        finally:
            self.after(100, self._tick)

    def _tick_once(self) -> None:
        while True:
            try:
                item = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if item[0] == "probe_done":
                _, url, save_dir, result = item
                self._handle_probe_done(url, save_dir, result)
                continue
            task, kind = item
            card = self.cards.get(task)
            if card is not None and kind == "status":
                card.apply_status()
        for card in self.cards.values():
            card.refresh()
        self._update_counts()

    def _update_counts(self) -> None:
        active = queued = done = failed = 0
        for t in self.tasks:
            with t.lock:
                s = t.status
            if s in (TaskStatus.DOWNLOADING, TaskStatus.MERGING):
                active += 1
            elif s in (TaskStatus.QUEUED, TaskStatus.COOLDOWN):
                queued += 1
            elif s == TaskStatus.DONE:
                done += 1
            elif s == TaskStatus.FAILED:
                failed += 1
        self.counts_label.configure(
            text=f"活动 {active} · 排队 {queued} · 完成 {done} · 失败 {failed}")

    # ---- 关窗 ----

    def _on_close(self) -> None:
        for t in self.tasks:
            t.cancel_event.set()
        self.pool.shutdown()
        self._save_cfg()
        self.destroy()


# ============================================================
# §8 入口
# ============================================================

def main() -> None:
    _install_excepthook()
    # 顺手收掉上次异常退出留下的 cookie 副本（里面是完整登录凭据）
    cleanup_stale_cookie_copies()
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")

    cfg = load_config()
    if not os.path.isdir(cfg["save_dir"]):
        cfg["save_dir"] = default_save_dir()

    ffmpeg_path = shutil.which("ffmpeg")
    parts, enabled = [], True
    if ffmpeg_path:
        try:
            out = subprocess.run([ffmpeg_path, "-version"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace",
                                 timeout=5, stdin=subprocess.DEVNULL)
            m = re.search(r"ffmpeg version (\S+)", (out.stdout or "").splitlines()[0]
                          if out.stdout else "")
            ver = m.group(1).split("-git")[0][:18] if m else ""
            parts.append(f"✓ ffmpeg {ver} 已就绪" if ver else "✓ ffmpeg 已就绪")
        except Exception:
            parts.append("✗ ffmpeg 不可用")
            enabled = False
    else:
        parts.append("✗ 未找到 ffmpeg，无法合并音视频")
        enabled = False

    if YTDLP_AVAILABLE:
        parts.append(f"yt-dlp {YTDLP_VERSION}")
    else:
        parts.append("✗ 未安装 yt-dlp")
        enabled = False

    if not parts:
        parts.append("就绪")

    app = App(cfg, "　".join(parts), enabled)
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # pythonw 下异常不可见：必须落盘，否则用户只看到"窗口一闪而过"
        log_error("启动失败", exc)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                f"程序启动失败，详细信息已写入：\n{LOG_FILE}\n\n"
                f"{type(exc).__name__}: {exc}",
                "DASH 视频下载器", 0x10)
        except Exception:
            pass
        raise SystemExit(1)
