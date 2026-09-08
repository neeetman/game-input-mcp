from __future__ import annotations

import pytest

from game_input_mcp import daemon
from game_input_mcp.input.state import SessionRegistry
from game_input_mcp.models import Rect, TargetInfo


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _target(hwnd: int = 1, foreground: bool = True) -> TargetInfo:
    return TargetInfo(
        hwnd=hwnd,
        pid=2,
        title="Game",
        window_rect=Rect(0, 0, 100, 100),
        client_rect_screen=Rect(0, 0, 100, 100),
        client_size=(100, 100),
        client_screen_origin=(0, 0),
        dpi=96,
        is_foreground=foreground,
    )


@pytest.fixture
def env(monkeypatch):
    clock = Clock()
    registry = SessionRegistry(clock=clock)
    sent_batches: list[list[tuple[str, str, bool]]] = []
    focus_calls: list[int] = []
    foreground = {"hwnd": 1}

    monkeypatch.setattr(daemon, "SESSIONS", registry)
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(
        daemon.win32,
        "focus_window_detailed",
        lambda pid: focus_calls.append(pid) or {"success": True, "hwnd": 1},
    )
    monkeypatch.setattr(daemon.win32, "get_foreground_hwnd", lambda: foreground["hwnd"])
    monkeypatch.setattr(daemon.win32, "qpc_ns", lambda: 123_456_789)

    def fake_send_edges(edges):
        sent_batches.append([(e.kind, e.name, e.down) for e in edges])
        return len(edges)

    monkeypatch.setattr(daemon.win32, "send_edges", fake_send_edges)
    return type(
        "Env",
        (),
        {
            "clock": clock,
            "registry": registry,
            "sent": sent_batches,
            "focus_calls": focus_calls,
            "foreground": foreground,
        },
    )()


def _open(env, **params) -> str:
    result = daemon._h_session_open({"target": {"pid": 2}, **params})
    assert result["success"] is True, result
    return result["session_id"]


def test_session_open_focuses_once_and_reports_state(env) -> None:
    result = daemon._h_session_open({"target": {"pid": 2}, "lease_ms": 3000})

    assert result["success"] is True
    assert result["hwnd"] == 1 and result["pid"] == 2
    assert result["foreground"] is True
    assert result["focus_policy"] == "acquire_once"
    assert result["lease_ms"] == 3000
    assert env.focus_calls == [2]


def test_session_open_with_policy_none_does_not_focus(env) -> None:
    env.foreground["hwnd"] = 99

    result = daemon._h_session_open({"target": {"pid": 2}, "focus": "none"})

    assert result["success"] is True
    assert result["foreground"] is False
    assert env.focus_calls == []


def test_session_open_fails_closed_when_focus_fails(env, monkeypatch) -> None:
    monkeypatch.setattr(
        daemon.win32, "focus_window_detailed", lambda pid: {"success": False, "hwnd": 1}
    )

    result = daemon._h_session_open({"target": {"pid": 2}})

    assert result["error_code"] == "FOCUS_FAILED"
    assert env.registry.sweep() == [] and env.registry.live_count() == 0


def test_session_open_reports_target_and_duplicate_errors(env, monkeypatch) -> None:
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: None)
    assert daemon._h_session_open({"target": {"pid": 2}})["error_code"] == "TARGET_NOT_FOUND"

    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    first = _open(env)
    dup = daemon._h_session_open({"target": {"pid": 2}})

    assert dup["error_code"] == "SESSION_EXISTS"
    assert dup["details"]["session_id"] == first


def test_session_open_takeover_releases_previous_session(env) -> None:
    first = _open(env)
    daemon._h_set_keys({"session_id": first, "down": ["w"]})

    result = daemon._h_session_open({"target": {"pid": 2}, "takeover": True})

    assert result["success"] is True
    assert result["replaced_session_id"] == first
    assert env.sent[-1] == [("key", "w", False)]
    assert daemon._h_session_state({"session_id": first})["error_code"] == "SESSION_NOT_FOUND"


def test_session_open_rejects_bad_parameters(env) -> None:
    result = daemon._h_session_open({"target": {"pid": 2}, "lease_ms": 5})

    assert result["error_code"] == "INVALID_PARAMS"


def test_set_keys_sends_one_batch_ups_first_and_tracks_state(env) -> None:
    sid = _open(env)
    daemon._h_set_keys({"session_id": sid, "down": ["s"]})

    result = daemon._h_set_keys(
        {"session_id": sid, "down": ["w", "a"], "up": ["s"], "buttons_down": ["right"]}
    )

    assert result["success"] is True
    assert result["sent"] == 4 and result["expected"] == 4
    assert result["qpc_ns"] == 123_456_789
    assert result["held_keys"] == ["a", "w"]
    assert result["held_buttons"] == ["right"]
    assert env.sent[-1] == [
        ("key", "s", False),
        ("button", "right", True),
        ("key", "w", True),
        ("key", "a", True),
    ]
    assert len(env.sent) == 2  # exactly one SendInput per set_keys call


def test_set_keys_is_idempotent_and_reports_skips(env) -> None:
    sid = _open(env)
    daemon._h_set_keys({"session_id": sid, "down": ["w"]})
    batches_before = len(env.sent)

    result = daemon._h_set_keys({"session_id": sid, "down": ["w"], "up": ["d"]})

    assert result["success"] is True
    assert result["sent"] == 0 and result["skipped"] == ["d", "w"]
    assert len(env.sent) == batches_before  # nothing to send, no SendInput call


def test_set_keys_reports_partial_send(env, monkeypatch) -> None:
    sid = _open(env)
    monkeypatch.setattr(daemon.win32, "send_edges", lambda edges: len(edges) - 1)

    result = daemon._h_set_keys({"session_id": sid, "down": ["w", "a"]})

    assert result["success"] is False
    assert result["sent"] == 1 and result["expected"] == 2
    assert result["held_keys"] == ["a", "w"]  # tracked so release still covers them


def test_set_keys_invalid_key_sends_nothing(env, monkeypatch) -> None:
    sid = _open(env)
    monkeypatch.setattr(daemon.keys, "_map_vk_to_scancode", lambda vk: None)
    monkeypatch.setattr(daemon.keys, "_char_to_vk", lambda ch: None)

    result = daemon._h_set_keys({"session_id": sid, "down": ["w", "not-a-key"]})

    assert result["error_code"] == "INVALID_KEY"
    assert env.sent == []
    assert daemon._h_session_state({"session_id": sid})["held_keys"] == []


def test_set_keys_releases_everything_on_focus_loss(env) -> None:
    sid = _open(env)
    daemon._h_set_keys({"session_id": sid, "down": ["w"], "buttons_down": ["left"]})
    env.foreground["hwnd"] = 42

    result = daemon._h_set_keys({"session_id": sid, "down": ["a"]})

    assert result["error_code"] == "FOCUS_LOST"
    assert result["retryable"] is True
    assert sorted(result["details"]["released"]) == ["left", "w"]
    assert result["details"]["foreground_hwnd"] == 42
    assert sorted(env.sent[-1]) == [("button", "left", False), ("key", "w", False)]
    state = daemon._h_session_state({"session_id": sid})
    assert state["status"] == "paused" and state["held_keys"] == []
    assert len(env.focus_calls) == 1  # never re-focused after open


def test_paused_session_resumes_when_target_is_foreground_again(env) -> None:
    sid = _open(env)
    env.foreground["hwnd"] = 42
    daemon._h_set_keys({"session_id": sid, "down": ["w"]})
    env.foreground["hwnd"] = 1

    result = daemon._h_set_keys({"session_id": sid, "down": ["w"]})

    assert result["success"] is True
    assert daemon._h_session_state({"session_id": sid})["status"] == "active"


def test_acquire_each_policy_focuses_before_every_send(env) -> None:
    sid = _open(env, focus="acquire_each")
    env.foreground["hwnd"] = 42

    result = daemon._h_set_keys({"session_id": sid, "down": ["w"]})

    assert result["success"] is True
    assert env.focus_calls == [2, 2]


def test_set_keys_on_expired_session_reports_reason(env) -> None:
    sid = _open(env, lease_ms=1000)
    daemon._h_set_keys({"session_id": sid, "down": ["w"]})
    env.clock.now += 5
    daemon._watchdog_sweep()

    assert env.sent[-1] == [("key", "w", False)]
    result = daemon._h_set_keys({"session_id": sid, "down": ["w"]})

    assert result["error_code"] == "SESSION_EXPIRED"
    assert result["details"]["reason"] == "lease_expired"
    assert daemon._h_set_keys({"session_id": "nope", "down": ["w"]})["error_code"] == "SESSION_NOT_FOUND"


def test_heartbeat_keeps_session_alive(env) -> None:
    sid = _open(env, lease_ms=1000)
    env.clock.now += 0.8
    assert daemon._h_session_heartbeat({"session_id": sid})["success"] is True
    env.clock.now += 0.8
    daemon._watchdog_sweep()

    assert daemon._h_session_state({"session_id": sid})["status"] == "active"


def test_session_close_releases_and_removes(env) -> None:
    sid = _open(env)
    daemon._h_set_keys({"session_id": sid, "down": ["w", "space"]})

    result = daemon._h_session_close({"session_id": sid})

    assert result["success"] is True
    assert sorted(result["released"]) == ["space", "w"]
    assert sorted(env.sent[-1]) == [("key", "space", False), ("key", "w", False)]
    assert daemon._h_session_close({"session_id": sid})["error_code"] == "SESSION_NOT_FOUND"


def test_session_state_reports_held_and_age(env) -> None:
    sid = _open(env)
    daemon._h_set_keys({"session_id": sid, "down": ["w"]})
    env.clock.now += 1.25

    result = daemon._h_session_state({"session_id": sid})

    assert result["held_keys"] == ["w"] and result["held_buttons"] == []
    assert result["age_ms"] == 1250
    assert result["since_heartbeat_ms"] == 1250
    assert result["foreground"] is True


def test_release_all_sessions_on_shutdown(env) -> None:
    a = _open(env)
    daemon._h_set_keys({"session_id": a, "down": ["w"]})

    released = daemon._release_all_sessions("daemon_shutdown")

    assert released == {a: ["w"]}
    assert env.sent[-1] == [("key", "w", False)]


def test_hotkey_sends_chord_as_two_batches(env, monkeypatch) -> None:
    monkeypatch.setattr(daemon.win32, "focus_window", lambda pid: True)

    result = daemon._h_hotkey({"target": {"pid": 2}, "keys": ["ctrl", "s"], "mode": "vk"})

    assert result["success"] is True and result["sent"] == 4
    assert env.sent == [
        [("key", "ctrl", True), ("key", "s", True)],
        [("key", "s", False), ("key", "ctrl", False)],
    ]
