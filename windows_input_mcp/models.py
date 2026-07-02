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

    @classmethod
    def from_value(cls, value: int | dict[str, Any] | "TargetSpec") -> "TargetSpec":
        if isinstance(value, TargetSpec):
            spec = value
        elif isinstance(value, int):
            spec = cls(pid=value)
        elif isinstance(value, dict):
            pid = value.get("pid")
            hwnd = value.get("hwnd")
            spec = cls(
                pid=int(pid) if pid is not None else None,
                hwnd=int(hwnd) if hwnd is not None else None,
            )
        else:
            raise ValueError("target must be a pid integer or object with pid or hwnd")
        if spec.pid is None and spec.hwnd is None:
            raise ValueError("target must include pid or hwnd")
        return spec

    def to_dict(self) -> dict[str, int]:
        out: dict[str, int] = {}
        if self.pid is not None:
            out["pid"] = self.pid
        if self.hwnd is not None:
            out["hwnd"] = self.hwnd
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
        }


def ok_response(**fields: Any) -> dict[str, Any]:
    return {"success": True, **fields}


def error_response(
    error_code: str,
    message: str,
    *,
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
