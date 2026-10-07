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
        exe=getattr(info, "exe", None),
        monitor=getattr(info, "monitor", None),
    )


class TargetAmbiguous(Exception):
    """An exe/title target matched windows of more than one process."""

    def __init__(self, candidates: list[dict[str, Any]]) -> None:
        super().__init__(f"target matches {len(candidates)} processes")
        self.candidates = candidates


MAX_CANDIDATES = 10


def _exe_matches(exe: str | None, wanted: str) -> bool:
    if not exe:
        return False
    normalize = lambda name: name.lower().removesuffix(".exe")  # noqa: E731
    return normalize(exe) == normalize(wanted)


def _find_by_exe_or_title(spec: TargetSpec) -> Any | None:
    """The window for an exe/title target. Several windows of ONE process are not
    ambiguous (the largest client area wins, as for a pid target); windows of
    several processes are: TargetAmbiguous lists them."""
    wanted_title = spec.title.lower() if spec.title else None
    best: dict[int, Any] = {}  # pid -> window with the largest client area
    for info in win32.list_window_infos():
        if spec.exe and not _exe_matches(getattr(info, "exe", None), spec.exe):
            continue
        if wanted_title and wanted_title not in str(info.title).lower():
            continue
        area = info.client_size[0] * info.client_size[1]
        current = best.get(info.pid)
        if current is None or area > current.client_size[0] * current.client_size[1]:
            best[info.pid] = info
    if not best:
        return None
    if len(best) > 1:
        raise TargetAmbiguous(
            [
                {"pid": i.pid, "hwnd": i.hwnd, "exe": getattr(i, "exe", None), "title": i.title}
                for i in sorted(best.values(), key=lambda i: i.pid)[:MAX_CANDIDATES]
            ]
        )
    return next(iter(best.values()))


def resolve_target(target: int | dict[str, Any] | TargetSpec) -> TargetInfo | None:
    """hwnd, then pid, then exe/title. Raises TargetAmbiguous when an exe/title
    matches more than one process, ValueError for a malformed target."""
    spec = TargetSpec.from_value(target)
    info = None
    if spec.hwnd is not None:
        info = win32.get_window_info_by_hwnd(spec.hwnd)
    if info is None and spec.pid is not None:
        info = win32.get_window_info(spec.pid)
    if info is None and spec.hwnd is None and spec.pid is None:
        info = _find_by_exe_or_title(spec)
    return from_win32_info(info) if info is not None else None


def list_targets() -> list[TargetInfo]:
    out: list[TargetInfo] = []
    for info in win32.list_window_infos():
        target = from_win32_info(info)
        if target.client_size[0] > 0 and target.client_size[1] > 0:
            out.append(target)
    out.sort(key=lambda item: item.client_size[0] * item.client_size[1], reverse=True)
    return out
