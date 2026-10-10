"""A user who is active never makes the daemon skip input; it is only reported
(`presence` in get_target_info and a USER_PRESENT warning)."""
from __future__ import annotations

import dataclasses

import pytest

from game_input_mcp import config as config_module
from game_input_mcp import daemon
from tests.test_session_handlers import _open, env  # noqa: F401  (fixture re-export)
from tests.test_timeline_handlers import tenv  # noqa: F401  (fixture re-export)


def _policy(monkeypatch, policy: str) -> None:
    monkeypatch.setattr(daemon, "CONFIG", dataclasses.replace(config_module.Config(), presence=policy))


def _user_active(ticks, ms_ago: int = 5000) -> None:
    ticks.last_input = ticks.now - ms_ago


def _codes(result) -> list[str]:
    return [w["code"] for w in result.get("warnings", [])]


def test_get_target_info_reports_presence(env, ticks) -> None:  # noqa: F811
    away = daemon._h_get_target_info({"target": {"pid": 2}})
    _user_active(ticks, 4000)
    present = daemon._h_get_target_info({"target": {"pid": 2}})

    assert away["presence"]["state"] == "away" and away["presence"]["user_idle_ms"] == 1_000_000
    assert present["presence"]["state"] == "present"
    assert present["presence"]["user_idle_ms"] == 4000
    assert present["presence"]["policy"] == "warn" and present["presence"]["threshold_ms"] == 30_000


def test_focus_target_still_focuses_while_the_user_is_active(env, ticks) -> None:  # noqa: F811
    env.foreground["hwnd"] = 99
    _user_active(ticks)

    result = daemon._h_focus_target({"target": {"pid": 2}})

    assert result["success"] is True and env.focus_calls == [2]
    assert _codes(result) == ["USER_PRESENT"]


def test_legacy_focus_window_is_not_blocked_either(env, ticks, monkeypatch) -> None:  # noqa: F811
    env.foreground["hwnd"] = 99
    monkeypatch.setattr(
        daemon.win32, "get_window_info", lambda pid: type("Info", (), {"hwnd": 1, "pid": pid})()
    )
    _user_active(ticks)

    result = daemon._h_focus_window({"pid": 2})

    assert result["success"] is True and env.focus_calls == [2]
    assert _codes(result) == ["USER_PRESENT"]


def test_no_warning_when_the_user_is_away(env, ticks) -> None:  # noqa: F811
    env.foreground["hwnd"] = 99

    result = daemon._h_focus_target({"target": {"pid": 2}})

    assert result["success"] is True and "warnings" not in result


def test_off_turns_the_warnings_off(env, ticks, monkeypatch) -> None:  # noqa: F811
    _policy(monkeypatch, "off")
    env.foreground["hwnd"] = 99
    _user_active(ticks)

    result = daemon._h_focus_target({"target": {"pid": 2}})

    assert result["success"] is True and "warnings" not in result
    assert daemon._h_get_target_info({"target": {"pid": 2}})["presence"]["policy"] == "off"


def test_one_shot_input_is_sent_while_the_user_is_active(env, ticks, monkeypatch) -> None:  # noqa: F811
    sends: list[str] = []
    monkeypatch.setattr(daemon.win32, "send_key_down", lambda *a, **k: sends.append("down") or 1)
    _user_active(ticks)

    in_front = daemon._h_key_down({"target": {"pid": 2}, "key": "w", "activate": False})
    env.foreground["hwnd"] = 99
    after_focus = daemon._h_key_down({"target": {"pid": 2}, "key": "w"})  # focus is mocked, foreground stays 99

    assert in_front["success"] is True and _codes(in_front) == ["USER_PRESENT"]
    assert sends == ["down"]  # the second call is stopped by the *foreground* guard, not by presence
    assert after_focus["error_code"] == "TARGET_NOT_FOREGROUND"


def test_session_open_and_set_keys_just_warn(env, ticks) -> None:  # noqa: F811
    _user_active(ticks)

    opened = daemon._h_session_open({"target": {"pid": 2}})
    sid = opened["session_id"]
    sent = daemon._h_set_keys({"session_id": sid, "down": ["w"]})

    assert opened["success"] is True and _codes(opened) == ["USER_PRESENT"]
    assert opened["presence"]["state"] == "present"
    assert sent["success"] is True and sent["held_keys"] == ["w"] and _codes(sent) == ["USER_PRESENT"]


def test_user_input_never_pauses_a_session(env, ticks) -> None:  # noqa: F811
    sid = _open(env)
    assert daemon._h_set_keys({"session_id": sid, "down": ["w"]})["held_keys"] == ["w"]

    _user_active(ticks, 200)  # the user touches the keyboard
    again = daemon._h_set_keys({"session_id": sid, "down": ["a"]})

    assert again["success"] is True and again["held_keys"] == ["a", "w"]
    state = daemon._h_session_state({"session_id": sid})
    assert state["status"] == "active" and state["reason"] is None


def test_a_timeline_runs_to_the_end_even_if_the_user_types_during_it(tenv, ticks) -> None:  # noqa: F811
    sid = _open(tenv)
    original = daemon.win32.send_edges

    def user_types_after_each_batch(edges):
        ticks.last_input = ticks.now - 50
        return original(edges)

    daemon.win32.send_edges = user_types_after_each_batch
    try:
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
    finally:
        daemon.win32.send_edges = original

    assert result["success"] is True and result["stopped_reason"] == "completed"
    assert len(result["batches"]) == 4 and result["pending_indices"] == []


def test_the_refusing_error_codes_are_gone() -> None:
    import pathlib

    source = pathlib.Path(daemon.__file__).read_text(encoding="utf-8")

    assert "USER_TOOK_OVER" not in source and "user_input" not in source
