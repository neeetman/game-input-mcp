from __future__ import annotations

import pytest

from game_input_mcp import guard
from game_input_mcp.geometry import FrameGeometry
from game_input_mcp.models import Rect, TargetInfo

TARGET = 10


@pytest.fixture
def world(monkeypatch):
    state = {"foreground": TARGET, "root_at": {}, "described": []}
    monkeypatch.setattr(guard.win32, "get_foreground_hwnd", lambda: state["foreground"])
    monkeypatch.setattr(guard.win32, "root_window_at", lambda x, y: state["root_at"].get((x, y), 0))
    monkeypatch.setattr(guard.win32, "root_window", lambda hwnd: hwnd)
    monkeypatch.setattr(
        guard.win32,
        "describe_window",
        lambda hwnd: {"hwnd": hwnd, "pid": 7, "exe": "other.exe", "title": "Other"},
    )
    return state


@pytest.mark.parametrize("kind", ["keyboard", "mouse_rel", "mouse_abs"])
def test_target_foreground_passes_every_kind(world, kind) -> None:
    outcome = guard.gate_foreground(TARGET, kind, point=(1, 1))

    assert outcome.ok and outcome.warnings == [] and outcome.guard is None


@pytest.mark.parametrize("kind", ["keyboard", "mouse_rel", "mouse_abs"])
def test_other_foreground_refuses_every_kind(world, kind) -> None:
    world["foreground"] = 99
    world["root_at"][(5, 5)] = TARGET  # pointer over the target does not rescue it

    outcome = guard.gate_foreground(TARGET, kind, point=(5, 5))

    assert not outcome.ok
    assert outcome.error["error_code"] == "TARGET_NOT_FOREGROUND"
    assert outcome.error["retryable"] is True
    assert outcome.error["details"]["foreground"]["exe"] == "other.exe"
    assert outcome.error["details"]["target_hwnd"] == TARGET


def test_no_foreground_refuses_keyboard_and_relative_mouse(world) -> None:
    world["foreground"] = 0
    world["root_at"][(5, 5)] = TARGET

    for kind in ("keyboard", "mouse_rel"):
        assert not guard.gate_foreground(TARGET, kind, point=(5, 5)).ok


def test_no_foreground_allows_absolute_mouse_only_over_target(world) -> None:
    world["foreground"] = 0
    world["root_at"][(5, 5)] = TARGET
    world["root_at"][(6, 6)] = 55

    over = guard.gate_foreground(TARGET, "mouse_abs", point=(5, 5))
    elsewhere = guard.gate_foreground(TARGET, "mouse_abs", point=(6, 6))
    nowhere = guard.gate_foreground(TARGET, "mouse_abs", point=(7, 7))
    no_point = guard.gate_foreground(TARGET, "mouse_abs")

    assert over.ok and over.guard == {"exception": "no_foreground_pointer_over_target"}
    assert not elsewhere.ok and not nowhere.ok and not no_point.ok


def test_warn_mode_sends_but_annotates(world) -> None:
    world["foreground"] = 99

    outcome = guard.gate_foreground(TARGET, "keyboard", mode="warn")

    assert outcome.ok
    assert outcome.warnings[0]["code"] == "TARGET_NOT_FOREGROUND"


def test_off_mode_skips_everything(world) -> None:
    world["foreground"] = 99

    outcome = guard.gate_foreground(TARGET, "keyboard", mode="off")

    assert outcome.ok and outcome.warnings == []


def test_unknown_kind_is_a_programming_error(world) -> None:
    with pytest.raises(ValueError):
        guard.gate_foreground(TARGET, "gamepad")


def _target(rect: Rect, dpi: int = 96) -> TargetInfo:
    return TargetInfo(
        hwnd=TARGET,
        pid=2,
        title="Game",
        window_rect=rect,
        client_rect_screen=rect,
        client_size=(rect.width, rect.height),
        client_screen_origin=(rect.left, rect.top),
        dpi=dpi,
        is_foreground=True,
    )


def _frame(rect: Rect) -> FrameGeometry:
    return FrameGeometry(image_size=(100, 100), capture_rect_screen=rect, client_rect_screen=rect)


def test_frame_geometry_unchanged_passes() -> None:
    rect = Rect(100, 100, 300, 200)

    assert guard.check_frame_geometry(_frame(rect), 96, _target(rect)).ok


def test_frame_geometry_moved_window_is_refused_with_both_rects() -> None:
    outcome = guard.check_frame_geometry(
        _frame(Rect(100, 100, 300, 200)), 96, _target(Rect(110, 100, 310, 200))
    )

    assert not outcome.ok
    assert outcome.error["error_code"] == "FRAME_GEOMETRY_CHANGED"
    assert outcome.error["retryable"] is True
    assert outcome.error["details"]["frame_client_rect"] == [100, 100, 300, 200]
    assert outcome.error["details"]["current_client_rect"] == [110, 100, 310, 200]


def test_frame_geometry_dpi_change_is_refused_but_missing_dpi_is_ignored() -> None:
    rect = Rect(0, 0, 100, 100)

    assert not guard.check_frame_geometry(_frame(rect), 96, _target(rect, dpi=144)).ok
    assert guard.check_frame_geometry(_frame(rect), None, _target(rect, dpi=144)).ok


def test_frame_geometry_warn_and_off_modes() -> None:
    old, new = Rect(0, 0, 100, 100), Rect(5, 5, 105, 105)

    warned = guard.check_frame_geometry(_frame(old), 96, _target(new), mode="warn")
    off = guard.check_frame_geometry(_frame(old), 96, _target(new), mode="off")
    no_frame = guard.check_frame_geometry(None, None, _target(new))

    assert warned.ok and warned.warnings[0]["code"] == "FRAME_GEOMETRY_CHANGED"
    assert off.ok and off.warnings == []
    assert no_frame.ok


def test_merge_keeps_first_error_and_accumulates_warnings() -> None:
    first = guard.Outcome(error={"error_code": "A"}, warnings=[{"code": "w1"}])
    second = guard.Outcome(error={"error_code": "B"}, warnings=[{"code": "w2"}], guard={"exception": "x"})

    merged = guard.merge(first, second)

    assert merged.error == {"error_code": "A"}
    assert [w["code"] for w in merged.warnings] == ["w1", "w2"]
    assert merged.guard == {"exception": "x"}


def test_annotate_only_adds_keys_when_there_is_something_to_say() -> None:
    assert guard.annotate({"success": True}, guard.Outcome()) == {"success": True}
    annotated = guard.annotate({"success": True}, guard.Outcome(warnings=[{"code": "w"}], guard={"exception": "x"}))
    assert annotated == {"success": True, "warnings": [{"code": "w"}], "guard": {"exception": "x"}}
