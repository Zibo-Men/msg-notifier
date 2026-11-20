"""探测脚本：观察飞书 / Teams 收到消息时，Windows 上到底发生了什么。

运行期间（默认 120 秒）请让别人给你发几条飞书、Teams 消息，并在中途把
飞书/Teams 窗口最小化一次、切到别的窗口一次。结束后把 probe_output.txt 发给我。

记录内容：
  [FLASH]   任务栏按钮闪烁（FlashWindowEx），所有进程
  [REDRAW]  目标进程的任务栏按钮重绘（标题/图标变化）
  [SHOW]    目标进程新出现的顶层窗口（自绘弹窗通常在这里）+ 窗口里能读到的文字
  [TITLE]   目标进程顶层窗口标题变化
  [TASKBAR] 任务栏/托盘里按钮名称（含提示文字）的变化
  [ICON]    目标应用任务栏按钮/托盘图标的像素变化（保存截图到 probe_shots/）
  [DB]      Windows 通知数据库新记录（toast / badge / tile …）

依赖（可选，没装会跳过对应功能）：
  pip install uiautomation pillow
用法：
  python probe.py              # 120 秒
  python probe.py --seconds 300
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import sys
import time
from datetime import datetime
from pathlib import Path

if sys.platform != "win32":
    sys.exit("probe.py 只能在 Windows 上运行")

HERE = Path(__file__).resolve().parent
OUT = open(HERE / "probe_output.txt", "w", encoding="utf-8")
SHOTS = HERE / "probe_shots"
TARGETS = ("feishu", "lark", "ms-teams", "teams", "飞书")

# 必须在截图/UIA 之前设置，否则坐标会被 DPI 缩放
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass


def out(tag: str, msg: str) -> None:
    line = f"{datetime.now():%H:%M:%S.%f}"[:-3] + f" [{tag}] {msg}"
    print(line, flush=True)
    OUT.write(line + "\n")
    OUT.flush()


try:
    import uiautomation as auto
except Exception as e:  # noqa: BLE001
    auto = None
    print(f"未安装 uiautomation（{e}），跳过任务栏/窗口文字读取。pip install uiautomation")
try:
    from PIL import ImageGrab
except Exception as e:  # noqa: BLE001
    ImageGrab = None
    print(f"未安装 pillow（{e}），跳过图标像素监测。pip install pillow")

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
WINEVENT_OUTOFCONTEXT = 0
GA_ROOT = 2
SHELL_CODES = {1: "WINDOWCREATED", 2: "WINDOWDESTROYED", 4: "ACTIVATED", 6: "REDRAW",
               0x8004: "RUDEACTIVATED", 0x8006: "FLASH"}

# ---------------------------------------------------------------- 窗口信息
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


def win_rect(hwnd) -> str:
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return f"({r.left},{r.top}) {r.right - r.left}x{r.bottom - r.top}"


def is_target(name: str) -> bool:
    low = name.lower()
    return any(t in low for t in TARGETS)


def describe(hwnd) -> str:
    return f'{proc_name(hwnd)} hwnd={hwnd:#x} class="{win_class(hwnd)}" title="{win_text(hwnd)}" rect={win_rect(hwnd)}'


# ---------------------------------------------------------------- 事件处理
pending_dumps: list[int] = []
last_titles: dict[int, str] = {}


def on_shell(code: int, hwnd) -> None:
    if not hwnd:
        return
    name = proc_name(hwnd)
    tag = SHELL_CODES.get(code, f"SHELL{code:#x}")
    if code == 0x8006:
        out("FLASH", describe(hwnd))
    elif is_target(name) and code in (6, 1, 0x8004):
        out(tag if code != 6 else "REDRAW", describe(hwnd))


@WINEVENTPROC
def win_event(hook, event, hwnd, id_obj, id_child, thread, ts):  # noqa: ARG001
    try:
        if not hwnd or id_obj != 0 or id_child != 0:
            return
        if user32.GetAncestor(hwnd, GA_ROOT) != hwnd:
            return
        name = proc_name(hwnd)
        if not is_target(name):
            return
        if event == EVENT_OBJECT_SHOW:
            out("SHOW", describe(hwnd))
            pending_dumps.append(hwnd)
        elif event == EVENT_OBJECT_NAMECHANGE:
            t = win_text(hwnd)
            if last_titles.get(hwnd) != t:
                last_titles[hwnd] = t
                out("TITLE", f'{name} hwnd={hwnd:#x} -> "{t}"')
    except Exception as e:  # noqa: BLE001
        out("ERR", f"win_event: {e}")


# ---------------------------------------------------------------- UIA：窗口文字、任务栏
def dump_window_text(hwnd) -> None:
    if auto is None:
        return
    try:
        ctrl = auto.ControlFromHandle(hwnd)
        texts = []
        for c, _depth in auto.WalkControl(ctrl, maxDepth=10):
            n = (c.Name or "").strip()
            if n and n not in texts:
                texts.append(n)
            if len(texts) >= 30:
                break
        out("TEXT", f"hwnd={hwnd:#x} UIA 读到 {len(texts)} 段: " + " | ".join(t[:60] for t in texts))
    except Exception as e:  # noqa: BLE001
        out("TEXT", f"hwnd={hwnd:#x} UIA 读取失败: {e}")


taskbar_prev: dict[str, tuple] = {}
icon_hash: dict[str, str] = {}
icon_saved: dict[str, int] = {}


def scan_taskbar(first: bool) -> None:
    if auto is None:
        return
    roots = []
    for cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd", "TopLevelWindowForOverflowXamlIsland"):
        h = user32.FindWindowW(cls, None)
        if h:
            roots.append(h)
    cur: dict[str, tuple] = {}
    try:
        for h in roots:
            for c, _d in auto.WalkControl(auto.ControlFromHandle(h), maxDepth=12):
                if c.ControlTypeName not in ("ButtonControl", "MenuItemControl", "ListItemControl"):
                    continue
                name = (c.Name or "").strip()
                if not name:
                    continue
                key = f"{c.AutomationId}|{c.ClassName}|{name.split(' - ')[0][:30]}"
                r = c.BoundingRectangle
                cur[key] = (name, (r.left, r.top, r.right, r.bottom))
    except Exception as e:  # noqa: BLE001
        out("ERR", f"任务栏扫描: {e}")
        return
    if first:
        out("TASKBAR", f"初始共 {len(cur)} 个按钮：")
        for k, (n, rect) in cur.items():
            out("TASKBAR", f"  {n}  rect={rect}  key={k}")
    else:
        for k, (n, rect) in cur.items():
            old = taskbar_prev.get(k)
            if old is None:
                out("TASKBAR", f"+ 新按钮: {n}")
            elif old[0] != n:
                out("TASKBAR", f'~ 名称变化: "{old[0]}" -> "{n}"')
        for k, (n, _r) in taskbar_prev.items():
            if k not in cur:
                out("TASKBAR", f"- 按钮消失: {n}")
    taskbar_prev.clear()
    taskbar_prev.update(cur)
    watch_icons(cur)


def watch_icons(cur: dict[str, tuple]) -> None:
    if ImageGrab is None:
        return
    for k, (name, (l, t, r, b)) in cur.items():
        if not is_target(name) or r <= l or b <= t:
            continue
        try:
            img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)
        except Exception as e:  # noqa: BLE001
            out("ERR", f"截图失败 {name}: {e}")
            continue
        h = hashlib.md5(img.tobytes()).hexdigest()[:10]
        if icon_hash.get(k) != h:
            first = k not in icon_hash
            icon_hash[k] = h
            n = icon_saved.get(k, 0)
            fn = ""
            if n < 8:
                SHOTS.mkdir(exist_ok=True)
                safe = "".join(ch if ch.isalnum() else "_" for ch in name[:20])
                fn = SHOTS / f"{datetime.now():%H%M%S}_{safe}_{h}.png"
                img.save(fn)
                icon_saved[k] = n + 1
            out("ICON", f'{"初始" if first else "变化"} "{name}" hash={h} {fn and "已存 " + fn.name}')


# ---------------------------------------------------------------- 通知数据库
db_last = 0


def scan_db(first: bool) -> None:
    global db_last
    try:
        from notify_agent import DEFAULT_DB, NotificationDB, parse_payload
        con = NotificationDB(DEFAULT_DB)._connect()
        try:
            rows = con.execute(
                "SELECT n.Id, n.Type, n.Payload, IFNULL(h.PrimaryId,'') FROM Notification n "
                "LEFT JOIN NotificationHandler h ON n.HandlerId=h.RecordId WHERE n.Id > ? ORDER BY n.Id",
                (db_last,),
            ).fetchall()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        out("ERR", f"数据库: {e}")
        return
    for rid, typ, payload, app in rows:
        db_last = max(db_last, rid)
        if first:
            continue
        title, body = parse_payload(payload)
        raw = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        out("DB", f"[{rid}] type={typ} app={app} {title} {body}".strip() + ("" if title else f" raw={raw[:150]}"))


# ---------------------------------------------------------------- 主循环
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=120)
    args = ap.parse_args()

    hinst = kernel32.GetModuleHandleW(None)
    shell_msg = user32.RegisterWindowMessageW("SHELLHOOK")
    start = time.time()
    tick = {"n": 0}

    @WNDPROC
    def wndproc(hwnd, msg, wparam, lparam):
        try:
            if msg == shell_msg:
                on_shell(wparam & 0xFFFFFFFF, lparam)
                return 0
            if msg == WM_TIMER:
                tick["n"] += 1
                while pending_dumps:
                    dump_window_text(pending_dumps.pop(0))
                scan_db(False)
                scan_taskbar(False)
                left = args.seconds - (time.time() - start)
                if tick["n"] % 15 == 0:
                    print(f"... 还剩 {left:.0f} 秒")
                if left <= 0:
                    user32.PostQuitMessage(0)
                return 0
        except Exception as e:  # noqa: BLE001
            out("ERR", f"wndproc: {e}")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc
    wc.hInstance = hinst
    wc.lpszClassName = "MsgNotifierProbe"
    if not user32.RegisterClassW(ctypes.byref(wc)):
        sys.exit(f"RegisterClassW 失败: {ctypes.get_last_error()}")
    hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "probe", 0, 0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        sys.exit(f"CreateWindowExW 失败: {ctypes.get_last_error()}")
    if not user32.RegisterShellHookWindow(hwnd):
        out("ERR", f"RegisterShellHookWindow 失败: {ctypes.get_last_error()}")
    hooks = [
        user32.SetWinEventHook(EVENT_OBJECT_SHOW, EVENT_OBJECT_SHOW, None, win_event, 0, 0, WINEVENT_OUTOFCONTEXT),
        user32.SetWinEventHook(EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_NAMECHANGE, None, win_event, 0, 0,
                               WINEVENT_OUTOFCONTEXT),
    ]
    if not all(hooks):
        out("ERR", "SetWinEventHook 失败")

    out("INFO", f"Python {sys.version.split()[0]}，uiautomation={'有' if auto else '无'}，pillow={'有' if ImageGrab else '无'}")
    scan_db(True)
    scan_taskbar(True)
    out("INFO", f"开始监测 {args.seconds} 秒，现在请让别人给你发飞书、Teams 消息……")
    user32.SetTimer(hwnd, 1, 2000, None)

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    out("INFO", f"结束。结果在 {HERE / 'probe_output.txt'}，截图在 {SHOTS}")
    OUT.close()


if __name__ == "__main__":
    main()
