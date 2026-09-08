from __future__ import annotations

import threading
import time

import pytest

from game_input_mcp.input import state
from game_input_mcp.input.state import SessionRegistry
from game_input_mcp.input.timeline import (
    Batch,
    TimelineRunner,
    compile_timeline,
    expand_look,
)


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _record(**kwargs):
    registry = SessionRegistry(clock=Clock())
    record, _ = registry.open(hwnd=1, pid=2, **kwargs)
    return record


# --- compile -----------------------------------------------------------------------

def test_compile_coalesces_equal_timestamps_with_ups_first() -> None:
    record = _record()
    events = [
        {"t_ms": 0, "op": "down", "key": "w"},
        {"t_ms": 500, "op": "down", "key": "a"},
        {"t_ms": 500, "op": "up", "key": "w"},
        {"t_ms": 500, "op": "button_down", "button": "right"},
        {"t_ms": 900, "op": "up", "key": "a"},
        {"t_ms": 900, "op": "button_up", "button": "right"},
    ]

    batches = compile_timeline(record, events, total_ms=1000)

    assert [b.t_ms for b in batches] == [0.0, 500.0, 900.0]
    assert [(e.edge.kind, e.edge.name, e.edge.down) for e in batches[1].events] == [
        ("key", "w", False),
        ("key", "a", True),
        ("button", "right", True),
    ]
    assert [e.index for e in batches[1].events] == [2, 1, 3]
    assert all(isinstance(b, Batch) for b in batches)


def test_compile_requires_matching_up_unless_dangling_allowed() -> None:
    record = _record()
    events = [{"t_ms": 0, "op": "down", "key": "w"}]

    with pytest.raises(ValueError, match="still down"):
        compile_timeline(record, events, total_ms=1000)

    batches = compile_timeline(record, events, total_ms=1000, allow_dangling=True)
    assert len(batches) == 1


def test_compile_rejects_inconsistent_key_state() -> None:
    record = _record()
    with pytest.raises(ValueError, match="not down"):
        compile_timeline(record, [{"t_ms": 0, "op": "up", "key": "w"}], total_ms=100)
    with pytest.raises(ValueError, match="already down"):
        compile_timeline(
            record,
            [
                {"t_ms": 0, "op": "down", "key": "w"},
                {"t_ms": 10, "op": "down", "key": "w"},
                {"t_ms": 20, "op": "up", "key": "w"},
            ],
            total_ms=100,
        )


def test_compile_honours_keys_already_held_by_the_session() -> None:
    record = _record()
    record.held_keys["w"] = state.HeldEntry(stroke=None, since=0.0)

    batches = compile_timeline(record, [{"t_ms": 0, "op": "up", "key": "w"}], total_ms=100)

    assert batches[0].events[0].edge.down is False


def test_compile_validates_bounds_and_ops() -> None:
    record = _record(max_hold_ms=5000)
    with pytest.raises(ValueError, match="total_ms"):
        compile_timeline(record, [], total_ms=0)
    with pytest.raises(ValueError, match="max_hold_ms"):
        compile_timeline(record, [], total_ms=6000)
    with pytest.raises(ValueError, match="t_ms"):
        compile_timeline(record, [{"t_ms": 2000, "op": "wheel", "delta": 120}], total_ms=1000)
    with pytest.raises(ValueError, match="t_ms"):
        compile_timeline(record, [{"t_ms": -1, "op": "wheel", "delta": 120}], total_ms=1000)
    with pytest.raises(ValueError, match="unknown op"):
        compile_timeline(record, [{"t_ms": 0, "op": "teleport"}], total_ms=1000)
    with pytest.raises(ValueError, match="unsupported key"):
        compile_timeline(
            record,
            [{"t_ms": 0, "op": "down", "key": "not-a-key"}, {"t_ms": 1, "op": "up", "key": "not-a-key"}],
            total_ms=1000,
        )
    with pytest.raises(ValueError, match="events"):
        compile_timeline(record, [{"t_ms": 0}] * 5001, total_ms=1000)


def test_compile_wheel_and_single_move() -> None:
    record = _record()
    batches = compile_timeline(
        record,
        [
            {"t_ms": 10, "op": "wheel", "delta": -120},
            {"t_ms": 20, "op": "look", "dx": 15, "dy": -3},
        ],
        total_ms=100,
    )

    wheel = batches[0].events[0].edge
    move = batches[1].events[0].edge
    assert wheel.kind == "wheel" and wheel.delta == -120
    assert move.kind == "move" and (move.dx, move.dy) == (15, -3)


# --- look expansion ------------------------------------------------------------------

def test_expand_look_integer_deltas_sum_exactly() -> None:
    steps = expand_look(dx=1000, dy=-333, duration_ms=1000, rate_hz=250)

    assert len(steps) == 250
    assert sum(dx for _, dx, _ in steps) == 1000
    assert sum(dy for _, _, dy in steps) == -333
    offsets = [t for t, _, _ in steps]
    assert offsets[0] == 0.0
    assert offsets[-1] == pytest.approx(996.0)
    assert all(abs(dy) <= 2 for _, _, dy in steps)


def test_expand_look_zero_duration_is_one_step() -> None:
    assert expand_look(dx=7, dy=0, duration_ms=0, rate_hz=250) == [(0.0, 7, 0)]


def test_expand_look_validates() -> None:
    with pytest.raises(ValueError, match="rate_hz"):
        expand_look(1, 1, 100, rate_hz=0)
    with pytest.raises(ValueError, match="rate_hz"):
        expand_look(1, 1, 100, rate_hz=5000)
    with pytest.raises(ValueError, match="duration_ms"):
        expand_look(1, 1, -1, rate_hz=100)


def test_compile_expands_look_into_sub_events_sharing_the_source_index() -> None:
    record = _record()
    batches = compile_timeline(
        record,
        [
            {"t_ms": 100, "op": "look", "dx": 40, "dy": 0, "duration_ms": 40, "rate_hz": 100},
            {"t_ms": 120, "op": "down", "key": "space"},
            {"t_ms": 130, "op": "up", "key": "space"},
        ],
        total_ms=500,
    )

    assert [b.t_ms for b in batches] == [100.0, 110.0, 120.0, 130.0]
    assert [e.edge.kind for e in batches[2].events] == ["key", "move"]
    assert {e.index for b in batches for e in b.events if e.edge.kind == "move"} == {0}
    assert sum(e.edge.dx for b in batches for e in b.events if e.edge.kind == "move") == 40


# --- runner ----------------------------------------------------------------------------

class FakeClock:
    """Clock that only advances when the runner waits."""

    def __init__(self) -> None:
        self.now = 10.0
        self.waits: list[float] = []

    def __call__(self) -> float:
        return self.now

    def wait(self, abort: threading.Event, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now += seconds
        return abort.is_set()


def _batches(record, events, total_ms, **kw):
    return compile_timeline(record, events, total_ms=total_ms, **kw)


def test_runner_fires_batches_at_schedule_and_reports_timestamps() -> None:
    record = _record()
    clock = FakeClock()
    sent: list[tuple[float, list[str]]] = []

    def send(edges):
        sent.append((clock.now, [f"{e.name}:{'d' if e.down else 'u'}" for e in edges]))
        return len(edges)

    runner = TimelineRunner(send=send, clock=clock, wait=clock.wait, spin_margin_s=0.0, qpc_ns=lambda: int(clock.now * 1e9))
    batches = _batches(
        record,
        [
            {"t_ms": 0, "op": "down", "key": "w"},
            {"t_ms": 250, "op": "down", "key": "a"},
            {"t_ms": 250, "op": "up", "key": "w"},
            {"t_ms": 600, "op": "up", "key": "a"},
        ],
        total_ms=1000,
    )

    result = runner.run(batches, total_ms=1000, abort=threading.Event())

    assert result.stopped_reason == "completed"
    assert [round(t - 10.0, 3) for t, _ in sent] == [0.0, 0.25, 0.6]
    assert sent[1][1] == ["w:u", "a:d"]
    assert [round(b.actual_ms, 3) for b in result.batches] == [0.0, 250.0, 600.0]
    assert [b.sent for b in result.batches] == [1, 2, 1]
    assert result.batches[1].qpc_ns == int(10.25 * 1e9)
    assert result.ended_ms == pytest.approx(1000.0)  # waits out the whole window
    assert [e.index for e in result.batches[1].events] == [2, 1]


def test_runner_abort_stops_before_next_batch() -> None:
    record = _record()
    clock = FakeClock()
    abort = threading.Event()
    sent = []

    def send(edges):
        sent.append([e.name for e in edges])
        if len(sent) == 1:
            abort.set()  # aborted right after the first batch fires
        return len(edges)

    runner = TimelineRunner(send=send, clock=clock, wait=clock.wait, spin_margin_s=0.0)
    batches = _batches(
        record,
        [
            {"t_ms": 0, "op": "down", "key": "w"},
            {"t_ms": 500, "op": "up", "key": "w"},
        ],
        total_ms=1000,
    )

    result = runner.run(batches, total_ms=1000, abort=abort)

    assert result.stopped_reason == "aborted"
    assert sent == [["w"]]
    assert len(result.batches) == 1
    assert result.pending_indices == [1]


def test_runner_stops_on_focus_loss_between_batches() -> None:
    record = _record()
    clock = FakeClock()
    fg = {"ok": True}
    sent = []

    def send(edges):
        sent.append([e.name for e in edges])
        fg["ok"] = False
        return len(edges)

    runner = TimelineRunner(send=send, clock=clock, wait=clock.wait, spin_margin_s=0.0, foreground_ok=lambda: fg["ok"])
    batches = _batches(
        record,
        [
            {"t_ms": 0, "op": "down", "key": "w"},
            {"t_ms": 100, "op": "up", "key": "w"},
        ],
        total_ms=200,
    )

    result = runner.run(batches, total_ms=200, abort=threading.Event())

    assert result.stopped_reason == "focus_lost"
    assert sent == [["w"]]


def test_runner_calls_on_batch_hook_after_each_send() -> None:
    record = _record()
    clock = FakeClock()
    seen = []
    runner = TimelineRunner(send=lambda edges: len(edges), clock=clock, wait=clock.wait, spin_margin_s=0.0)
    batches = _batches(
        record,
        [{"t_ms": 0, "op": "down", "key": "w"}, {"t_ms": 50, "op": "up", "key": "w"}],
        total_ms=100,
    )

    runner.run(batches, total_ms=100, abort=threading.Event(), on_batch=lambda batch, edges: seen.append(len(edges)))

    assert seen == [1, 1]


@pytest.mark.slow
def test_runner_real_clock_precision_is_sub_two_ms() -> None:
    record = _record()
    actual: list[float] = []
    runner = TimelineRunner(send=lambda edges: len(edges))
    events = []
    for i in range(10):
        events.append({"t_ms": 20 * i, "op": "down", "key": "w"})
        events.append({"t_ms": 20 * i + 10, "op": "up", "key": "w"})
    batches = compile_timeline(record, events, total_ms=200)

    with runner.precise_timing():
        result = runner.run(batches, total_ms=200, abort=threading.Event())

    errors = [abs(b.actual_ms - b.t_ms) for b in result.batches]
    assert result.stopped_reason == "completed"
    assert max(errors) < 2.0, errors
    assert len(result.batches) == 20
