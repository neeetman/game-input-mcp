from __future__ import annotations

from typing import Any

from . import win32
from .models import Rect, TargetInfo, TargetSpec


def from_win32_info(info: Any) -> TargetInfo:
    window_rect = Rect.from_list(list(info.window_rect))
    cx, cy = info.client_screen_origin
    cw, ch = info.client_size
    client_rect = Rect(cx, cy, cx + cw, cy + ch)
    return TargetInfo(
        hwnd=int(info.hwnd),
        pid=int(info.pid),
        title=str(info.title),
        window_rect=window_rect,
        client_rect_screen=client_rect,
        client_size=(int(cw), int(ch)),
        client_screen_origin=(int(cx), int(cy)),
        dpi=int(info.dpi),
        is_foreground=bool(info.is_foreground),
        is_minimized=bool(getattr(info, "is_minimized", False)),
    )


def resolve_target(target: int | dict[str, Any] | TargetSpec) -> TargetInfo | None:
    spec = TargetSpec.from_value(target)
    info = None
    if spec.hwnd is not None:
        info = win32.get_window_info_by_hwnd(spec.hwnd)
    if info is None and spec.pid is not None:
        info = win32.get_window_info(spec.pid)
    return from_win32_info(info) if info is not None else None


def list_targets() -> list[TargetInfo]:
    out: list[TargetInfo] = []
    for info in win32.list_window_infos():
        target = from_win32_info(info)
        if target.client_size[0] > 0 and target.client_size[1] > 0:
            out.append(target)
    out.sort(key=lambda item: item.client_size[0] * item.client_size[1], reverse=True)
    return out
