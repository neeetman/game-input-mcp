"""One daemon per user, a status RPC, and no restart under a live input session."""
from __future__ import annotations

import pytest

from game_input_mcp import daemon, install
from tests.test_session_handlers import _open, env  # noqa: F401  (fixture re-export)


def test_daemon_status_counts_live_sessions_and_held_input(env) -> None:  # noqa: F811
    assert daemon._h_daemon_status({})["sessions"] == 0
    sid = _open(env)
    daemon._h_set_keys({"session_id": sid, "down": ["w", "a"]})
    status = daemon.HANDLERS["daemon_status"]({})
    assert status["success"] is True and status["sessions"] == 1 and status["held"] == 2
    assert status["timelines_running"] == 0 and status["pid"] > 0


def test_second_daemon_exits_without_serving(monkeypatch) -> None:
    monkeypatch.setattr(daemon, "_setup_logging", lambda: None)
    monkeypatch.setattr(daemon, "_single_instance", lambda: None)
    monkeypatch.setattr(daemon.ipc, "Server", lambda: pytest.fail("a second daemon must not serve the pipe"))
    assert daemon.main() == 0


def test_mutex_is_exclusive_per_user() -> None:
    first = daemon._single_instance()
    if first is None:
        pytest.skip("a real daemon owns the mutex on this machine")
    try:
        assert daemon._single_instance() is None
    finally:
        import win32api
        win32api.CloseHandle(first)


def test_restart_refuses_while_sessions_are_live(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(install, "_is_admin", lambda: True)
    monkeypatch.setattr(install, "_schtasks", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(install, "live_input", lambda: {"pid": 9, "sessions": 2, "timelines_running": 1})
    with pytest.raises(SystemExit, match="2 live input session"):
        install.restart()
    assert calls == []
    install.restart(force=True)
    assert calls[0][0] == "/End" and calls[1][0] == "/Run"
    calls.clear()
    monkeypatch.setattr(install, "live_input", lambda: None)   # no daemon / an old one: nothing to protect
    install.restart()
    assert len(calls) == 2
