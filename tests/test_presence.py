from __future__ import annotations

import threading

import pytest

from game_input_mcp import guard, presence
from tests.conftest import FakeTicks


def _monitor(ticks: FakeTicks) -> presence.PresenceMonitor:
    return presence.PresenceMonitor(ticks.last_input_tick, ticks.tick_count)


def _inject(monitor: presence.PresenceMonitor, ticks: FakeTicks, at: int | None) -> None:
    """One of our own injections; `at` is where GetLastInputInfo lands after it
    (None = the OS did not move it, the opposite of what spike S1 measured)."""
    with monitor.injecting():
        if at is not None:
            ticks.last_input = at


def test_first_probe_reports_real_idle() -> None:
    ticks = FakeTicks(now=100_000, last_input=97_000)

    assert _monitor(ticks).idle_ms() == 3000


def test_own_injection_does_not_count_as_the_user() -> None:
    ticks = FakeTicks(now=100_000, last_input=90_000)
    monitor = _monitor(ticks)
    assert monitor.idle_ms() == 10_000

    ticks.now = 100_500
    _inject(monitor, ticks, at=100_500)  # SendInput bumps dwTime
    ticks.now = 101_000

    assert monitor.idle_ms() == 11_000  # still measured from the user's input


def test_user_input_after_our_injection_is_detected() -> None:
    ticks = FakeTicks(now=100_000, last_input=90_000)
    monitor = _monitor(ticks)
    _inject(monitor, ticks, at=100_000)

    ticks.last_input = 100_400  # the user types
    ticks.now = 100_500

    assert monitor.idle_ms() == 100


def test_user_input_between_two_injections_survives_the_overwrite() -> None:
    ticks = FakeTicks(now=100_000, last_input=50_000)
    monitor = _monitor(ticks)
    _inject(monitor, ticks, at=100_000)
    ticks.last_input = 100_100          # user types between our injections...
    ticks.now = 100_200
    _inject(monitor, ticks, at=100_200)  # ...and our next injection overwrites dwTime
    ticks.now = 100_300

    assert monitor.idle_ms() == 200  # the probe before our injection caught it


def test_works_when_injection_does_not_move_the_last_input_tick() -> None:
    ticks = FakeTicks(now=100_000, last_input=90_000)
    monitor = _monitor(ticks)
    _inject(monitor, ticks, at=None)
    assert monitor.idle_ms() == 10_000

    ticks.last_input = 100_000
    assert monitor.idle_ms() == 0


def test_tick_counter_wraps() -> None:
    ticks = FakeTicks(now=5, last_input=0xFFFFFFF0)

    assert _monitor(ticks).idle_ms() == 21


def test_unavailable_signal_is_unknown_not_away() -> None:
    monitor = presence.PresenceMonitor(lambda: None, lambda: 1)

    reading = monitor.reading("focus", 30)

    assert monitor.idle_ms() is None
    assert reading["state"] == "unknown" and reading["user_idle_ms"] is None


def test_reading_states_and_shape() -> None:
    ticks = FakeTicks(now=100_000, last_input=95_000)
    monitor = _monitor(ticks)

    present = monitor.reading("strict", 30)
    ticks.now = 130_000
    away = monitor.reading("strict", 30)

    assert present == {
        "state": "present",
        "user_idle_ms": 5000,
        "threshold_ms": 30_000,
        "policy": "strict",
        "source": "GetLastInputInfo",
    }
    assert away["state"] == "away" and away["user_idle_ms"] == 35_000


def test_concurrent_injection_is_never_mistaken_for_the_user() -> None:
    """Thread A is mid-injection (dwTime already moved, note not yet taken)
    when thread B asks for the idle time: B must wait for A's attribution."""
    ticks = FakeTicks(now=100_000, last_input=50_000)
    monitor = _monitor(ticks)
    assert monitor.idle_ms() == 50_000
    inside = threading.Event()
    release = threading.Event()
    result: dict = {}

    def injector() -> None:
        with monitor.injecting():
            ticks.last_input = 99_990  # our SendInput moved dwTime
            inside.set()
            assert release.wait(5)

    def reader() -> None:
        result["idle"] = monitor.idle_ms()

    a = threading.Thread(target=injector)
    a.start()
    assert inside.wait(5)
    b = threading.Thread(target=reader)
    b.start()
    b.join(0.2)
    assert b.is_alive(), "reader must block while an injection is being attributed"
    release.set()
    a.join(5)
    b.join(5)

    assert result["idle"] == 50_000


@pytest.mark.parametrize(
    ("policy", "state", "op", "expected"),
    [
        ("off", "present", "focus", "allow"),
        ("off", "present", "inject", "allow"),
        ("warn", "present", "focus", "warn"),
        ("warn", "present", "inject", "warn"),
        ("focus", "present", "focus", "refuse"),
        ("focus", "present", "inject", "warn"),
        ("strict", "present", "focus", "refuse"),
        ("strict", "present", "inject", "refuse"),
        ("strict", "away", "focus", "allow"),
        ("focus", "unknown", "focus", "allow"),
        ("warn", "away", "inject", "allow"),
    ],
)
def test_policy_matrix(policy, state, op, expected) -> None:
    reading = {"state": state, "user_idle_ms": 4000, "threshold_ms": 30_000, "policy": policy, "source": "x"}

    outcome = guard.gate_presence(reading, op)

    if expected == "allow":
        assert outcome.ok and not outcome.warnings
    elif expected == "warn":
        assert outcome.ok and outcome.warnings[0]["code"] == "USER_PRESENT"
    else:
        assert not outcome.ok
        error = outcome.error
        assert error["error_code"] == "USER_PRESENT" and error["retryable"] is True
        assert error["details"]["retry_after_ms"] == 26_000
        assert error["details"]["policy"] == policy and error["details"]["op"] == op


def test_unknown_presence_op_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        guard.gate_presence({"state": "present", "policy": "warn"}, "dance")
