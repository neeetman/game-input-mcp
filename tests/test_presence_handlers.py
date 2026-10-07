"""Presence policy applied by the daemon handlers (Phase 2 of the 2026-10-07 spec)."""
from __future__ import annotations

import dataclasses

import pytest

from game_input_mcp import config as config_module
from game_input_mcp import daemon
from tests.test_session_handlers import _open, env  # noqa: F401  (fixture re-export)
from tests.test_timeline_handlers import tenv  # noqa: F401  (fixture re-export)


def _policy(monkeypatch, policy: str, idle_s: float = 30.0) -> None:
    monkeypatch.setattr(
        daemon,
        "CONFIG",
        dataclasses.replace(config_module.Config(), presence=policy, presence_idle_s=idle_s),
    )


def _user_active(ticks, ms_ago: int = 5000) -> None:
    ticks.last_input = ticks.now - ms_ago


def test_get_target_info_reports_presence(env, ticks) -> None:  # noqa: F811
    away = daemon._h_get_target_info({"target": {"pid": 2}})
    _user_active(ticks, 4000)
    present = daemon._h_get_target_info({"target": {"pid": 2}})

    assert away["presence"]["state"] == "away" and away["presence"]["user_idle_ms"] == 1_000_000
    assert present["presence"]["state"] == "present"
    assert present["presence"]["user_idle_ms"] == 4000
    assert present["presence"]["policy"] == "focus" and present["presence"]["threshold_ms"] == 30_000


def test_focus_target_refused_while_user_is_active(env, ticks) -> None:  # noqa: F811
    env.foreground["hwnd"] = 99
    _user_active(ticks)

    result = daemon._h_focus_target({"target": {"pid": 2}})

    assert result["error_code"] == "USER_PRESENT"
    assert result["details"]["retry_after_ms"] == 25_000
    assert env.focus_calls == []


def test_focus_target_allowed_once_user_is_idle(env, ticks) -> None:  # noqa: F811
    env.foreground["hwnd"] = 99
    _user_active(ticks)
    ticks.now += 31_000

    result = daemon._h_focus_target({"target": {"pid": 2}})

    assert result["success"] is True
    assert env.focus_calls == [2]


def test_focus_policy_only_warns_when_target_is_already_foreground(env, ticks) -> None:  # noqa: F811
    _user_active(ticks)

    result = daemon._h_focus_target({"target": {"pid": 2}})

    assert result["success"] is True
    assert result["warnings"][0]["code"] == "USER_PRESENT"


def test_legacy_focus_window_is_gated_too(env, ticks, monkeypatch) -> None:  # noqa: F811
    env.foreground["hwnd"] = 99
    monkeypatch.setattr(
        daemon.win32, "get_window_info", lambda pid: type("Info", (), {"hwnd": 1, "pid": pid})()
    )
    _user_active(ticks)

    result = daemon._h_focus_window({"pid": 2})

    assert result["error_code"] == "USER_PRESENT"
    assert env.focus_calls == []


@pytest.mark.parametrize("policy", ["off", "warn"])
def test_relaxed_policies_do_not_block_focus(env, ticks, monkeypatch, policy) -> None:  # noqa: F811
    _policy(monkeypatch, policy)
    env.foreground["hwnd"] = 99
    _user_active(ticks)

    result = daemon._h_focus_target({"target": {"pid": 2}})

    assert result["success"] is True
    assert ("warnings" in result) == (policy == "warn")


def test_one_shot_activate_that_would_steal_focus_is_refused(env, ticks, monkeypatch) -> None:  # noqa: F811
    sends: list[str] = []
    monkeypatch.setattr(daemon.win32, "send_key_down", lambda *a, **k: sends.append("down") or 1)
    env.foreground["hwnd"] = 99
    _user_active(ticks)

    result = daemon._h_key_down({"target": {"pid": 2}, "key": "w"})

    assert result["error_code"] == "USER_PRESENT"
    assert sends == [] and env.focus_calls == []


def test_one_shot_in_foreground_warns_under_focus_and_refuses_under_strict(env, ticks, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(daemon.win32, "send_key_down", lambda *a, **k: 1)
    _user_active(ticks)

    warned = daemon._h_key_down({"target": {"pid": 2}, "key": "w", "activate": False})
    _policy(monkeypatch, "strict")
    refused = daemon._h_key_down({"target": {"pid": 2}, "key": "w", "activate": False})

    assert warned["success"] is True and warned["warnings"][0]["code"] == "USER_PRESENT"
    assert refused["error_code"] == "USER_PRESENT"


def test_key_up_is_never_blocked_by_presence(env, ticks, monkeypatch) -> None:  # noqa: F811
    _policy(monkeypatch, "strict")
    monkeypatch.setattr(daemon.win32, "send_key_up", lambda *a, **k: 1)
    _user_active(ticks)

    result = daemon._h_key_up({"target": {"pid": 2}, "key": "w", "activate": False})

    assert result["success"] is True


def test_session_open_refused_without_creating_a_session(env, ticks) -> None:  # noqa: F811
    env.foreground["hwnd"] = 99
    _user_active(ticks)

    result = daemon._h_session_open({"target": {"pid": 2}})

    assert result["error_code"] == "USER_PRESENT"
    assert env.registry.live_count() == 0 and env.focus_calls == []


def test_session_open_in_front_warns_and_reports_presence(env, ticks) -> None:  # noqa: F811
    _user_active(ticks)

    result = daemon._h_session_open({"target": {"pid": 2}})

    assert result["success"] is True
    assert result["warnings"][0]["code"] == "USER_PRESENT"
    assert result["presence"]["state"] == "present"


def test_set_keys_under_focus_policy_warns_but_sends(env, ticks) -> None:  # noqa: F811
    sid = _open(env)
    _user_active(ticks)

    result = daemon._h_set_keys({"session_id": sid, "down": ["w"]})

    assert result["success"] is True and result["held_keys"] == ["w"]
    assert result["warnings"][0]["code"] == "USER_PRESENT"


def test_strict_user_input_pauses_session_releases_keys_and_resumes_when_idle(env, ticks, monkeypatch) -> None:  # noqa: F811
    _policy(monkeypatch, "strict")
    sid = _open(env)
    assert daemon._h_set_keys({"session_id": sid, "down": ["w"]})["held_keys"] == ["w"]

    _user_active(ticks, 200)  # the user touches the keyboard
    taken = daemon._h_set_keys({"session_id": sid, "down": ["a"]})

    assert taken["error_code"] == "USER_TOOK_OVER" and taken["retryable"] is True
    assert taken["details"]["released"] == ["w"]
    assert env.sent[-1] == [("key", "w", False)]  # the release went out
    state = daemon._h_session_state({"session_id": sid})
    assert state["status"] == "paused" and state["reason"] == "user_input" and state["held_keys"] == []

    again = daemon._h_set_keys({"session_id": sid, "down": ["a"]})
    assert again["error_code"] == "USER_TOOK_OVER"

    ticks.now += 31_000  # hands off for longer than the threshold
    resumed = daemon._h_set_keys({"session_id": sid, "down": ["a"]})

    assert resumed["success"] is True and resumed["held_keys"] == ["a"]
    assert daemon._h_session_state({"session_id": sid})["status"] == "active"


def test_strict_session_open_refused_while_user_present(env, ticks, monkeypatch) -> None:  # noqa: F811
    _policy(monkeypatch, "strict")
    _user_active(ticks)

    result = daemon._h_session_open({"target": {"pid": 2}, "focus": "none"})

    assert result["error_code"] == "USER_PRESENT"


def test_strict_timeline_stops_when_user_types_mid_run(tenv, ticks, monkeypatch) -> None:  # noqa: F811
    _policy(monkeypatch, "strict")
    sid = _open(tenv)
    original = daemon.win32.send_edges

    def user_types_after_first_batch(edges):
        ticks.last_input = ticks.now - 50
        return original(edges)

    monkeypatch.setattr(daemon.win32, "send_edges", user_types_after_first_batch)

    result = daemon._h_run_timeline(
        {
            "session_id": sid,
            "events": [
                {"t_ms": 0, "op": "down", "key": "w"},
                {"t_ms": 100, "op": "up", "key": "w"},
                {"t_ms": 200, "op": "down", "key": "a"},
                {"t_ms": 300, "op": "up", "key": "a"},
            ],
            "total_ms": 400,
        }
    )

    assert result["error_code"] == "USER_TOOK_OVER"
    details = result["details"]
    assert details["stopped_reason"] == "user_input"
    assert len(details["batches"]) == 1
    assert details["pending_indices"] == [1, 2, 3]
    assert details["released_on_exit"] == ["w"]
    assert details["held_keys"] == []
    state = daemon._h_session_state({"session_id": sid})
    assert state["status"] == "paused" and state["reason"] == "user_input"


def test_timeline_ignores_user_activity_unless_policy_is_strict(tenv, ticks, monkeypatch) -> None:  # noqa: F811
    sid = _open(tenv)
    _user_active(ticks, 50)

    result = daemon._h_run_timeline(
        {
            "session_id": sid,
            "events": [{"t_ms": 0, "op": "down", "key": "w"}, {"t_ms": 100, "op": "up", "key": "w"}],
            "total_ms": 200,
        }
    )

    assert result["success"] is True and result["stopped_reason"] == "completed"
