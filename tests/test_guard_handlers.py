"""One-shot input handlers fail closed (Phase 1 of the 2026-10-07 spec)."""
from __future__ import annotations

import dataclasses

import pytest

from game_input_mcp import config as config_module
from game_input_mcp import daemon
from game_input_mcp.models import Rect, TargetInfo


def _target() -> TargetInfo:
    return TargetInfo(
        hwnd=1,
        pid=2,
        title="Game",
        window_rect=Rect(90, 80, 500, 400),
        client_rect_screen=Rect(100, 120, 420, 320),
        client_size=(320, 200),
        client_screen_origin=(100, 120),
        dpi=144,
        is_foreground=True,
    )


def _frame_record(client=(100, 120, 420, 320), dpi=144):
    return type(
        "Record",
        (),
        {
            "metadata": {
                "geometry": {
                    "capture_rect_screen": list(client),
                    "client_rect_screen": list(client),
                    "dpi": dpi,
                },
                "image": {"width": 160, "height": 100},
            },
        },
    )()


@pytest.fixture
def env(monkeypatch):
    sent: list[str] = []
    state = {"foreground": 99, "focus_ok": True, "frame": _frame_record(), "root_at": 0}
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(daemon.win32, "get_foreground_hwnd", lambda: state["foreground"])
    monkeypatch.setattr(daemon.win32, "focus_window", lambda pid: state["focus_ok"])
    monkeypatch.setattr(daemon.guard.win32, "get_foreground_hwnd", lambda: state["foreground"])
    monkeypatch.setattr(daemon.guard.win32, "root_window_at", lambda x, y: state["root_at"])
    monkeypatch.setattr(daemon.guard.win32, "root_window", lambda hwnd: hwnd)
    monkeypatch.setattr(
        daemon.guard.win32, "describe_window", lambda hwnd: {"hwnd": hwnd, "exe": "other.exe"}
    )
    monkeypatch.setattr(daemon.win32, "send_mouse_click", lambda *a, **k: sent.append("click") or True)
    monkeypatch.setattr(daemon.win32, "send_mouse_drag", lambda *a, **k: sent.append("drag") or True)
    monkeypatch.setattr(daemon.win32, "send_scroll", lambda *a, **k: sent.append("scroll") or True)
    monkeypatch.setattr(daemon.win32, "send_keys", lambda *a, **k: sent.append("keys") or True)
    monkeypatch.setattr(daemon.win32, "send_key_down", lambda *a, **k: sent.append("down") or 1)
    monkeypatch.setattr(daemon.win32, "send_key_up", lambda *a, **k: sent.append("up") or 1)
    monkeypatch.setattr(daemon.win32, "tap_key", lambda *a, **k: sent.append("tap") or 2)
    monkeypatch.setattr(daemon.win32, "send_edges", lambda edges: sent.append("edges") or len(edges))
    monkeypatch.setattr(
        daemon, "FRAME_CACHE", type("Cache", (), {"get": lambda self, fid: state["frame"]})()
    )
    return type("Env", (), {"sent": sent, "state": state})


MOUSE_CALLS = {
    "mouse_click": {"x": 10, "y": 10, "scope": "client"},
    "mouse_drag": {"from_x": 1, "from_y": 1, "to_x": 5, "to_y": 5, "scope": "client"},
    "scroll": {"x": 10, "y": 10, "scope": "client"},
}
KEY_CALLS = {
    "key_down": {"key": "w"},
    "tap_key": {"key": "w"},
    "hotkey": {"keys": ["ctrl", "c"]},
    "type_text": {"text": "hi"},
    "send_keys": {"keys": "hi"},
}


@pytest.mark.parametrize("name", sorted(MOUSE_CALLS))
def test_mouse_tools_refuse_activate_false_when_not_foreground(env, name) -> None:
    result = getattr(daemon, f"_h_{name}")({"target": {"pid": 2}, "activate": False, **MOUSE_CALLS[name]})

    assert result["success"] is False
    assert result["error_code"] == "TARGET_NOT_FOREGROUND"
    assert env.sent == []


@pytest.mark.parametrize("name", sorted(KEY_CALLS))
def test_key_tools_refuse_activate_false_when_not_foreground(env, name) -> None:
    result = getattr(daemon, f"_h_{name}")({"target": {"pid": 2}, "activate": False, **KEY_CALLS[name]})

    assert result["success"] is False
    assert result["error_code"] == "TARGET_NOT_FOREGROUND"
    assert env.sent == []


@pytest.mark.parametrize("name", sorted(MOUSE_CALLS))
def test_mouse_tools_stop_when_focus_fails(env, name) -> None:
    env.state["focus_ok"] = False

    result = getattr(daemon, f"_h_{name}")({"target": {"pid": 2}, **MOUSE_CALLS[name]})

    assert result["success"] is False
    assert result["error_code"] == "FOCUS_FAILED"
    assert env.sent == []


@pytest.mark.parametrize("name", sorted(MOUSE_CALLS))
def test_mouse_tools_send_once_target_is_foreground(env, name) -> None:
    env.state["foreground"] = 1

    result = getattr(daemon, f"_h_{name}")({"target": {"pid": 2}, "activate": False, **MOUSE_CALLS[name]})

    assert result["success"] is True
    assert "guard" not in result and "warnings" not in result
    assert len(env.sent) == 1


def test_key_up_is_never_gated(env) -> None:
    result = daemon._h_key_up({"target": {"pid": 2}, "key": "w", "activate": False})

    assert result["success"] is True
    assert env.sent == ["up"]


def test_absolute_mouse_exception_when_nothing_is_foreground(env) -> None:
    env.state["foreground"] = 0
    env.state["root_at"] = 1  # the target is under the destination point

    result = daemon._h_mouse_click({"target": {"pid": 2}, "activate": False, "x": 5, "y": 5, "scope": "client"})

    assert result["success"] is True
    assert result["guard"] == {"exception": "no_foreground_pointer_over_target"}
    assert env.sent == ["click"]


def test_keyboard_has_no_nothing_foreground_exception(env) -> None:
    env.state["foreground"] = 0
    env.state["root_at"] = 1

    result = daemon._h_key_down({"target": {"pid": 2}, "key": "w", "activate": False})

    assert result["error_code"] == "TARGET_NOT_FOREGROUND"
    assert env.sent == []


def test_warn_mode_sends_with_a_warning(env, monkeypatch) -> None:
    monkeypatch.setattr(daemon, "CONFIG", dataclasses.replace(config_module.Config(), foreground_guard="warn"))

    result = daemon._h_key_down({"target": {"pid": 2}, "key": "w", "activate": False})

    assert result["success"] is True
    assert result["warnings"][0]["code"] == "TARGET_NOT_FOREGROUND"
    assert env.sent == ["down"]


def test_off_mode_restores_v1_behaviour(env, monkeypatch) -> None:
    monkeypatch.setattr(daemon, "CONFIG", dataclasses.replace(config_module.Config(), foreground_guard="off"))

    result = daemon._h_mouse_click({"target": {"pid": 2}, "activate": False, "x": 1, "y": 1, "scope": "client"})

    assert result["success"] is True
    assert env.sent == ["click"]


def test_activate_true_focuses_then_still_requires_foreground(env) -> None:
    # focus "succeeded" but another window took the foreground again
    result = daemon._h_key_down({"target": {"pid": 2}, "key": "w"})

    assert result["error_code"] == "TARGET_NOT_FOREGROUND"
    assert env.sent == []


def test_frame_click_refused_when_window_moved(env) -> None:
    env.state["foreground"] = 1
    env.state["frame"] = _frame_record(client=(110, 120, 430, 320))

    result = daemon._h_mouse_click(
        {"target": {"pid": 2}, "x": 10, "y": 10, "scope": "capture", "frame_id": "frame_1", "activate": False}
    )

    assert result["error_code"] == "FRAME_GEOMETRY_CHANGED"
    assert result["details"]["current_client_rect"] == [100, 120, 420, 320]
    assert env.sent == []


def test_frame_click_refused_on_dpi_change_and_ok_when_frame_matches(env) -> None:
    env.state["foreground"] = 1
    call = {"target": {"pid": 2}, "x": 10, "y": 10, "scope": "capture", "frame_id": "frame_1", "activate": False}

    env.state["frame"] = _frame_record(dpi=96)
    assert daemon._h_mouse_click(call)["error_code"] == "FRAME_GEOMETRY_CHANGED"

    env.state["frame"] = _frame_record()
    assert daemon._h_mouse_click(call)["success"] is True


def test_frame_geometry_warn_mode_clicks_with_warning(env, monkeypatch) -> None:
    monkeypatch.setattr(
        daemon, "CONFIG", dataclasses.replace(config_module.Config(), frame_geometry_check="warn")
    )
    env.state["foreground"] = 1
    env.state["frame"] = _frame_record(client=(110, 120, 430, 320))

    result = daemon._h_mouse_click(
        {"target": {"pid": 2}, "x": 10, "y": 10, "scope": "capture", "frame_id": "frame_1", "activate": False}
    )

    assert result["success"] is True
    assert result["warnings"][0]["code"] == "FRAME_GEOMETRY_CHANGED"


def test_client_scope_is_not_subject_to_frame_geometry_check(env) -> None:
    env.state["foreground"] = 1
    env.state["frame"] = _frame_record(client=(1, 1, 2, 2))

    result = daemon._h_mouse_click(
        {"target": {"pid": 2}, "x": 10, "y": 10, "scope": "client", "frame_id": "frame_1", "activate": False}
    )

    assert result["success"] is True


def test_missing_frame_is_reported_before_any_focus_attempt(env, monkeypatch) -> None:
    focused: list[int] = []
    monkeypatch.setattr(daemon.win32, "focus_window", lambda pid: focused.append(pid) or True)
    env.state["frame"] = None

    result = daemon._h_mouse_click({"target": {"pid": 2}, "x": 1, "y": 1, "scope": "capture", "frame_id": "frame_1"})

    assert result["error_code"] == "FRAME_NOT_FOUND"
    assert focused == []
