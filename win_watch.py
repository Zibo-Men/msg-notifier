"""Windows 窗口层面的消息检测（Teams / 飞书等自绘提醒的应用）。

信号来源：
  - EVENT_OBJECT_SHOW：目标进程弹出小窗口（自绘消息弹窗）→ 用 UI Automation 读内容，
    读不到再截图 OCR，再不行就推“有新消息”
  - 任务栏闪烁（shell hook HSHELL_FLASH）→ 延迟几秒的“有新消息”兜底
  - 窗口标题里的未读数上升 → 兜底
  - 托盘图标提示文字变化 / 图标开始闪烁（像素变化）→ 兜底
兜底信号在同一应用刚推过具体内容时会被 Notifier 抑制，所以一条消息通常只推一次。

debug_apps 里的应用（默认飞书）会记录所有相关事件、完整 UIA 文字和 OCR 结果，
并把弹窗截图存到 debug_shots/，用于以后优化解析规则。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import logging
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from text_parse import clean_lines, clean_ocr, is_ignored_popup, parse_teams_items, unread_in_title

log = logging.getLogger("notify_agent")

HERE = Path(__file__).resolve().parent
OCR_PS1 = HERE / "ocr.ps1"
DEBUG_DIR = HERE / "debug_shots"
DEBUG_MAX_FILES = 100

# 必须在 UIA / 截图之前设置，否则坐标会被 DPI 缩放
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:  # noqa: BLE001
    pass

try:
    import uiautomation as auto
except Exception as e:  # noqa: BLE001
    auto = None
    log.warning("未安装 uiautomation（%s），无法读取弹窗文字和托盘状态：pip install uiautomation", e)
try:
    from PIL import ImageGrab
except Exception as e:  # noqa: BLE001
    ImageGrab = None
    log.warning("未安装 pillow（%s），无法截图 OCR / 监测托盘图标：pip install pillow", e)

# ---------------------------------------------------------------- Win32 声明
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HANDLE),
        ("hCursor", wt.HANDLE), ("hbrBackground", wt.HANDLE), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
    ]


user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.RegisterShellHookWindow.argtypes = [wt.HWND]
user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
user32.RegisterWindowMessageW.restype = wt.UINT
user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, WINEVENTPROC, wt.DWORD, wt.DWORD, wt.DWORD]
user32.SetWinEventHook.restype = wt.HANDLE
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindow.argtypes = [wt.HWND]
user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]

WM_TIMER = 0x0113
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_NAMECHANGE = 0x800C
GA_ROOT = 2
HSHELL_FLASH = 0x8006
TIMER_POLL, TIMER_TASKS = 1, 2

_proc_cache: dict[int, str] = {}


def proc_name(hwnd) -> str:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    p = pid.value
    if p in _proc_cache:
        return _proc_cache[p]
    name = f"pid{p}"
    h = kernel32.OpenProcess(0x1000, False, p)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            name = Path(buf.value).name
        kernel32.CloseHandle(h)
    if len(_proc_cache) > 2000:
        _proc_cache.clear()
    _proc_cache[p] = name
    return name


def win_text(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def win_class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def rect_of(hwnd) -> tuple[int, int, int, int]:
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def describe(hwnd) -> str:
    l, t, r, b = rect_of(hwnd)
    return f'{proc_name(hwnd)} hwnd={hwnd:#x} class="{win_class(hwnd)}" title="{win_text(hwnd)}" ({l},{t}) {r - l}x{b - t}'


# ---------------------------------------------------------------- UIA 读取
def uia_items(hwnd, max_nodes: int = 500) -> list[tuple[str, str, int]]:
    """返回 [(控件类型, 文字, 深度)]。"""
    ctrl = auto.ControlFromHandle(hwnd)
    items, nodes = [], 0
    for c, depth in auto.WalkControl(ctrl, maxDepth=30):
        nodes += 1
        typ = c.ControlTypeName.replace("Control", "")
        name = (c.Name or "").strip()
        if name:
            items.append((typ, name, depth))
        if nodes >= max_nodes:
            break
    return items


# ---------------------------------------------------------------- 主类
class WindowWatcher:
    def __init__(self, cfg: dict, notifier) -> None:
        self.cfg = cfg
        self.notifier = notifier
        self.apps = cfg["window_apps"]
        for a in self.apps:
            a["_procs"] = [p.lower() for p in a.get("processes", [])]
        self.tasks: list[tuple[float, object]] = []
        self._tasks_lock = threading.Lock()         # OCR 线程也会 schedule
        self.flash_at: dict[str, float] = {}        # 应用名 -> 最近一次任务栏闪烁时间
        self.titles: dict[int, int] = {}            # hwnd -> 标题里的未读数
        self.tray_names: dict[str, str] = {}        # tray key -> 最近名称
        self.tray_base: dict[str, str] = {}         # tray key -> 初始名称
        self.tray_hash: dict[str, str] = {}
        self.tray_changed_at: dict[str, float] = {}
        self.tray_hash_base: dict[str, str] = {}
        self.popup_seq = 0
        self._tray_tick = 0

    # ---- 工具
    @property
    def debug_apps(self) -> set[str]:
        """每次现读 cfg，hub 下发的新设置立即生效。"""
        return set(self.cfg.get("debug_apps", []))

    def app_of(self, hwnd) -> dict | None:
        p = proc_name(hwnd).lower()
        for a in self.apps:
            if p in a["_procs"]:
                return a
        return None

    def dbg(self, app: dict, msg: str) -> None:
        if app["name"] in self.debug_apps:
            log.info("[DEBUG-%s] %s", app["name"], msg)

    def schedule(self, delay: float, fn) -> None:
        with self._tasks_lock:
            self.tasks.append((time.time() + delay, fn))

    def run_due(self) -> None:
        now = time.time()
        with self._tasks_lock:
            due = [t for t in self.tasks if t[0] <= now]
            for t in due:
                self.tasks.remove(t)
        for t in due:
            try:
                t[1]()
            except Exception as e:  # noqa: BLE001
                log.exception("定时任务出错: %s", e)

    def generic(self, app: dict, reason: str) -> None:
        """延迟一会儿再推“有新消息”，给弹窗内容留出时间；Notifier 会在刚推过内容时抑制它。"""
        delay = self.cfg.get("generic_delay", 5)
        self.schedule(delay, lambda: self.notifier.submit(app["name"], "", "有新消息", source=reason, generic=True))

    # ---- 事件
    def on_shell(self, code: int, hwnd) -> None:
        if code != HSHELL_FLASH or not hwnd:
            return
        app = self.app_of(hwnd)
        if app is None:
            log.debug("FLASH 其他程序: %s", describe(hwnd))
            return
        log.info("FLASH [%s] %s", app["name"], describe(hwnd))
        self.flash_at[app["name"]] = time.time()
        self.generic(app, "flash")

    def on_win_event(self, event: int, hwnd) -> None:
        if user32.GetAncestor(hwnd, GA_ROOT) != hwnd:
            return
        app = self.app_of(hwnd)
        if app is None:
            return
        if event == EVENT_OBJECT_SHOW:
            l, t, r, b = rect_of(hwnd)
            w, h = r - l, b - t
            pmin, pmax = app.get("popup_min", [120, 40]), app.get("popup_max", [800, 600])
            is_popup = pmin[0] <= w <= pmax[0] and pmin[1] <= h <= pmax[1]
            self.dbg(app, f"SHOW {'弹窗' if is_popup else '非弹窗'} {describe(hwnd)}")
            if is_popup:
                self.popup_seq += 1
                seq, shown = self.popup_seq, time.time()
                self.schedule(0.5, lambda: self.read_popup(app, hwnd, seq, shown, attempt=1))
        elif event == EVENT_OBJECT_NAMECHANGE:
            title = win_text(hwnd)
            n = unread_in_title(title)
            prev = self.titles.get(hwnd)
            self.titles[hwnd] = n
            if prev is not None and n != prev:
                self.dbg(app, f"TITLE 未读 {prev} -> {n}: {title}")
            if prev is not None and n > prev:
                log.info("标题未读数上升 [%s] %d -> %d", app["name"], prev, n)
                self.generic(app, "title")

    # ---- 弹窗内容
    def popup_fallback(self, app: dict, reason: str) -> None:
        """弹窗读不到时的“有新消息”兜底。飞书等关掉了它：划词工具栏等界面一闪就没，
        会被误当成新消息；真正的新消息必然伴随任务栏闪烁，闪烁本身已有兜底。"""
        if app.get("popup_fallback", True):
            self.generic(app, reason)
        else:
            self.dbg(app, f"弹窗兜底已关闭，不推 ({reason})")

    def ignored(self, app: dict, texts: list[str], seq: int, how: str) -> bool:
        words = app.get("ignore_popup_words") or []
        if words and is_ignored_popup(texts, words):
            log.info("弹窗 #%d [%s] 是划词工具栏等界面，忽略（%s）: %s",
                     seq, app["name"], how, " | ".join(texts)[:100])
            return True
        return False

    def submit_popup(self, app: dict, title: str, body: str, source: str, seq: int, shown: float,
                     waited: bool = False) -> None:
        """提交弹窗内容。popup_needs_flash 的应用：弹窗前后 3 秒内必须有任务栏闪烁，
        否则不是新消息通知（菜单、名片、工具栏之类）。闪烁可能稍晚，最多等到弹窗后 4 秒。"""
        if app.get("popup_needs_flash"):
            flashed = self.flash_at.get(app["name"], 0) >= shown - 3
            if not flashed:
                wait = shown + 4 - time.time()
                if not waited and wait > 0:
                    self.dbg(app, f"弹窗 #{seq} 还没看到任务栏闪烁，{wait:.1f}s 后再判断")
                    self.schedule(wait, lambda: self.submit_popup(app, title, body, source, seq, shown, waited=True))
                    return
                log.info("弹窗 #%d [%s] 没有伴随任务栏闪烁，不是新消息，忽略: %s | %s",
                         seq, app["name"], title, (body or "")[:60])
                return
        self.notifier.submit(app["name"], title, body or "", source=source)

    def read_popup(self, app: dict, hwnd, seq: int, shown: float, attempt: int) -> bool:
        """返回 True 表示已处理（提交、忽略或等待闪烁），不用再重试/OCR。"""
        if not user32.IsWindow(hwnd):
            log.info("弹窗 #%d [%s] 已关闭，无法读取", seq, app["name"])
            self.popup_fallback(app, "popup-gone")
            return True
        debug = app["name"] in self.debug_apps
        shot = None
        if attempt == 1 and (debug or auto is None):
            shot = self.screenshot(hwnd, seq, app, keep=debug)
        title = body = None
        if auto is not None:
            try:
                items = uia_items(hwnd)
                if debug:
                    self.dbg(app, f"UIA #{seq} 第{attempt}次 {len(items)} 段: "
                             + " | ".join(f"{t}@{d}:{n[:80]}" for t, n, d in items))
                if self.ignored(app, [n for _, n, _ in items], seq, "UIA"):
                    return True
                if app.get("parser") == "teams":
                    parsed = parse_teams_items(items)
                    if parsed:
                        title, body = parsed
                if title is None:
                    lines = clean_lines([(t, n) for t, n, _ in items], {app["name"], win_text(hwnd)})
                    if lines:
                        title, body = lines[0], "\n".join(lines[1:])
            except Exception as e:  # noqa: BLE001
                log.warning("UIA 读取弹窗 #%d 失败: %s", seq, e)
        if title:
            log.info("弹窗 #%d [%s] UIA 读到: %s | %s", seq, app["name"], title, (body or "")[:80])
            self.submit_popup(app, title, body or "", "popup-uia", seq, shown)
            if debug and shot:
                self.ocr_async(app, shot, seq, shown, push=False)  # 调试：也看看 OCR 的结果
            return True
        if attempt == 1:
            # 第一次读不到：先截图（窗口可能很快消失），1 秒后再读一次
            shot = shot or self.screenshot(hwnd, seq, app, keep=debug)
            self.schedule(1.0, lambda: self.read_popup_retry(app, hwnd, seq, shown, shot))
        return False

    def read_popup_retry(self, app: dict, hwnd, seq: int, shown: float, shot: Path | None) -> None:
        if user32.IsWindow(hwnd) and auto is not None:
            if self.read_popup(app, hwnd, seq, shown, attempt=2):
                return
        if shot and self.cfg.get("ocr_fallback", True):
            self.ocr_async(app, shot, seq, shown, push=True)
        else:
            self.popup_fallback(app, "popup-unreadable")

    def screenshot(self, hwnd, seq: int, app: dict, keep: bool) -> Path | None:
        if ImageGrab is None or not user32.IsWindowVisible(hwnd):
            return None
        try:
            img = ImageGrab.grab(bbox=rect_of(hwnd), all_screens=True)
            img = img.resize((img.width * 2, img.height * 2))  # 放大后 OCR 更准
            if keep:
                DEBUG_DIR.mkdir(exist_ok=True)
                fn = DEBUG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_{app['name']}_{seq:03d}.png"
                self.prune_debug()
            else:
                fn = Path(tempfile.gettempdir()) / f"notify_agent_popup_{seq % 10}.png"
            img.save(fn)
            if keep:
                self.dbg(app, f"截图 #{seq} 已存 {fn.name}")
            return fn
        except Exception as e:  # noqa: BLE001
            log.warning("截图失败: %s", e)
            return None

    @staticmethod
    def prune_debug() -> None:
        files = sorted(DEBUG_DIR.glob("*.png"))
        for f in files[: max(0, len(files) - DEBUG_MAX_FILES)]:
            f.unlink(missing_ok=True)

    def ocr_async(self, app: dict, path: Path, seq: int, shown: float, push: bool) -> None:
        def work() -> None:
            text = run_ocr(path, self.cfg.get("ocr_lang", "zh-Hans-CN"))
            self.dbg(app, f"OCR #{seq}: " + text.replace("\n", " | "))
            if not push:
                return
            lines = clean_ocr(text, {app["name"], "Microsoft Teams"})
            if lines and self.ignored(app, lines, seq, "OCR"):
                return
            if lines:
                log.info("弹窗 #%d [%s] OCR 读到: %s", seq, app["name"], " | ".join(lines)[:120])
                self.submit_popup(app, lines[0], "\n".join(lines[1:]), "popup-ocr", seq, shown)
            else:
                self.popup_fallback(app, "popup-ocr-empty")

        threading.Thread(target=work, daemon=True).start()

    # ---- 托盘
    def scan_tray(self) -> None:
        if auto is None:
            return
        roots = [h for h in (user32.FindWindowW(c, None) for c in ("Shell_TrayWnd", "TopLevelWindowForOverflowXamlIsland")) if h]
        found: dict[str, tuple[dict, str, tuple]] = {}
        try:
            for h in roots:
                for c, _d in auto.WalkControl(auto.ControlFromHandle(h), maxDepth=12):
                    if c.AutomationId != "NotifyItemIcon" and not (c.ClassName or "").startswith("SystemTray."):
                        continue
                    name = (c.Name or "").strip()
                    for a in self.apps:
                        if any(k.lower() in name.lower() for k in a.get("tray_keywords", [])):
                            r = c.BoundingRectangle
                            found[a["name"]] = (a, name, (r.left, r.top, r.right, r.bottom))
                            break
        except Exception as e:  # noqa: BLE001
            log.debug("托盘扫描失败: %s", e)
            return
        now = time.time()
        for key, (app, name, rect) in found.items():
            # 提示文字变化
            if key not in self.tray_base:
                self.tray_base[key] = name
                log.info("托盘图标 [%s]: %s", key, name.replace("\n", " "))
            prev = self.tray_names.get(key)
            self.tray_names[key] = name
            if prev is not None and name != prev:
                log.info("托盘提示变化 [%s]: %r -> %r", key, prev, name)
                if name != self.tray_base[key] and app.get("tray_signals", True):
                    self.generic(app, "tray-name")
            # 图标像素变化（闪烁）：只在“安静一段时间后开始变化”时算一次信号
            if ImageGrab is None or rect[2] <= rect[0] or rect[3] <= rect[1]:
                continue
            try:
                img = ImageGrab.grab(bbox=rect, all_screens=True)
            except Exception:  # noqa: BLE001
                continue
            h = hashlib.md5(img.tobytes()).hexdigest()[:10]
            old = self.tray_hash.get(key)
            base = self.tray_hash_base.setdefault(key, h)
            self.tray_hash[key] = h
            if old is not None and h != old:
                quiet = now - self.tray_changed_at.get(key, 0)
                self.tray_changed_at[key] = now
                self.dbg(app, f"托盘图标变化 hash {old} -> {h}（距上次变化 {quiet:.0f}s，初始 {base}）")
                # 变回初始样子 = 未读被清掉，不算新消息；Teams 等有弹窗的应用关闭了托盘信号
                if quiet > 20 and h != base and app.get("tray_signals", True):
                    self.generic(app, "tray-icon")

    # ---- 消息循环
    def run(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        shell_msg = user32.RegisterWindowMessageW("SHELLHOOK")
        tray_every = max(1, int(self.cfg.get("tray_poll_seconds", 3) * 1000 // 500))

        @WNDPROC
        def wndproc(hwnd, msg, wparam, lparam):
            try:
                if msg == shell_msg:
                    self.on_shell(wparam & 0xFFFFFFFF, lparam)
                    return 0
                if msg == WM_TIMER:
                    if wparam == TIMER_TASKS:
                        self.run_due()
                        self._tray_tick += 1
                        if self._tray_tick % tray_every == 0:
                            self.scan_tray()
                    return 0
            except KeyboardInterrupt:
                user32.PostQuitMessage(0)
                return 0
            except Exception as e:  # noqa: BLE001
                log.exception("wndproc 出错: %s", e)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        @WINEVENTPROC
        def win_event(hook, event, hwnd, id_obj, id_child, thread, ts):  # noqa: ARG001
            try:
                if hwnd and id_obj == 0 and id_child == 0:
                    self.on_win_event(event, hwnd)
            except Exception as e:  # noqa: BLE001
                log.exception("win_event 出错: %s", e)

        # 回调对象必须保持引用，否则会被回收导致崩溃
        self._refs = (wndproc, win_event)
        wc = WNDCLASSW()
        wc.lpfnWndProc = wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "MsgNotifierAgent"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            raise OSError(f"RegisterClassW 失败: {ctypes.get_last_error()}")
        hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "msg-notifier", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            raise OSError(f"CreateWindowExW 失败: {ctypes.get_last_error()}")
        if not user32.RegisterShellHookWindow(hwnd):
            log.error("RegisterShellHookWindow 失败: %s", ctypes.get_last_error())
        for ev in (EVENT_OBJECT_SHOW, EVENT_OBJECT_NAMECHANGE):
            if not user32.SetWinEventHook(ev, ev, None, win_event, 0, 0, 0):
                log.error("SetWinEventHook(%#x) 失败", ev)
        user32.SetTimer(hwnd, TIMER_TASKS, 500, None)
        log.info("窗口监测已启动：%s；调试应用：%s", ", ".join(a["name"] for a in self.apps),
                 ", ".join(self.debug_apps) or "无")
        self.scan_tray()

        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def run_ocr(path: Path, lang: str) -> str:
    if not OCR_PS1.exists():
        return ""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(OCR_PS1),
             "-Path", str(path), "-Lang", lang],
            capture_output=True, timeout=60, encoding="utf-8", errors="replace",
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        if r.stderr.strip():
            log.warning("OCR stderr: %s", r.stderr.strip()[:300])
        return r.stdout or ""
    except Exception as e:  # noqa: BLE001
        log.warning("OCR 失败: %s", e)
        return ""
