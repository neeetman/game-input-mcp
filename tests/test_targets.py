from __future__ import annotations

from dataclasses import dataclass

from windows_input_mcp import targets
from windows_input_mcp.models import Rect, TargetInfo


@dataclass
class FakeWindowInfo:
    hwnd: int
    pid: int
    title: str
    window_rect: tuple[int, int, int, int]
    client_size: tuple[int, int]
    client_screen_origin: tuple[int, int]
    dpi: int
    is_foreground: bool
    is_minimized: bool = False


def test_from_win32_info_builds_client_screen_rect() -> None:
    info = FakeWindowInfo(
        hwnd=10,
        pid=20,
        title="Game",
        window_rect=(90, 80, 500, 400),
        client_size=(320, 200),
        client_screen_origin=(100, 120),
        dpi=144,
        is_foreground=True,
    )

    target = targets.from_win32_info(info)

    assert target == TargetInfo(
        hwnd=10,
        pid=20,
        title="Game",
        window_rect=Rect(90, 80, 500, 400),
        client_rect_screen=Rect(100, 120, 420, 320),
        client_size=(320, 200),
        client_screen_origin=(100, 120),
        dpi=144,
        is_foreground=True,
        is_minimized=False,
    )


def test_resolve_target_prefers_hwnd(monkeypatch) -> None:
    by_hwnd = FakeWindowInfo(11, 22, "By hwnd", (0, 0, 20, 20), (10, 10), (1, 2), 96, False)
    by_pid = FakeWindowInfo(33, 44, "By pid", (0, 0, 40, 40), (20, 20), (3, 4), 96, False)
    monkeypatch.setattr(targets.win32, "get_window_info_by_hwnd", lambda hwnd: by_hwnd)
    monkeypatch.setattr(targets.win32, "get_window_info", lambda pid: by_pid)

    resolved = targets.resolve_target({"pid": 44, "hwnd": 11})

    assert resolved is not None
    assert resolved.hwnd == 11
    assert resolved.pid == 22


def test_list_targets_filters_zero_client_windows(monkeypatch) -> None:
    visible = FakeWindowInfo(1, 2, "Visible", (0, 0, 20, 20), (10, 10), (0, 0), 96, False)
    empty = FakeWindowInfo(3, 4, "Empty", (0, 0, 20, 20), (0, 0), (0, 0), 96, False)
    monkeypatch.setattr(targets.win32, "list_window_infos", lambda: [visible, empty])

    listed = targets.list_targets()

    assert [item.hwnd for item in listed] == [1]
