"""Win32 API wrappers for input + window manipulation.

DPI awareness is set to PER_MONITOR_AWARE_V2 at import time so all coordinates
returned by GetWindowRect / GetClientRect are in physical pixels matching what
SendInput consumes — no implicit virtual-pixel scaling surprises.
"""
from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import time

from .input.keys import KeyStroke, resolve_key

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shcore = ctypes.WinDLL("shcore", use_last_error=True)

# DPI awareness — call once at import
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
try:
    user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
except (OSError, AttributeError):
    # Older Windows — fall back to per-monitor v1
    try:
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (OSError, AttributeError):
        pass


# === SendInput structures ===

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_EXTENDEDKEY = 0x0001

VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_TAB = 0x09
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_F1 = 0x70  # F1..F12 = 0x70..0x7B

KEY_NAME_TO_VK = {
    "return": VK_RETURN, "enter": VK_RETURN,
    "escape": VK_ESCAPE, "esc": VK_ESCAPE,
    "space": VK_SPACE,
    "tab": VK_TAB,
    "left": VK_LEFT, "up": VK_UP, "right": VK_RIGHT, "down": VK_DOWN,
}
for i in range(12):
    KEY_NAME_TO_VK[f"f{i+1}"] = VK_F1 + i


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


# === Window helpers ===

@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    window_rect: tuple[int, int, int, int]  # (l, t, r, b) screen coords
    client_size: tuple[int, int]            # (w, h)
    client_screen_origin: tuple[int, int]   # ClientToScreen((0,0))
    dpi: int                                # GetDpiForWindow
    is_foreground: bool
    is_minimized: bool = False
    exe: str | None = None                  # basename of the owning process image
    monitor: int | None = None              # index into get_monitor_rects() order


# Prototypes for the calls whose handle/struct arguments must not be truncated.
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = ctypes.c_void_p
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

GA_ROOT = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MONITOR_DEFAULTTONEAREST = 2


def exe_of_pid(pid: int) -> str | None:
    """Basename of the process image, or None when it cannot be opened."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value.replace("/", "\\").rsplit("\\", 1)[-1] or None
    finally:
        kernel32.CloseHandle(handle)


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(_FILETIME)] * 4
kernel32.GetProcessTimes.restype = wintypes.BOOL


def process_cpu_ms(pid: int) -> int | None:
    """Kernel + user CPU time the process has used so far, in milliseconds."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        created, exited, kernel, user = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None

        def ticks(ft: _FILETIME) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        return (ticks(kernel) + ticks(user)) // 10_000  # 100 ns units -> ms
    finally:
        kernel32.CloseHandle(handle)


def root_window(hwnd: int) -> int:
    """Top-level ancestor of hwnd (hwnd itself when it already is one, 0 if invalid)."""
    if not hwnd:
        return 0
    return int(user32.GetAncestor(hwnd, GA_ROOT) or 0)


def root_window_at(x: int, y: int) -> int:
    """Top-level window under a screen point (0 when nothing is there).
    WindowFromPoint skips hidden, disabled and click-through windows."""
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return root_window(int(hwnd or 0))


def monitor_index_of_hwnd(hwnd: int) -> int | None:
    """Index of the monitor the window is mostly on, in EnumDisplayMonitors order."""
    target = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not target:
        return None
    handles: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def enum_proc(hmonitor, hdc, rect, lparam):
        handles.append(hmonitor)
        return True

    user32.EnumDisplayMonitors(None, None, enum_proc, 0)
    try:
        return handles.index(target)
    except ValueError:
        return None


GWL_STYLE = -16
WS_CAPTION = 0x00C00000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL


def window_frame_kind(hwnd: int) -> dict:
    """{"has_frame", "maximized", "minimized", "fullscreen"}: what kind of window
    this is, to decide whether resizing it makes sense. "fullscreen" means the
    window rectangle is exactly one monitor's (borderless or exclusive)."""
    style = int(user32.GetWindowLongPtrW(hwnd, GWL_STYLE)) & 0xFFFFFFFF
    wrect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wrect))
    rect = (wrect.left, wrect.top, wrect.right, wrect.bottom)
    return {
        "has_frame": (style & WS_CAPTION) == WS_CAPTION,
        "maximized": bool(user32.IsZoomed(hwnd)),
        "minimized": bool(user32.IsIconic(hwnd)),
        "fullscreen": any(
            (m.left, m.top, m.right, m.bottom) == rect for m in get_monitor_rects()
        ),
    }


def set_window_geometry(
    hwnd: int,
    client_size: tuple[int, int] | None = None,
    position: tuple[int, int] | None = None,
) -> tuple[bool, int]:
    """Resize so the CLIENT area is client_size and/or move the window's top-left
    to ``position`` (screen pixels). Does not activate or reorder the window.
    Returns (ok, last_error)."""
    wrect, crect = wintypes.RECT(), wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wrect))
    user32.GetClientRect(hwnd, ctypes.byref(crect))
    border_w = (wrect.right - wrect.left) - (crect.right - crect.left)
    border_h = (wrect.bottom - wrect.top) - (crect.bottom - crect.top)
    flags = SWP_NOZORDER | SWP_NOACTIVATE
    x, y = position if position is not None else (0, 0)
    cx = cy = 0
    if client_size is not None:
        cx, cy = int(client_size[0]) + border_w, int(client_size[1]) + border_h
    else:
        flags |= SWP_NOSIZE
    if position is None:
        flags |= SWP_NOMOVE
    ok = bool(user32.SetWindowPos(hwnd, None, int(x), int(y), cx, cy, flags))
    return ok, 0 if ok else ctypes.get_last_error()


DWMWA_EXTENDED_FRAME_BOUNDS = 9


def extended_frame_bounds(hwnd: int) -> tuple[int, int, int, int] | None:
    """The window's visible bounds as DWM composes them (no drop shadow); the
    size of a Windows.Graphics.Capture frame of this window."""
    rect = wintypes.RECT()
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        hr = dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
        )
    except (OSError, AttributeError):
        return None
    if hr != 0:
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def describe_window(hwnd: int) -> dict:
    """Small, JSON-friendly identity of a window for error details."""
    if not hwnd:
        return {"hwnd": 0}
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return {"hwnd": int(hwnd), "pid": int(pid.value), "exe": exe_of_pid(pid.value), "title": buf.value}


_AUX_WINDOW_CLASSES = {
    # BepInEx / .NET console window — same pid as the game, not what we want.
    "ConsoleWindowClass",
    # IME composition / candidate windows.
    "IME",
    "MSCTFIME UI",
    # Steam / launcher overlays often piggy-back the game's pid.
    "Default IME",
}


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def find_window_by_pid(pid: int) -> int:
    """Return the main HWND for a given process pid, or 0 if not found.

    Selection rule: among visible top-level windows of this pid, exclude
    known auxiliary classes (BepInEx ConsoleWindowClass, IME, …) and pick
    the one with the largest client area. This reliably picks the game
    render window over BepInEx's AllocConsole log window even though both
    belong to the same process.
    """
    candidates: list[tuple[int, int]] = []  # (area, hwnd)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, lparam):
        proc_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_pid))
        if proc_pid.value != pid or not user32.IsWindowVisible(hwnd):
            return True
        if _class_name(hwnd) in _AUX_WINDOW_CLASSES:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        cw, ch = _client_size(hwnd)
        if length == 0 and (cw, ch) == (0, 0):
            return True
        candidates.append((cw * ch, hwnd))
        return True

    user32.EnumWindows(enum_proc, 0)
    if not candidates:
        return 0
    # Largest client area wins — game render window is always bigger than
    # tooltips, mini overlays, etc.
    candidates.sort(reverse=True)
    return candidates[0][1]


def _client_size(hwnd: int) -> tuple[int, int]:
    rect = wintypes.RECT()
    if user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return (rect.right - rect.left, rect.bottom - rect.top)
    return (0, 0)


def get_window_info(pid: int) -> WindowInfo | None:
    hwnd = find_window_by_pid(pid)
    if not hwnd:
        return None
    return _window_info_from_hwnd(hwnd)


def _window_info_from_hwnd(hwnd: int) -> WindowInfo | None:
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    proc_pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_pid))

    title_len = user32.GetWindowTextLengthW(hwnd)
    title_buf = ctypes.create_unicode_buffer(title_len + 1)
    user32.GetWindowTextW(hwnd, title_buf, title_len + 1)

    wrect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(wrect))

    cw, ch = _client_size(hwnd)

    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))

    try:
        dpi = user32.GetDpiForWindow(hwnd)
    except (OSError, AttributeError):
        dpi = 96

    return WindowInfo(
        hwnd=hwnd,
        pid=proc_pid.value,
        title=title_buf.value,
        window_rect=(wrect.left, wrect.top, wrect.right, wrect.bottom),
        client_size=(cw, ch),
        client_screen_origin=(pt.x, pt.y),
        dpi=dpi,
        is_foreground=(user32.GetForegroundWindow() == hwnd),
        is_minimized=bool(user32.IsIconic(hwnd)),
        exe=exe_of_pid(proc_pid.value),
        monitor=monitor_index_of_hwnd(hwnd),
    )


def get_window_info_by_hwnd(hwnd: int) -> WindowInfo | None:
    return _window_info_from_hwnd(hwnd)


def list_window_infos() -> list[WindowInfo]:
    infos: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if _class_name(hwnd) in _AUX_WINDOW_CLASSES:
            return True
        info = _window_info_from_hwnd(hwnd)
        if info is not None:
            infos.append(info)
        return True

    user32.EnumWindows(enum_proc, 0)
    return infos


def get_monitor_rects() -> list["Rect"]:
    from .models import Rect

    monitors: list[Rect] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    def enum_proc(hmonitor, hdc, rect, lparam):
        monitors.append(Rect(rect.contents.left, rect.contents.top, rect.contents.right, rect.contents.bottom))
        return True

    user32.EnumDisplayMonitors(None, None, enum_proc, 0)
    return monitors


SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
SPIF_SENDCHANGE = 0x2
VK_MENU = 0x12  # Alt — self-press resets last-input timestamp


def focus_window_detailed(pid: int) -> dict:
    """Bring the target window to foreground, return per-step diagnostics.

    Three layers of Windows focus-stealing defense, each defeated by a
    different trick (all required in worst case):

      1. last-input rule         → self-press Alt via keybd_event so our
                                    process holds the freshest input
                                    timestamp.
      2. foreground lock timeout → SystemParametersInfo zero-out, restore
                                    afterwards.
      3. SetForegroundWindow ACL → AttachThreadInput borrows the current
                                    foreground thread's privilege.

    Truth is always GetForegroundWindow() == hwnd at the end. SFW's
    return value is unreliable across animations / restore transitions.
    """
    hwnd = find_window_by_pid(pid)
    if not hwnd:
        return {"success": False, "error": "no window for pid", "hwnd": 0}

    SW_RESTORE = 9
    SW_SHOW = 5
    user32.ShowWindow(hwnd, SW_RESTORE)

    if user32.GetForegroundWindow() == hwnd:
        return {"success": True, "hwnd": hwnd, "path": "already-foreground"}

    # Snapshot the system lock timeout and zero it out for the duration.
    old_timeout = wintypes.UINT(0)
    user32.SystemParametersInfoW(
        SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(old_timeout), 0
    )
    user32.SystemParametersInfoW(
        SPI_SETFOREGROUNDLOCKTIMEOUT, 0, 0, SPIF_SENDCHANGE
    )

    try:
        # Self-press Alt so we satisfy the "last received input" rule.
        # keybd_event is the legacy form but functionally equivalent to
        # SendInput for one key — and it doesn't require building INPUT
        # structures, which keeps this branch cheap.
        with _injecting():  # our own input: must not be mistaken for the user's
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)

        fg_hwnd = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
        target_tid = user32.GetWindowThreadProcessId(hwnd, None)
        my_tid = kernel32.GetCurrentThreadId()

        attached = []
        attach_fg_ok = None
        attach_tg_ok = None
        if fg_tid and fg_tid != my_tid:
            attach_fg_ok = bool(user32.AttachThreadInput(my_tid, fg_tid, True))
            if attach_fg_ok:
                attached.append(fg_tid)
        if target_tid and target_tid != my_tid:
            attach_tg_ok = bool(user32.AttachThreadInput(my_tid, target_tid, True))
            if attach_tg_ok:
                attached.append(target_tid)

        try:
            user32.BringWindowToTop(hwnd)
            user32.ShowWindow(hwnd, SW_SHOW)
            sfw_ret = bool(user32.SetForegroundWindow(hwnd))
        finally:
            for tid in attached:
                user32.AttachThreadInput(my_tid, tid, False)

        final_fg = user32.GetForegroundWindow()
        success = (final_fg == hwnd)

        return {
            "success": success,
            "hwnd": hwnd,
            "path": "force-foreground",
            "fg_before_hwnd": fg_hwnd,
            "fg_before_tid": fg_tid,
            "target_tid": target_tid,
            "attach_fg_ok": attach_fg_ok,
            "attach_target_ok": attach_tg_ok,
            "sfw_returned": sfw_ret,
            "final_fg_hwnd": final_fg,
            "old_lock_timeout_ms": old_timeout.value,
        }
    finally:
        user32.SystemParametersInfoW(
            SPI_SETFOREGROUNDLOCKTIMEOUT, 0, old_timeout.value, SPIF_SENDCHANGE
        )


def focus_window(pid: int) -> bool:
    """Backwards-compatible bool entry point for daemon handler."""
    return bool(focus_window_detailed(pid).get("success"))


# === Foreground / timing helpers ===

def get_foreground_hwnd() -> int:
    """HWND of the current foreground window (0 when none)."""
    return int(user32.GetForegroundWindow() or 0)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


kernel32.GetTickCount.restype = wintypes.DWORD


def tick_count() -> int:
    """GetTickCount (32-bit milliseconds, wraps every ~49.7 days)."""
    return int(kernel32.GetTickCount())


def last_input_tick() -> int | None:
    """GetLastInputInfo.dwTime: tick of the last input event on this desktop
    session, injected input included. None when the call fails."""
    info = _LASTINPUTINFO(ctypes.sizeof(_LASTINPUTINFO), 0)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return int(info.dwTime)


# Presence attribution hook: the daemon installs PresenceMonitor.injecting so
# every SendInput/keybd_event is bracketed by "probe, send, note" under one lock.
_injection_hook = None


def set_injection_hook(factory) -> None:
    global _injection_hook
    _injection_hook = factory


def _injecting():
    return _injection_hook() if _injection_hook is not None else contextlib.nullcontext()


def qpc_ns() -> int:
    """Monotonic high-resolution timestamp in nanoseconds (QueryPerformanceCounter
    on Windows via time.perf_counter_ns)."""
    return time.perf_counter_ns()


# === Coordinate scope translation ===

def translate_to_screen(
    info: WindowInfo,
    x: int,
    y: int,
    scope: str,
    framebuffer_size: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Convert (x, y) in given scope to absolute screen coords.

    scope:
      - "screen"     : (x, y) is already absolute screen coords
      - "client"     : (x, y) is relative to window client area top-left
      - "framebuffer": (x, y) is in framebuffer coords (Unity render texture).
                        If framebuffer_size given, scale to client first; else
                        assume framebuffer == client (no scale).
    """
    if scope == "screen":
        return (x, y)

    cx, cy = info.client_screen_origin

    if scope == "client":
        return (cx + x, cy + y)

    if scope == "framebuffer":
        cw, ch = info.client_size
        if framebuffer_size is not None and framebuffer_size[0] > 0 and framebuffer_size[1] > 0:
            fbw, fbh = framebuffer_size
            sx = x * cw / fbw
            sy = y * ch / fbh
            return (int(cx + sx), int(cy + sy))
        # No framebuffer_size hint → assume framebuffer == client
        return (cx + x, cy + y)

    raise ValueError(f"unknown scope: {scope}")


# === SendInput primitives ===

def _send_inputs(inputs: list[INPUT]) -> int:
    arr_type = INPUT * len(inputs)
    arr = arr_type(*inputs)
    with _injecting():
        return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


_SCREEN_W = None
_SCREEN_H = None


def _virtual_screen_size() -> tuple[int, int]:
    """Width/height of the virtual screen spanning all monitors."""
    global _SCREEN_W, _SCREEN_H
    if _SCREEN_W is None:
        SM_CXVIRTUALSCREEN = 78
        SM_CYVIRTUALSCREEN = 79
        _SCREEN_W = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        _SCREEN_H = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return _SCREEN_W, _SCREEN_H


def _virtual_screen_origin() -> tuple[int, int]:
    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    return user32.GetSystemMetrics(SM_XVIRTUALSCREEN), user32.GetSystemMetrics(SM_YVIRTUALSCREEN)


def _screen_to_absolute(x: int, y: int) -> tuple[int, int]:
    """Convert screen pixel coords to MOUSEEVENTF_ABSOLUTE 0..65535 normalized range,
    accounting for multi-monitor virtual screen (incl. negative-origin)."""
    vw, vh = _virtual_screen_size()
    vx, vy = _virtual_screen_origin()
    nx = int((x - vx) * 65535 / max(vw - 1, 1))
    ny = int((y - vy) * 65535 / max(vh - 1, 1))
    return nx, ny


def send_mouse_click(
    screen_x: int,
    screen_y: int,
    button: str = "left",
    clicks: int = 1,
) -> bool:
    flags_down = {
        "left": MOUSEEVENTF_LEFTDOWN,
        "right": MOUSEEVENTF_RIGHTDOWN,
        "middle": MOUSEEVENTF_MIDDLEDOWN,
    }[button]
    flags_up = {
        "left": MOUSEEVENTF_LEFTUP,
        "right": MOUSEEVENTF_RIGHTUP,
        "middle": MOUSEEVENTF_MIDDLEUP,
    }[button]

    ax, ay = _screen_to_absolute(screen_x, screen_y)

    inputs = []
    # Move
    inputs.append(INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(
        dx=ax, dy=ay, mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
        time=0, dwExtraInfo=None
    ))))
    # Click(s)
    for _ in range(clicks):
        inputs.append(INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(
            dx=ax, dy=ay, mouseData=0,
            dwFlags=flags_down | MOUSEEVENTF_ABSOLUTE,
            time=0, dwExtraInfo=None
        ))))
        inputs.append(INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(
            dx=ax, dy=ay, mouseData=0,
            dwFlags=flags_up | MOUSEEVENTF_ABSOLUTE,
            time=0, dwExtraInfo=None
        ))))

    sent = _send_inputs(inputs)
    return sent == len(inputs)


def send_mouse_drag(
    from_screen: tuple[int, int],
    to_screen: tuple[int, int],
    button: str = "left",
    steps: int = 10,
) -> bool:
    flags_down = {"left": MOUSEEVENTF_LEFTDOWN, "right": MOUSEEVENTF_RIGHTDOWN}[button]
    flags_up = {"left": MOUSEEVENTF_LEFTUP, "right": MOUSEEVENTF_RIGHTUP}[button]

    fx, fy = from_screen
    tx, ty = to_screen

    inputs = []
    ax, ay = _screen_to_absolute(fx, fy)
    inputs.append(INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(
        dx=ax, dy=ay, mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
        time=0, dwExtraInfo=None
    ))))
    inputs.append(INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(
        dx=ax, dy=ay, mouseData=0,
        dwFlags=flags_down | MOUSEEVENTF_ABSOLUTE,
        time=0, dwExtraInfo=None
    ))))
    for i in range(1, steps + 1):
        ix = fx + (tx - fx) * i // steps
        iy = fy + (ty - fy) * i // steps
        ax, ay = _screen_to_absolute(ix, iy)
        inputs.append(INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(
            dx=ax, dy=ay, mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
            time=0, dwExtraInfo=None
        ))))
    ax, ay = _screen_to_absolute(tx, ty)
    inputs.append(INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(
        dx=ax, dy=ay, mouseData=0,
        dwFlags=flags_up | MOUSEEVENTF_ABSOLUTE,
        time=0, dwExtraInfo=None
    ))))

    sent = _send_inputs(inputs)
    return sent == len(inputs)


def send_scroll(screen_x: int, screen_y: int, delta: int) -> bool:
    """delta > 0 scrolls up, < 0 scrolls down. 120 = one notch."""
    ax, ay = _screen_to_absolute(screen_x, screen_y)
    inputs = [
        INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(
            dx=ax, dy=ay, mouseData=0,
            dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
            time=0, dwExtraInfo=None
        ))),
        INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(
            dx=ax, dy=ay, mouseData=delta,
            dwFlags=MOUSEEVENTF_WHEEL | MOUSEEVENTF_ABSOLUTE,
            time=0, dwExtraInfo=None
        ))),
    ]
    sent = _send_inputs(inputs)
    return sent == len(inputs)


def send_keys(keys: str) -> bool:
    """Send a sequence of keys. `keys` is a string with named keys in {braces}
    like "{enter}", "{esc}", "{f1}", or literal characters.

    Examples:
      "abc"           → types a, b, c as Unicode
      "{enter}"       → press Return
      "Hello{enter}"  → type Hello then press Return
    """
    inputs = []
    i = 0
    while i < len(keys):
        ch = keys[i]
        if ch == "{":
            end = keys.find("}", i)
            if end == -1:
                # Unmatched brace, treat as literal
                inputs.extend(_unicode_input(ch))
                i += 1
                continue
            name = keys[i+1:end].lower()
            vk = KEY_NAME_TO_VK.get(name)
            if vk is not None:
                inputs.extend(_vk_input(vk))
            else:
                # Unknown named key, type literally
                for c in keys[i:end+1]:
                    inputs.extend(_unicode_input(c))
            i = end + 1
        else:
            inputs.extend(_unicode_input(ch))
            i += 1

    if not inputs:
        return True
    sent = _send_inputs(inputs)
    return sent == len(inputs)


def _keystroke_input(stroke: KeyStroke, key_up: bool) -> INPUT:
    flags = KEYEVENTF_KEYUP if key_up else 0
    if stroke.scan_code is not None:
        flags |= KEYEVENTF_SCANCODE
        if stroke.extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        return INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=KEYBDINPUT(
            wVk=0,
            wScan=stroke.scan_code,
            dwFlags=flags,
            time=0,
            dwExtraInfo=None
        )))
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=KEYBDINPUT(
        wVk=stroke.vk or 0,
        wScan=0,
        dwFlags=flags,
        time=0,
        dwExtraInfo=None
    )))


def _button_input(spec, down: bool) -> INPUT:
    return INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(
        dx=0, dy=0,
        mouseData=spec.mouse_data,
        dwFlags=spec.down_flag if down else spec.up_flag,
        time=0, dwExtraInfo=None
    )))


def build_inputs(edges) -> list[INPUT]:
    """INPUT records for a mixed edge list (game_input_mcp.input.state.Edge):
    key and button edges, relative mouse moves and wheel notches."""
    inputs: list[INPUT] = []
    for edge in edges:
        if edge.kind == "key":
            inputs.append(_keystroke_input(edge.stroke, key_up=not edge.down))
        elif edge.kind == "button":
            inputs.append(_button_input(edge.button, edge.down))
        elif edge.kind == "move":
            inputs.append(INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(
                dx=int(edge.dx), dy=int(edge.dy), mouseData=0,
                dwFlags=MOUSEEVENTF_MOVE,   # relative counts, like a physical mouse
                time=0, dwExtraInfo=None
            ))))
        elif edge.kind == "wheel":
            inputs.append(INPUT(type=INPUT_MOUSE, u=_INPUTunion(mi=MOUSEINPUT(
                dx=0, dy=0, mouseData=int(edge.delta) & 0xFFFFFFFF,
                dwFlags=MOUSEEVENTF_WHEEL,
                time=0, dwExtraInfo=None
            ))))
        else:
            raise ValueError(f"unknown edge kind: {edge.kind}")
    return inputs


def send_edges(edges) -> int:
    """Inject a mixed edge list as ONE SendInput call so the target sees every
    edge in the same input burst. Returns the number of events the OS accepted."""
    inputs = build_inputs(edges)
    if not inputs:
        return 0
    return _send_inputs(inputs)


def send_key_down(key: str, mode: str = "scancode") -> int:
    stroke = resolve_key(key, mode)
    return _send_inputs([_keystroke_input(stroke, key_up=False)])


def send_key_up(key: str, mode: str = "scancode") -> int:
    stroke = resolve_key(key, mode)
    return _send_inputs([_keystroke_input(stroke, key_up=True)])


def tap_key(key: str, mode: str = "scancode", hold_ms: int = 30) -> int:
    stroke = resolve_key(key, mode)
    sent = _send_inputs([_keystroke_input(stroke, key_up=False)])
    if hold_ms > 0:
        time.sleep(hold_ms / 1000.0)
    sent += _send_inputs([_keystroke_input(stroke, key_up=True)])
    return sent


def _vk_input(vk: int) -> list[INPUT]:
    return [
        INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=KEYBDINPUT(
            wVk=vk, wScan=0, dwFlags=0, time=0, dwExtraInfo=None
        ))),
        INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=KEYBDINPUT(
            wVk=vk, wScan=0, dwFlags=KEYEVENTF_KEYUP, time=0, dwExtraInfo=None
        ))),
    ]


def _unicode_input(ch: str) -> list[INPUT]:
    code = ord(ch)
    return [
        INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=KEYBDINPUT(
            wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=None
        ))),
        INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=KEYBDINPUT(
            wVk=0, wScan=code, dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, time=0, dwExtraInfo=None
        ))),
    ]
