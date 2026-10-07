from __future__ import annotations

import pytest
from PIL import Image

from game_input_mcp.capture import diagnostics
from game_input_mcp.models import Rect, TargetInfo

RECT = Rect(100, 100, 300, 200)


def _target(hwnd: int = 10, pid: int = 7) -> TargetInfo:
    return TargetInfo(
        hwnd=hwnd,
        pid=pid,
        title="Game",
        window_rect=RECT,
        client_rect_screen=RECT,
        client_size=(200, 100),
        client_screen_origin=(100, 100),
        dpi=96,
        is_foreground=True,
    )


def test_sample_points_cover_corners_edges_and_centre_inside_the_rect() -> None:
    points = diagnostics.sample_points(RECT)

    assert len(points) == 9 and len(set(points)) == 9
    assert (100, 100) in points and (299, 199) in points and (199, 149) in points
    assert all(RECT.left <= x < RECT.right and RECT.top <= y < RECT.bottom for x, y in points)
    assert diagnostics.sample_points(Rect(5, 5, 5, 9)) == []


@pytest.fixture
def desktop(monkeypatch):
    state = {"at": {}, "default": 10}
    monkeypatch.setattr(diagnostics.win32, "root_window", lambda hwnd: hwnd)
    monkeypatch.setattr(diagnostics.win32, "root_window_at", lambda x, y: state["at"].get((x, y), state["default"]))
    monkeypatch.setattr(
        diagnostics.win32,
        "describe_window",
        lambda hwnd: {"hwnd": hwnd, "pid": {20: 99, 21: 7}.get(hwnd, 50), "exe": "x.exe", "title": f"w{hwnd}"},
    )
    return state


def test_clear_target_has_no_occluders(desktop) -> None:
    assert diagnostics.probe_visibility(_target(), RECT) == {"occluders": [], "offscreen": False}


def test_foreign_window_over_part_of_the_target_is_reported_once(desktop) -> None:
    desktop["at"][(100, 100)] = 20
    desktop["at"][(299, 199)] = 20

    result = diagnostics.probe_visibility(_target(), RECT)

    assert [w["hwnd"] for w in result["occluders"]] == [20]
    assert result["occluders"][0]["exe"] == "x.exe"
    assert result["offscreen"] is False


def test_windows_of_the_same_process_are_not_occluders(desktop) -> None:
    desktop["at"][(100, 100)] = 21  # pid 7, the target's own popup

    assert diagnostics.probe_visibility(_target(pid=7), RECT)["occluders"] == []


def test_points_with_no_window_mean_off_screen(desktop) -> None:
    desktop["at"][(299, 199)] = 0

    result = diagnostics.probe_visibility(_target(), RECT)

    assert result["offscreen"] is True and result["occluders"] == []


def test_within_one_monitor(monkeypatch) -> None:
    monitors = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 3840, 1080)]
    monkeypatch.setattr(diagnostics.win32, "get_monitor_rects", lambda: monitors)

    assert diagnostics.within_one_monitor(Rect(100, 100, 500, 400))
    assert diagnostics.within_one_monitor(Rect(2000, 0, 3000, 500))
    assert not diagnostics.within_one_monitor(Rect(1800, 100, 2100, 400))
    assert not diagnostics.within_one_monitor(Rect(-50, 0, 100, 100))


@pytest.mark.parametrize(
    ("color", "black"),
    [((0, 0, 0), True), ((7, 7, 7), True), ((8, 0, 0), False), ((0, 0, 200), False), ((255, 255, 255), False)],
)
def test_black_detection(color, black) -> None:
    assert diagnostics.is_black(Image.new("RGB", (64, 36), color)) is black


def test_a_dark_scene_with_one_bright_area_is_not_black() -> None:
    image = Image.new("RGB", (320, 180), (0, 0, 0))
    image.paste((255, 255, 255), (0, 0, 160, 180))

    assert diagnostics.is_black(image) is False
