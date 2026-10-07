"""set_window_geometry: gated, refuses windows it should not touch (Phase 5)."""
from __future__ import annotations

import dataclasses

import pytest

from game_input_mcp import config as config_module
from game_input_mcp import daemon, server
from game_input_mcp.models import Rect, TargetInfo


def _target(client=(800, 600), minimized=False) -> TargetInfo:
    return TargetInfo(
        hwnd=7,
        pid=3,
        title="Game",
        window_rect=Rect(100, 100, 100 + client[0] + 16, 100 + client[1] + 39),
        client_rect_screen=Rect(108, 131, 108 + client[0], 131 + client[1]),
        client_size=client,
        client_screen_origin=(108, 131),
        dpi=96,
        is_foreground=True,
        is_minimized=minimized,
    )


@pytest.fixture
def env(monkeypatch):
    state = type(
        "State",
        (),
        {
            "target": _target(),
            "kind": {"has_frame": True, "maximized": False, "minimized": False, "fullscreen": False},
            "calls": [],
            "ok": True,
            "result_size": None,   # None -> exactly what was asked for
        },
    )()
    monkeypatch.setattr(
        daemon, "CONFIG", dataclasses.replace(config_module.Config(), allow_window_mutation=True)
    )
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: state.target)
    monkeypatch.setattr(daemon.win32, "window_frame_kind", lambda hwnd: state.kind)

    def fake_set(hwnd, client_size=None, position=None):
        state.calls.append((hwnd, client_size, position))
        if state.ok and client_size is not None:
            state.target = _target(state.result_size or client_size)
        return state.ok, 0 if state.ok else 5

    monkeypatch.setattr(daemon.win32, "set_window_geometry", fake_set)
    return state


def test_disabled_by_default_and_changes_nothing(monkeypatch) -> None:
    monkeypatch.setattr(daemon, "CONFIG", config_module.Config())
    called = []
    monkeypatch.setattr(daemon.win32, "set_window_geometry", lambda *a, **k: called.append(a) or (True, 0))

    result = daemon._h_set_window_geometry({"target": {"pid": 3}, "client_size": [1920, 1080]})

    assert result["error_code"] == "WINDOW_MUTATION_DISABLED" and result["retryable"] is False
    assert "allow_window_mutation" in result["details"]["hint"]
    assert called == []


def test_resize_reports_the_new_client_size_and_warns_to_recapture(env) -> None:
    result = daemon._h_set_window_geometry({"target": {"pid": 3}, "client_size": [1920, 1080]})

    assert result["success"] is True
    assert env.calls == [(7, (1920, 1080), None)]
    assert result["target"]["client_size"] == [1920, 1080]
    assert result["requested"] == {"client_size": [1920, 1080], "position": None}
    assert "capture again" in result["hint"]
    assert "warnings" not in result


def test_move_only(env) -> None:
    result = daemon._h_set_window_geometry({"target": {"pid": 3}, "position": [-1800, 40]})

    assert result["success"] is True and env.calls == [(7, None, (-1800, 40))]


def test_a_window_that_clamps_the_size_gets_a_warning(env) -> None:
    env.result_size = (1280, 720)

    result = daemon._h_set_window_geometry({"target": {"pid": 3}, "client_size": [800, 600]})

    assert result["success"] is True
    [warning] = result["warnings"]
    assert warning["code"] == "WINDOW_SIZE_ADJUSTED"
    assert warning["details"] == {"requested": [800, 600], "actual": [1280, 720]}


@pytest.mark.parametrize(
    ("kind", "reason"),
    [
        ({"minimized": True}, "minimized"),
        ({"maximized": True}, "maximized"),
        ({"fullscreen": True}, "whole monitor"),
    ],
)
def test_refuses_windows_it_should_not_touch(env, kind, reason) -> None:
    env.kind = {**env.kind, **kind}

    result = daemon._h_set_window_geometry({"target": {"pid": 3}, "client_size": [800, 600]})

    assert result["error_code"] == "WINDOW_RESIZE_FAILED" and reason in result["message"]
    assert env.calls == []


def test_frameless_windows_cannot_be_resized_but_can_be_moved(env) -> None:
    env.kind = {**env.kind, "has_frame": False}

    refused = daemon._h_set_window_geometry({"target": {"pid": 3}, "client_size": [800, 600]})
    moved = daemon._h_set_window_geometry({"target": {"pid": 3}, "position": [10, 10]})

    assert refused["error_code"] == "WINDOW_RESIZE_FAILED" and "no caption frame" in refused["message"]
    assert moved["success"] is True


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"client_size": [800]},
        {"client_size": [10, 10]},
        {"client_size": [99999, 600]},
        {"client_size": "big"},
        {"position": [1, 2, 3]},
    ],
)
def test_invalid_parameters_are_rejected_before_touching_the_window(env, params) -> None:
    result = daemon._h_set_window_geometry({"target": {"pid": 3}, **params})

    assert result["error_code"] == "INVALID_PARAMS"
    assert env.calls == []


def test_setwindowpos_failure_is_structured_and_retryable(env) -> None:
    env.ok = False

    result = daemon._h_set_window_geometry({"target": {"pid": 3}, "client_size": [800, 600]})

    assert result["error_code"] == "WINDOW_RESIZE_FAILED" and result["retryable"] is True
    assert result["details"]["win32_error"] == 5


def test_unknown_target_and_ambiguity_pass_through(env, monkeypatch) -> None:
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: None)

    assert daemon._h_set_window_geometry({"target": {"pid": 9}, "client_size": [800, 600]})["error_code"] == "TARGET_NOT_FOUND"


def test_registered_with_the_daemon_and_exposed_as_a_tool(monkeypatch) -> None:
    assert daemon.HANDLERS["set_window_geometry"] is daemon._h_set_window_geometry
    calls = []
    monkeypatch.setattr(server, "_call", lambda method, **params: calls.append((method, params)) or {"success": True})

    server.set_window_geometry({"exe": "Game"}, client_size=[1920, 1080])

    assert calls == [
        ("set_window_geometry", {"target": {"exe": "Game"}, "client_size": [1920, 1080], "position": None})
    ]


def test_real_window_kind_of_the_desktop_is_well_formed() -> None:
    from game_input_mcp import win32

    infos = win32.list_window_infos()
    assert infos
    kind = win32.window_frame_kind(infos[0].hwnd)
    assert set(kind) == {"has_frame", "maximized", "minimized", "fullscreen"}
    assert all(isinstance(v, bool) for v in kind.values())
