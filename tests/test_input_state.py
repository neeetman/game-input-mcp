from __future__ import annotations

import pytest

from game_input_mcp.input import state
from game_input_mcp.input.state import (
    Edge,
    SessionExists,
    SessionNotFound,
    SessionRegistry,
    Watchdog,
    plan_release,
    plan_state_change,
)


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _open(registry: SessionRegistry, hwnd: int = 1, **kwargs):
    record, _replaced = registry.open(hwnd=hwnd, pid=hwnd * 10, **kwargs)
    return record


# --- registry ---------------------------------------------------------------

def test_open_returns_active_record_with_defaults() -> None:
    registry = SessionRegistry(clock=Clock())

    record = _open(registry)

    assert record.status == "active"
    assert record.focus_policy == "acquire_once"
    assert record.lease_ms == 2000
    assert record.max_hold_ms == 30000
    assert record.held_keys == {} and record.held_buttons == {}
    assert registry.get(record.session_id) is record


def test_open_same_hwnd_twice_requires_takeover() -> None:
    registry = SessionRegistry(clock=Clock())
    first = _open(registry)
    first.held_keys["w"] = state.HeldEntry(stroke=None, since=0.0)

    with pytest.raises(SessionExists):
        _open(registry)

    second, replaced = registry.open(hwnd=1, pid=10, takeover=True)

    assert replaced is first
    assert replaced.status == "closed"
    assert second.session_id != first.session_id
    with pytest.raises(SessionNotFound):
        registry.get(first.session_id)


def test_open_validates_bounds_and_policy() -> None:
    registry = SessionRegistry(clock=Clock())

    with pytest.raises(ValueError, match="lease_ms"):
        _open(registry, lease_ms=10)
    with pytest.raises(ValueError, match="max_hold_ms"):
        _open(registry, max_hold_ms=10)
    with pytest.raises(ValueError, match="focus_policy"):
        _open(registry, focus_policy="sometimes")


def test_heartbeat_extends_lease_and_can_change_it() -> None:
    clock = Clock()
    registry = SessionRegistry(clock=clock)
    record = _open(registry)

    clock.now += 1.5
    registry.heartbeat(record.session_id, lease_ms=5000)

    assert record.last_heartbeat == clock.now
    assert record.lease_ms == 5000


def test_close_removes_record() -> None:
    registry = SessionRegistry(clock=Clock())
    record = _open(registry)

    closed = registry.close(record.session_id)

    assert closed is record and record.status == "closed"
    with pytest.raises(SessionNotFound):
        registry.get(record.session_id)
    with pytest.raises(SessionNotFound):
        registry.close(record.session_id)


def test_sweep_expires_on_lease_timeout() -> None:
    clock = Clock()
    registry = SessionRegistry(clock=clock)
    record = _open(registry, lease_ms=1000)

    assert registry.sweep() == []
    clock.now += 1.2

    expired = registry.sweep()

    assert expired == [(record, "lease_expired")]
    assert record.status == "expired"
    assert registry.sweep() == []  # reported once


def test_sweep_expires_when_a_key_is_held_too_long() -> None:
    clock = Clock()
    registry = SessionRegistry(clock=clock)
    record = _open(registry, lease_ms=60000, max_hold_ms=500)
    record.held_keys["w"] = state.HeldEntry(stroke=None, since=clock.now)

    clock.now += 0.4
    assert registry.sweep() == []
    clock.now += 0.2

    assert registry.sweep() == [(record, "max_hold_exceeded")]


def test_expired_record_stays_readable_until_closed_or_replaced() -> None:
    clock = Clock()
    registry = SessionRegistry(clock=clock)
    record = _open(registry, lease_ms=1000)
    clock.now += 5
    registry.sweep()

    assert registry.get(record.session_id).status == "expired"

    fresh = _open(registry)  # expired session does not block a new one

    assert fresh.status == "active"
    with pytest.raises(SessionNotFound):
        registry.get(record.session_id)


def test_expire_all_marks_every_live_session() -> None:
    registry = SessionRegistry(clock=Clock())
    a = _open(registry, hwnd=1)
    b = _open(registry, hwnd=2)

    records = registry.expire_all("daemon_shutdown")

    assert {r.session_id for r in records} == {a.session_id, b.session_id}
    assert a.status == b.status == "expired"
    assert a.reason == "daemon_shutdown"


# --- state change planning ---------------------------------------------------

def test_plan_state_change_orders_ups_before_downs_and_skips_noops() -> None:
    registry = SessionRegistry(clock=Clock())
    record = _open(registry)
    record.held_keys["s"] = state.HeldEntry(stroke=None, since=0.0)
    record.held_keys["w"] = state.HeldEntry(stroke=None, since=0.0)

    edges, skipped = plan_state_change(
        record,
        down=["w", "a"],
        up=["s", "d"],
        buttons_down=["right"],
        buttons_up=["left"],
    )

    assert [(e.kind, e.name, e.down) for e in edges] == [
        ("key", "s", False),
        ("button", "right", True),
        ("key", "a", True),
    ]
    assert skipped == ["d", "left", "w"]
    assert all(isinstance(e, Edge) for e in edges)
    assert edges[0].stroke is not None and edges[0].stroke.scan_code == 0x1F
    assert edges[1].button is not None and edges[1].button.down_flag == 0x0008


def test_plan_state_change_rejects_conflicting_and_unknown_keys(monkeypatch) -> None:
    registry = SessionRegistry(clock=Clock())
    record = _open(registry)

    with pytest.raises(ValueError, match="both down and up"):
        plan_state_change(record, down=["w"], up=["w"])
    monkeypatch.setattr(state.keys, "_map_vk_to_scancode", lambda vk: None)
    monkeypatch.setattr(state.keys, "_char_to_vk", lambda ch: None)
    with pytest.raises(ValueError, match="unsupported key"):
        plan_state_change(record, down=["not-a-key"])


def test_apply_edges_updates_held_state() -> None:
    clock = Clock()
    registry = SessionRegistry(clock=clock)
    record = _open(registry)
    edges, _ = plan_state_change(record, down=["w"], buttons_down=["left"])

    state.apply_edges(record, edges, now=clock.now)
    assert set(record.held_keys) == {"w"} and set(record.held_buttons) == {"left"}
    assert record.held_keys["w"].since == clock.now

    edges, _ = plan_state_change(record, up=["w"], buttons_up=["left"])
    state.apply_edges(record, edges, now=clock.now)
    assert record.held_keys == {} and record.held_buttons == {}


def test_plan_release_emits_up_edges_for_everything_held() -> None:
    registry = SessionRegistry(clock=Clock())
    record = _open(registry)
    edges, _ = plan_state_change(record, down=["w", "a"], buttons_down=["right"])
    state.apply_edges(record, edges, now=0.0)

    release = plan_release(record)

    assert sorted((e.kind, e.name) for e in release) == [
        ("button", "right"), ("key", "a"), ("key", "w"),
    ]
    assert all(e.down is False for e in release)


# --- watchdog ------------------------------------------------------------------

def test_watchdog_run_once_releases_expired_sessions() -> None:
    clock = Clock()
    registry = SessionRegistry(clock=clock)
    record = _open(registry, lease_ms=1000)
    released = []
    watchdog = Watchdog(registry, on_expire=lambda rec, reason: released.append((rec.session_id, reason)))

    watchdog.run_once()
    assert released == []

    clock.now += 2
    watchdog.run_once()

    assert released == [(record.session_id, "lease_expired")]


def test_watchdog_survives_callback_errors() -> None:
    clock = Clock()
    registry = SessionRegistry(clock=clock)
    _open(registry, hwnd=1, lease_ms=1000)
    _open(registry, hwnd=2, lease_ms=1000)
    seen = []

    def boom(rec, reason):
        seen.append(rec.hwnd)
        raise RuntimeError("SendInput failed")

    watchdog = Watchdog(registry, on_expire=boom)
    clock.now += 2

    watchdog.run_once()  # must not raise

    assert sorted(seen) == [1, 2]


def test_sweep_skips_lease_check_while_a_timeline_is_busy() -> None:
    clock = Clock()
    registry = SessionRegistry(clock=clock)
    record = _open(registry, lease_ms=1000, max_hold_ms=5000)
    record.busy_until = clock.now + 3.0

    clock.now += 2.0
    assert registry.sweep() == []  # lease would have expired, but a timeline owns the session

    clock.now += 1.5
    assert registry.sweep() == [(record, "lease_expired")]


def test_sweep_still_enforces_max_hold_while_busy() -> None:
    clock = Clock()
    registry = SessionRegistry(clock=clock)
    record = _open(registry, lease_ms=1000, max_hold_ms=500)
    record.busy_until = clock.now + 10.0
    record.held_keys["w"] = state.HeldEntry(stroke=None, since=clock.now)

    clock.now += 0.6
    assert registry.sweep() == [(record, "max_hold_exceeded")]
