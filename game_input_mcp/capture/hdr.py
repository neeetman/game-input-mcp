"""Read-only HDR probe: can a capture of this window come out washed out?

With an HDR display, Windows Auto HDR (or a native HDR game) changes what the
compositor hands to a capture: an SDR game comes out 2-3x brighter and shifted
(observed by the universal-modder project). Two inputs decide it:

* whether the display the window is on is in HDR mode, from
  ``DisplayConfigGetDeviceInfo(GET_ADVANCED_COLOR_INFO)`` (no registry);
* whether Auto HDR is on for this exe or globally, from
  ``HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences``.

The registry access is ``KEY_READ`` on that one fixed key and is never exposed
as a tool; it is the only registry touch in this project. Nothing is fixed or
tone-mapped here: callers only get a warning.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Callable

from game_input_mcp.models import Rect

GPU_PREFS_KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"
GLOBAL_PREFS_NAME = "DirectXUserGlobalSettings"
CACHE_TTL_S = 5.0


# -- Auto HDR flag ----------------------------------------------------------------

def _flag(data: str) -> bool | None:
    """AutoHDREnable from "AppStatus=1;AutoHDREnable=2097;": odd = on (2097 on,
    2096 off). This parity rule is universal-modder's observation, not a
    documented contract."""
    pairs = dict(kv.split("=", 1) for kv in data.split(";") if "=" in kv)
    value = pairs.get("AutoHDREnable", "")
    return int(value) % 2 == 1 if value.isdigit() else None


def auto_hdr_for_exe(prefs: dict[str, str], exe: str | None) -> tuple[bool | None, str | None]:
    """(enabled, scope). The exe's own setting wins over the global one."""
    if exe:
        name = (exe if exe.lower().endswith(".exe") else exe + ".exe").lower()
        for path, data in prefs.items():
            if path.replace("/", "\\").lower().rsplit("\\", 1)[-1] == name:
                flag = _flag(data)
                if flag is not None:
                    return flag, "exe"
    flag = _flag(prefs.get(GLOBAL_PREFS_NAME, ""))
    return (flag, "global") if flag is not None else (None, None)


def read_gpu_prefs() -> dict[str, str]:
    """exe path (or DirectXUserGlobalSettings) -> preference string. Empty when
    the key does not exist or cannot be read."""
    out: dict[str, str] = {}
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, GPU_PREFS_KEY, 0, winreg.KEY_READ) as key:
            index = 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                out[name] = str(data)
                index += 1
    except (OSError, ImportError):
        pass
    return out


# -- display HDR state ------------------------------------------------------------

class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", wintypes.UINT), ("Denominator", wintypes.UINT)]


class _PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", _LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("statusFlags", wintypes.UINT),
    ]


class _PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", _LUID),
        ("id", wintypes.UINT),
        ("modeInfoIdx", wintypes.UINT),
        ("outputTechnology", wintypes.UINT),
        ("rotation", wintypes.UINT),
        ("scaling", wintypes.UINT),
        ("refreshRate", _RATIONAL),
        ("scanLineOrdering", wintypes.UINT),
        ("targetAvailable", wintypes.BOOL),
        ("statusFlags", wintypes.UINT),
    ]


class _PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", _PATH_SOURCE_INFO), ("targetInfo", _PATH_TARGET_INFO), ("flags", wintypes.UINT)]


class _SOURCE_MODE(ctypes.Structure):
    _fields_ = [
        ("width", wintypes.UINT),
        ("height", wintypes.UINT),
        ("pixelFormat", wintypes.UINT),
        ("positionX", wintypes.LONG),
        ("positionY", wintypes.LONG),
    ]


class _MODE_UNION(ctypes.Union):
    _fields_ = [("sourceMode", _SOURCE_MODE), ("padding", ctypes.c_ubyte * 48)]


class _MODE_INFO(ctypes.Structure):
    _fields_ = [("infoType", wintypes.UINT), ("id", wintypes.UINT), ("adapterId", _LUID), ("u", _MODE_UNION)]


class _DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", wintypes.UINT), ("size", wintypes.UINT), ("adapterId", _LUID), ("id", wintypes.UINT)]


class _ADVANCED_COLOR_INFO(ctypes.Structure):
    _fields_ = [
        ("header", _DEVICE_INFO_HEADER),
        ("value", wintypes.UINT),  # bit0 supported, bit1 enabled, bit2 wide-colour enforced, bit3 force-disabled
        ("colorEncoding", wintypes.UINT),
        ("bitsPerColorChannel", wintypes.UINT),
    ]


QDC_ONLY_ACTIVE_PATHS = 2
MODE_INFO_TYPE_SOURCE = 1
GET_ADVANCED_COLOR_INFO = 9


def query_display_paths() -> list[dict]:
    """[{"x", "y", "supported", "enabled"}] per active display path, where x/y is
    the desktop position of the display's source mode. Empty on any failure."""
    try:
        user32 = ctypes.WinDLL("user32")
        n_paths, n_modes = wintypes.UINT(), wintypes.UINT()
        if user32.GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), ctypes.byref(n_modes)) != 0:
            return []
        paths = (_PATH_INFO * n_paths.value)()
        modes = (_MODE_INFO * n_modes.value)()
        if user32.QueryDisplayConfig(
            QDC_ONLY_ACTIVE_PATHS, ctypes.byref(n_paths), paths, ctypes.byref(n_modes), modes, None
        ) != 0:
            return []
        out: list[dict] = []
        for index in range(n_paths.value):
            path = paths[index]
            mode_index = path.sourceInfo.modeInfoIdx
            if mode_index >= n_modes.value or modes[mode_index].infoType != MODE_INFO_TYPE_SOURCE:
                continue
            info = _ADVANCED_COLOR_INFO()
            info.header.type = GET_ADVANCED_COLOR_INFO
            info.header.size = ctypes.sizeof(info)
            info.header.adapterId = path.targetInfo.adapterId
            info.header.id = path.targetInfo.id
            if user32.DisplayConfigGetDeviceInfo(ctypes.byref(info)) != 0:
                continue
            source = modes[mode_index].u.sourceMode
            out.append(
                {
                    "x": int(source.positionX),
                    "y": int(source.positionY),
                    "supported": bool(info.value & 0x1),
                    "enabled": bool(info.value & 0x2),
                }
            )
        return out
    except (OSError, AttributeError):
        return []


def display_hdr_for(rect: Rect, monitors: list[Rect], paths: list[dict]) -> bool | None:
    """Is the display under the centre of ``rect`` in HDR mode? None = unknown."""
    cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
    monitor = next((m for m in monitors if m.left <= cx < m.right and m.top <= cy < m.bottom), None)
    if monitor is None:
        return None
    for path in paths:
        if (path["x"], path["y"]) == (monitor.left, monitor.top):
            return bool(path["enabled"])
    return None


# -- the probe --------------------------------------------------------------------

class HdrProbe:
    """Combines both inputs; results are cached briefly because capture can be
    called in a tight loop and neither input changes that fast."""

    def __init__(
        self,
        *,
        read_prefs: Callable[[], dict[str, str]] = read_gpu_prefs,
        query_paths: Callable[[], list[dict]] = query_display_paths,
        monitors: Callable[[], list[Rect]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read_prefs = read_prefs
        self._query_paths = query_paths
        self._monitors = monitors
        self._clock = clock
        self._cache: dict[str, tuple[float, object]] = {}

    def _cached(self, key: str, compute: Callable[[], object]):
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL_S:
            return hit[1]
        value = compute()
        self._cache[key] = (now, value)
        return value

    def _monitor_rects(self) -> list[Rect]:
        if self._monitors is not None:
            return self._monitors()
        from game_input_mcp import win32

        return win32.get_monitor_rects()

    def inspect(self, rect: Rect, exe: str | None) -> dict:
        """{"display_hdr": bool|None, "auto_hdr": bool|None, "auto_hdr_scope": ...}"""
        paths = self._cached("paths", self._query_paths)
        prefs = self._cached("prefs", self._read_prefs)
        auto_hdr, scope = auto_hdr_for_exe(prefs, exe)
        return {
            "display_hdr": display_hdr_for(rect, self._monitor_rects(), paths),
            "auto_hdr": auto_hdr,
            "auto_hdr_scope": scope,
        }


def warning_for(info: dict, exe: str | None) -> dict | None:
    """HDR_COLOR_SHIFT_POSSIBLE when the display is HDR, or when its state is
    unknown but Auto HDR is on. An SDR display never warns."""
    display, auto = info["display_hdr"], info["auto_hdr"]
    if display is True or (display is None and auto is True):
        return {
            "code": "HDR_COLOR_SHIFT_POSSIBLE",
            "message": (
                "The display is in HDR mode"
                + (" and Auto HDR is on for this game" if auto else "")
                + ": captured colours may be brighter and shifted. Turn HDR / Auto HDR off for the game while capturing."
                if display
                else "Windows Auto HDR is on and the display's HDR state is unknown: captured colours may be shifted."
            ),
            "details": {**info, "exe": exe},
        }
    return None
