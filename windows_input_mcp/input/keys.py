from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KeyStroke:
    key: str
    vk: int | None = None
    scan_code: int | None = None
    extended: bool = False


SCAN_CODES = {
    "w": (0x11, False),
    "a": (0x1E, False),
    "s": (0x1F, False),
    "d": (0x20, False),
    "space": (0x39, False),
    "shift": (0x2A, False),
    "ctrl": (0x1D, False),
    "alt": (0x38, False),
    "enter": (0x1C, False),
    "esc": (0x01, False),
    "escape": (0x01, False),
    "tab": (0x0F, False),
    "left": (0x4B, True),
    "right": (0x4D, True),
    "up": (0x48, True),
    "down": (0x50, True),
}
for index in range(1, 13):
    SCAN_CODES[f"f{index}"] = (0x3A + index, False)

VIRTUAL_KEYS = {
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "tab": 0x09,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
}
for index in range(1, 13):
    VIRTUAL_KEYS[f"f{index}"] = 0x6F + index


def resolve_key(key: str, mode: str = "scancode") -> KeyStroke:
    normalized = key.strip().lower()
    if mode == "scancode":
        item = SCAN_CODES.get(normalized)
        if item is None:
            raise ValueError(f"unsupported key for scancode mode: {key}")
        scan_code, extended = item
        return KeyStroke(key=normalized, scan_code=scan_code, extended=extended)
    if mode == "vk":
        vk = VIRTUAL_KEYS.get(normalized)
        if vk is None and len(normalized) == 1:
            vk = ord(normalized.upper())
        if vk is None:
            raise ValueError(f"unsupported key for vk mode: {key}")
        return KeyStroke(key=normalized, vk=vk)
    raise ValueError("mode must be scancode or vk")
