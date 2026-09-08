"""Key and mouse-button name resolution.

Scan-code mode covers the full US-layout scan code set 1 table (letters,
digits, punctuation, numpad, side-specific modifiers, navigation cluster). Any
name that is not in the static table but resolves to a virtual key — including
single characters looked up through the active keyboard layout — falls back to
``MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX)`` so every physical key stays
reachable without a table edit. The Win32 lookups are isolated behind two
module-level functions so tests can replace them.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KeyStroke:
    key: str
    vk: int | None = None
    scan_code: int | None = None
    extended: bool = False


@dataclass(frozen=True)
class ButtonSpec:
    name: str
    down_flag: int
    up_flag: int
    mouse_data: int = 0


# --- scan code set 1 (US layout) ---------------------------------------------

SCAN_CODES: dict[str, tuple[int, bool]] = {
    "esc": (0x01, False),
    "1": (0x02, False), "2": (0x03, False), "3": (0x04, False), "4": (0x05, False),
    "5": (0x06, False), "6": (0x07, False), "7": (0x08, False), "8": (0x09, False),
    "9": (0x0A, False), "0": (0x0B, False),
    "-": (0x0C, False), "=": (0x0D, False),
    "backspace": (0x0E, False),
    "tab": (0x0F, False),
    "q": (0x10, False), "w": (0x11, False), "e": (0x12, False), "r": (0x13, False),
    "t": (0x14, False), "y": (0x15, False), "u": (0x16, False), "i": (0x17, False),
    "o": (0x18, False), "p": (0x19, False),
    "[": (0x1A, False), "]": (0x1B, False),
    "enter": (0x1C, False),
    "lctrl": (0x1D, False),
    "a": (0x1E, False), "s": (0x1F, False), "d": (0x20, False), "f": (0x21, False),
    "g": (0x22, False), "h": (0x23, False), "j": (0x24, False), "k": (0x25, False),
    "l": (0x26, False),
    ";": (0x27, False), "'": (0x28, False), "`": (0x29, False),
    "lshift": (0x2A, False),
    "\\": (0x2B, False),
    "z": (0x2C, False), "x": (0x2D, False), "c": (0x2E, False), "v": (0x2F, False),
    "b": (0x30, False), "n": (0x31, False), "m": (0x32, False),
    ",": (0x33, False), ".": (0x34, False), "/": (0x35, False),
    "rshift": (0x36, False),
    "numpad_multiply": (0x37, False),
    "lalt": (0x38, False),
    "space": (0x39, False),
    "capslock": (0x3A, False),
    "numlock": (0x45, False),
    "scrolllock": (0x46, False),
    "numpad7": (0x47, False), "numpad8": (0x48, False), "numpad9": (0x49, False),
    "numpad_minus": (0x4A, False),
    "numpad4": (0x4B, False), "numpad5": (0x4C, False), "numpad6": (0x4D, False),
    "numpad_plus": (0x4E, False),
    "numpad1": (0x4F, False), "numpad2": (0x50, False), "numpad3": (0x51, False),
    "numpad0": (0x52, False),
    "numpad_decimal": (0x53, False),
    "f11": (0x57, False),
    "f12": (0x58, False),
    # Extended (E0-prefixed) keys.
    "numpad_enter": (0x1C, True),
    "rctrl": (0x1D, True),
    "numpad_divide": (0x35, True),
    "printscreen": (0x37, True),
    "ralt": (0x38, True),
    "home": (0x47, True),
    "up": (0x48, True),
    "pageup": (0x49, True),
    "left": (0x4B, True),
    "right": (0x4D, True),
    "end": (0x4F, True),
    "down": (0x50, True),
    "pagedown": (0x51, True),
    "insert": (0x52, True),
    "delete": (0x53, True),
    "lwin": (0x5B, True),
    "rwin": (0x5C, True),
    "apps": (0x5D, True),
}
for _index in range(1, 11):
    SCAN_CODES[f"f{_index}"] = (0x3A + _index, False)

ALIASES: dict[str, str] = {
    "escape": "esc",
    "return": "enter",
    "ctrl": "lctrl", "control": "lctrl",
    "shift": "lshift",
    "alt": "lalt",
    "win": "lwin", "meta": "lwin", "super": "lwin",
    "menu": "apps",
    "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "pgdown": "pagedown",
    "bksp": "backspace", "back": "backspace",
    "caps": "capslock",
    "prtsc": "printscreen", "printscr": "printscreen",
    "minus": "-", "equals": "=", "equal": "=",
    "lbracket": "[", "rbracket": "]",
    "semicolon": ";", "apostrophe": "'", "quote": "'", "grave": "`", "tilde": "`",
    "backslash": "\\", "comma": ",", "period": ".", "slash": "/",
    "kp_enter": "numpad_enter",
}
for _index in range(10):
    ALIASES[f"kp{_index}"] = f"numpad{_index}"

# --- virtual keys --------------------------------------------------------------

VIRTUAL_KEYS: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D, "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D, "delete": 0x2E,
    "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
    "numpad_multiply": 0x6A, "numpad_plus": 0x6B, "numpad_minus": 0x6D,
    "numpad_decimal": 0x6E, "numpad_divide": 0x6F,
    "numlock": 0x90, "scrolllock": 0x91,
    "lshift": 0xA0, "rshift": 0xA1,
    "lctrl": 0xA2, "rctrl": 0xA3,
    "lalt": 0xA4, "ralt": 0xA5,
    "-": 0xBD, "=": 0xBB, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE, ";": 0xBA,
}
for _index in range(1, 13):
    VIRTUAL_KEYS[f"f{_index}"] = 0x6F + _index
for _index in range(10):
    VIRTUAL_KEYS[f"numpad{_index}"] = 0x60 + _index
VIRTUAL_KEYS["numpad_enter"] = 0x0D

# --- mouse buttons ---------------------------------------------------------------

MOUSE_BUTTONS: dict[str, ButtonSpec] = {
    "left": ButtonSpec("left", 0x0002, 0x0004),
    "right": ButtonSpec("right", 0x0008, 0x0010),
    "middle": ButtonSpec("middle", 0x0020, 0x0040),
    "x1": ButtonSpec("x1", 0x0080, 0x0100, 1),
    "x2": ButtonSpec("x2", 0x0080, 0x0100, 2),
}


# --- Win32 fallbacks (replaceable in tests) --------------------------------------

def _map_vk_to_scancode(vk: int) -> tuple[int, bool] | None:
    """MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX): scan code + extended flag."""
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        result = int(user32.MapVirtualKeyW(vk, 4))  # MAPVK_VK_TO_VSC_EX
    except (OSError, AttributeError):
        return None
    if result == 0:
        return None
    return result & 0xFF, (result >> 8) == 0xE0


def _char_to_vk(ch: str) -> int | None:
    """VkKeyScanW for one character under the active layout (no modifier info)."""
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        result = ctypes.c_short(user32.VkKeyScanW(ord(ch))).value
    except (OSError, AttributeError):
        return None
    if result == -1:
        return None
    return result & 0xFF


# --- resolution --------------------------------------------------------------------

def normalize_key_name(key: str) -> str:
    normalized = key.strip().lower() if len(key.strip()) > 1 else key.strip()
    if len(normalized) == 1 and normalized.isalpha():
        normalized = normalized.lower()
    return ALIASES.get(normalized, normalized)


def _resolve_vk(normalized: str) -> int | None:
    vk = VIRTUAL_KEYS.get(normalized)
    if vk is None and len(normalized) == 1:
        if normalized.isascii() and normalized.isalnum():
            vk = ord(normalized.upper())
        else:
            vk = _char_to_vk(normalized)
    return vk


def resolve_key(key: str, mode: str = "scancode") -> KeyStroke:
    normalized = normalize_key_name(key)
    if mode == "scancode":
        item = SCAN_CODES.get(normalized)
        if item is None:
            vk = _resolve_vk(normalized)
            item = _map_vk_to_scancode(vk) if vk is not None else None
        if item is None:
            raise ValueError(f"unsupported key for scancode mode: {key}")
        scan_code, extended = item
        return KeyStroke(key=normalized, scan_code=scan_code, extended=extended)
    if mode == "vk":
        vk = _resolve_vk(normalized)
        if vk is None:
            raise ValueError(f"unsupported key for vk mode: {key}")
        return KeyStroke(key=normalized, vk=vk)
    raise ValueError("mode must be scancode or vk")


def resolve_button(name: str) -> ButtonSpec:
    spec = MOUSE_BUTTONS.get(name.strip().lower())
    if spec is None:
        raise ValueError(f"unsupported mouse button: {name}")
    return spec
