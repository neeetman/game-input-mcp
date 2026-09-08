from __future__ import annotations

import threading
import time

import pytest

from game_input_mcp import daemon
from tests.test_session_handlers import _open, env  # noqa: F401  (fixture re-export)


@pytest.fixture
def tenv(env, monkeypatch):
    # Deterministic runner: waiting advances the fake clock instead of sleeping.
    def fake_wait(abort, seconds):
        env.clock.now += seconds
        return abort.is_set()

    monkeypatch.setattr(daemon, "_timeline_wait", fake_wait)
    monkeypatch.setattr(daemon, "_timeline_clock", env.clock)
    monkeypatch.setattr(daemon, "_timeline_spin_margin_s", 0.0)  # fake clock never advances while spinning
    return env


def test_run_timeline_applies_edges_and_reports_per_batch(tenv) -> None:
    sid = _open(tenv)
    events = [
        {"t_ms": 0, "op": "down", "key": "w"},
        {"t_ms": 200, "op": "down", "key": "a"},
        {"t_ms": 200, "op": "up", "key": "w"},
        {"t_ms": 500, "op": "up", "key": "a"},
    ]

    result = daemon._h_run_timeline({"session_id": sid, "events": events, "total_ms": 600})

    assert result["success"] is True
    assert result["stopped_reason"] == "completed"
    assert [b["t_ms"] for b in result["batches"]] == [0.0, 200.0, 500.0]
    assert result["batches"][1]["sent"] == 2 and result["batches"][1]["qpc_ns"] == 123_456_789
    assert [e["index"] for e in result["batches"][1]["events"]] == [2, 1]
    assert result["held_keys"] == [] and result["released_on_exit"] == []
    assert result["ended_ms"] == pytest.approx(600.0)
    assert tenv.sent == [
        [("key", "w", True)],
        [("key", "w", False), ("key", "a", True)],
        [("key", "a", False)],
    ]
    state = daemon._h_session_state({"session_id": sid})
    assert state["status"] == "active" and state["held_keys"] == []


def test_run_timeline_marks_session_busy_and_refreshes_heartbeat(tenv, monkeypatch) -> None:
    sid = _open(tenv, lease_ms=500)
    record = tenv.registry.get(sid)
    seen_busy = []
    original = daemon.win32.send_edges

    def spy(edges):
        seen_busy.append(record.busy_until is not None and record.busy_until > tenv.clock.now)
        return original(edges)

    monkeypatch.setattr(daemon.win32, "send_edges", spy)

    result = daemon._h_run_timeline(
        {
            "session_id": sid,
            "events": [{"t_ms": 0, "op": "down", "key": "w"}, {"t_ms": 2000, "op": "up", "key": "w"}],
            "total_ms": 2000,
        }
    )

    assert result["success"] is True
    assert seen_busy == [True, True]
    assert record.busy_until is None
    assert record.last_heartbeat == tenv.clock.now


def test_run_timeline_dangling_keys_stay_held(tenv) -> None:
    sid = _open(tenv)

    result = daemon._h_run_timeline(
        {
            "session_id": sid,
            "events": [{"t_ms": 0, "op": "down", "key": "w"}],
            "total_ms": 100,
            "allow_dangling": True,
        }
    )

    assert result["success"] is True
    assert result["held_keys"] == ["w"]
    assert daemon._h_session_state({"session_id": sid})["held_keys"] == ["w"]


def test_run_timeline_rejects_invalid_timeline_without_sending(tenv) -> None:
    sid = _open(tenv)

    result = daemon._h_run_timeline(
        {"session_id": sid, "events": [{"t_ms": 0, "op": "down", "key": "w"}], "total_ms": 100}
    )

    assert result["error_code"] == "INVALID_TIMELINE"
    assert tenv.sent == []


def test_run_timeline_focus_lost_releases_everything(tenv, monkeypatch) -> None:
    sid = _open(tenv)
    daemon._h_set_keys({"session_id": sid, "down": ["lshift"]})
    original = daemon.win32.send_edges

    def lose_focus_after_first(edges):
        tenv.foreground["hwnd"] = 42
        return original(edges)

    monkeypatch.setattr(daemon.win32, "send_edges", lose_focus_after_first)

    result = daemon._h_run_timeline(
        {
            "session_id": sid,
            "events": [{"t_ms": 0, "op": "down", "key": "w"}, {"t_ms": 100, "op": "up", "key": "w"}],
            "total_ms": 200,
        }
    )

    assert result["success"] is False
    assert result["error_code"] == "FOCUS_LOST"
    assert result["details"]["stopped_reason"] == "focus_lost"
    assert sorted(result["details"]["released_on_exit"]) == ["lshift", "w"]
    assert len(result["details"]["batches"]) == 1
    assert daemon._h_session_state({"session_id": sid})["status"] == "paused"


def test_run_timeline_refuses_concurrent_runs_and_abort_releases(tenv, monkeypatch) -> None:
    sid = _open(tenv)
    started = threading.Event()
    release_wait = threading.Event()

    def blocking_wait(abort, seconds):
        started.set()
        release_wait.wait(2.0)
        return abort.is_set()

    monkeypatch.setattr(daemon, "_timeline_wait", blocking_wait)
    box: dict = {}

    def run():
        box["r"] = daemon._h_run_timeline(
            {
                "session_id": sid,
                "events": [{"t_ms": 0, "op": "down", "key": "w"}, {"t_ms": 5000, "op": "up", "key": "w"}],
                "total_ms": 5000,
            }
        )

    t = threading.Thread(target=run)
    t.start()
    assert started.wait(2.0)

    busy = daemon._h_run_timeline({"session_id": sid, "events": [], "total_ms": 100})
    assert busy["error_code"] == "TIMELINE_RUNNING"

    abort_thread = threading.Thread(
        target=lambda: box.__setitem__("a", daemon._h_abort_timeline({"session_id": sid}))
    )
    abort_thread.start()
    time.sleep(0.05)
    release_wait.set()
    t.join(3.0)
    abort_thread.join(3.0)

    assert box["r"]["error_code"] == "ABORTED"
    assert box["r"]["details"]["released_on_exit"] == ["w"]
    assert box["a"]["success"] is True and box["a"]["released"] == ["w"]
    assert tenv.sent[-1] == [("key", "w", False)]
    assert daemon._h_abort_timeline({"session_id": sid})["error_code"] == "NO_TIMELINE"


def test_run_timeline_on_expired_or_unknown_session(tenv) -> None:
    sid = _open(tenv, lease_ms=1000)
    tenv.clock.now += 5
    daemon._watchdog_sweep()

    assert daemon._h_run_timeline({"session_id": sid, "events": [], "total_ms": 100})["error_code"] == "SESSION_EXPIRED"
    assert daemon._h_run_timeline({"session_id": "nope", "events": [], "total_ms": 100})["error_code"] == "SESSION_NOT_FOUND"
    assert daemon._h_abort_timeline({"session_id": "nope"})["error_code"] == "SESSION_NOT_FOUND"
