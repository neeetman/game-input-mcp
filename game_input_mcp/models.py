from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def to_list(self) -> list[int]:
        return [self.left, self.top, self.right, self.bottom]

    @classmethod
    def from_list(cls, value: list[int] | tuple[int, int, int, int]) -> "Rect":
        if len(value) != 4:
            raise ValueError("rect must contain exactly four integers")
        return cls(int(value[0]), int(value[1]), int(value[2]), int(value[3]))


@dataclass(frozen=True)
class TargetSpec:
    pid: int | None = None
    hwnd: int | None = None
    exe: str | None = None      # process image name, case-insensitive, ".exe" optional
    title: str | None = None    # case-insensitive substring of the window title

    @classmethod
    def from_value(cls, value: int | dict[str, Any] | "TargetSpec") -> "TargetSpec":
        if isinstance(value, TargetSpec):
            spec = value
        elif isinstance(value, int):
            spec = cls(pid=value)
        elif isinstance(value, dict):
            pid = value.get("pid")
            hwnd = value.get("hwnd")
            exe = value.get("exe")
            title = value.get("title")
            spec = cls(
                pid=int(pid) if pid is not None else None,
                hwnd=int(hwnd) if hwnd is not None else None,
                exe=str(exe).strip() or None if exe is not None else None,
                title=str(title).strip() or None if title is not None else None,
            )
        else:
            raise ValueError("target must be a pid integer or an object with pid, hwnd, exe or title")
        if spec.pid is None and spec.hwnd is None and spec.exe is None and spec.title is None:
            raise ValueError("target must include pid, hwnd, exe or title")
        return spec

    def to_dict(self) -> dict[str, int | str]:
        out: dict[str, int | str] = {}
        if self.pid is not None:
            out["pid"] = self.pid
        if self.hwnd is not None:
            out["hwnd"] = self.hwnd
        if self.exe is not None:
            out["exe"] = self.exe
        if self.title is not None:
            out["title"] = self.title
        return out


@dataclass(frozen=True)
class TargetInfo:
    hwnd: int
    pid: int
    title: str
    window_rect: Rect
    client_rect_screen: Rect
    client_size: tuple[int, int]
    client_screen_origin: tuple[int, int]
    dpi: int
    is_foreground: bool
    is_minimized: bool = False
    exe: str | None = None
    monitor: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "pid": self.pid,
            "title": self.title,
            "window_rect": self.window_rect.to_list(),
            "client_rect_screen": self.client_rect_screen.to_list(),
            "client_size": list(self.client_size),
            "client_screen_origin": list(self.client_screen_origin),
            "dpi": self.dpi,
            "is_foreground": self.is_foreground,
            "is_minimized": self.is_minimized,
            "exe": self.exe,
            "monitor": self.monitor,
        }


def ok_response(**fields: Any) -> dict[str, Any]:
    return {"success": True, **fields}


def error_response(
    error_code: str,
    message: str,
    retryable: bool = False,
    **details: Any,
) -> dict[str, Any]:
    return {
        "success": False,
        "error_code": error_code,
        "message": message,
        "retryable": retryable,
        "details": details,
    }
