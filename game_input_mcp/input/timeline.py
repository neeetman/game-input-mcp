"""Daemon-executed input timelines.

A timeline is a list of scheduled edges (key/button down/up, relative mouse
look, wheel) that the daemon injects from one high-resolution scheduler
thread. Equal timestamps are coalesced into one ``SendInput`` batch, every
batch is stamped with QPC time, and the run can be aborted or stopped by
focus loss between batches.

``compile_timeline`` and ``TimelineRunner`` are pure logic with injectable
clock/wait/send so they are testable without Win32; ``precise_timing`` is the
only Windows-specific piece.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

from . import keys
from .state import Edge, SessionRecord

MAX_EVENTS = 5000
MAX_RATE_HZ = 2000
_UP, _DOWN, _MOVE, _WHEEL = 0, 1, 2, 3


@dataclass(frozen=True)
class TimelineEvent:
    index: int  # index of the source event in the request
    t_ms: float
    edge: Edge


@dataclass
class Batch:
    t_ms: float
    events: list[TimelineEvent]


@dataclass
class BatchResult:
    t_ms: float
    actual_ms: float
    qpc_ns: int
    sent: int
    expected: int
    events: list[TimelineEvent]

    def to_dict(self) -> dict[str, Any]:
        return {
            "t_ms": self.t_ms,
            "actual_ms": round(self.actual_ms, 3),
            "qpc_ns": self.qpc_ns,
            "sent": self.sent,
            "expected": self.expected,
            "events": [
                {"index": e.index, "kind": e.edge.kind, "name": e.edge.name, "down": e.edge.down}
                for e in self.events
            ],
        }


@dataclass
class RunResult:
    stopped_reason: str  # completed | aborted | focus_lost
    started_qpc_ns: int
    batches: list[BatchResult]
    pending_indices: list[int]
    ended_ms: float


# --- expansion / compilation ------------------------------------------------------

def expand_look(dx: int, dy: int, duration_ms: float, rate_hz: float = 250) -> list[tuple[float, int, int]]:
    """Spread (dx, dy) over duration_ms at rate_hz as integer sub-moves whose
    sum is exactly (dx, dy). Returns [(offset_ms, sdx, sdy), ...]."""
    if not 0 < float(rate_hz) <= MAX_RATE_HZ:
        raise ValueError(f"rate_hz must be in (0, {MAX_RATE_HZ}]")
    if float(duration_ms) < 0:
        raise ValueError("duration_ms must be non-negative")
    dx, dy = int(dx), int(dy)
    if float(duration_ms) == 0:
        return [(0.0, dx, dy)]
    n = max(1, round(float(duration_ms) * float(rate_hz) / 1000.0))
    steps: list[tuple[float, int, int]] = []
    px = py = 0
    for k in range(n):
        cx = round(dx * (k + 1) / n)
        cy = round(dy * (k + 1) / n)
        steps.append((k * float(duration_ms) / n, cx - px, cy - py))
        px, py = cx, cy
    return steps


def _rank(edge: Edge) -> int:
    if edge.kind in ("key", "button"):
        return _DOWN if edge.down else _UP
    return _MOVE if edge.kind == "move" else _WHEEL


def compile_timeline(
    record: SessionRecord,
    events: Sequence[dict[str, Any]],
    *,
    total_ms: float,
    allow_dangling: bool = False,
) -> list[Batch]:
    """Validate and coalesce wire events into ordered batches. Fails closed on
    anything inconsistent before a single edge is planned."""
    total_ms = float(total_ms)
    if not total_ms > 0:
        raise ValueError("total_ms must be positive")
    if total_ms > record.max_hold_ms:
        raise ValueError("total_ms exceeds the session max_hold_ms")
    if len(events) > MAX_EVENTS:
        raise ValueError(f"too many events (max {MAX_EVENTS})")

    raw: list[tuple[float, int, int, int, Edge]] = []  # (t_ms, rank, position, index, edge)
    position = 0

    def add(t: float, index: int, edge: Edge) -> None:
        nonlocal position
        if not 0.0 <= t <= total_ms:
            raise ValueError(f"event {index} t_ms {t} outside [0, {total_ms}]")
        raw.append((t, _rank(edge), position, index, edge))
        position += 1

    for index, ev in enumerate(events):
        if "t_ms" not in ev:
            raise ValueError(f"event {index} is missing t_ms")
        t = float(ev["t_ms"])
        op = str(ev.get("op", ""))
        if op in ("down", "up"):
            stroke = keys.resolve_key(str(ev["key"]), str(ev.get("mode", "scancode")))
            add(t, index, Edge("key", stroke.key, op == "down", stroke=stroke))
        elif op in ("button_down", "button_up"):
            spec = keys.resolve_button(str(ev["button"]))
            add(t, index, Edge("button", spec.name, op == "button_down", button=spec))
        elif op == "look":
            steps = expand_look(
                int(ev.get("dx", 0)),
                int(ev.get("dy", 0)),
                float(ev.get("duration_ms", 0)),
                float(ev.get("rate_hz", 250)),
            )
            for offset, sdx, sdy in steps:
                add(t + offset, index, Edge("move", "look", True, dx=sdx, dy=sdy))
        elif op == "wheel":
            add(t, index, Edge("wheel", "wheel", True, delta=int(ev["delta"])))
        else:
            raise ValueError(f"event {index}: unknown op {op!r}")

    raw.sort(key=lambda item: (item[0], item[1], item[2]))
    batches: list[Batch] = []
    for t, _rank_, _pos, index, edge in raw:
        if batches and batches[-1].t_ms == t:
            batches[-1].events.append(TimelineEvent(index, t, edge))
        else:
            batches.append(Batch(t, [TimelineEvent(index, t, edge)]))

    _check_state(record, batches, allow_dangling)
    return batches


def _check_state(record: SessionRecord, batches: list[Batch], allow_dangling: bool) -> None:
    held_keys = set(record.held_keys)
    held_buttons = set(record.held_buttons)
    pressed: set[tuple[str, str]] = set()
    for batch in batches:
        for ev in batch.events:
            edge = ev.edge
            if edge.kind not in ("key", "button"):
                continue
            held = held_keys if edge.kind == "key" else held_buttons
            if edge.down:
                if edge.name in held:
                    raise ValueError(f"{edge.kind} {edge.name!r} already down at t_ms {batch.t_ms}")
                held.add(edge.name)
                pressed.add((edge.kind, edge.name))
            else:
                if edge.name not in held:
                    raise ValueError(f"{edge.kind} {edge.name!r} is not down at t_ms {batch.t_ms}")
                held.discard(edge.name)
    if not allow_dangling:
        dangling = sorted(
            name for kind, name in pressed
            if name in (held_keys if kind == "key" else held_buttons)
        )
        if dangling:
            raise ValueError(f"{dangling} still down at the end of the timeline (pass allow_dangling)")


# --- runner --------------------------------------------------------------------------

def _default_wait(abort: threading.Event, seconds: float) -> bool:
    return abort.wait(seconds)


class TimelineRunner:
    """Fires batches at their scheduled offsets from one thread: coarse waits
    on the abort event until spin_margin_s before the deadline, then spins on
    the clock. Stops early on abort or focus loss."""

    def __init__(
        self,
        *,
        send: Callable[[list[Edge]], int],
        clock: Callable[[], float] = time.perf_counter,
        wait: Callable[[threading.Event, float], bool] | None = None,
        qpc_ns: Callable[[], int] = time.perf_counter_ns,
        foreground_ok: Callable[[], bool] | None = None,
        spin_margin_s: float = 0.002,
        max_wait_chunk_s: float = 0.05,
    ) -> None:
        self._send = send
        self._clock = clock
        self._wait = wait or _default_wait
        self._qpc_ns = qpc_ns
        self._foreground_ok = foreground_ok
        self._spin_margin = spin_margin_s
        self._max_chunk = max_wait_chunk_s

    def _wait_until(self, deadline: float, abort: threading.Event) -> bool:
        """Return True if aborted, False when the deadline has been reached."""
        while True:
            if abort.is_set():
                return True
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            if remaining > self._spin_margin:
                if self._wait(abort, min(remaining - self._spin_margin, self._max_chunk)):
                    return True
            # else: spin until the deadline, re-checking abort each pass

    def run(
        self,
        batches: Sequence[Batch],
        total_ms: float,
        abort: threading.Event,
        on_batch: Callable[[Batch, list[Edge]], None] | None = None,
    ) -> RunResult:
        started = self._clock()
        started_qpc = self._qpc_ns()
        results: list[BatchResult] = []
        reason = "completed"
        for batch in batches:
            if self._wait_until(started + batch.t_ms / 1000.0, abort):
                reason = "aborted"
                break
            if self._foreground_ok is not None and not self._foreground_ok():
                reason = "focus_lost"
                break
            edges = [e.edge for e in batch.events]
            sent = self._send(edges)
            now = self._clock()
            results.append(BatchResult(batch.t_ms, (now - started) * 1000.0, self._qpc_ns(), sent, len(edges), list(batch.events)))
            if on_batch is not None:
                on_batch(batch, edges)
        else:
            if self._wait_until(started + float(total_ms) / 1000.0, abort):
                reason = "aborted"
        pending: list[int] = []
        for batch in batches[len(results):]:
            for e in batch.events:
                if e.index not in pending:
                    pending.append(e.index)
        return RunResult(reason, started_qpc, results, pending, (self._clock() - started) * 1000.0)

    @staticmethod
    @contextmanager
    def precise_timing() -> Iterator[None]:
        """1 ms system timer resolution + time-critical thread priority for the
        duration of a run. No-op where the Win32 calls are unavailable."""
        import ctypes

        winmm = kernel32 = None
        old_priority = None
        try:
            winmm = ctypes.WinDLL("winmm")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            winmm.timeBeginPeriod(1)
            thread = kernel32.GetCurrentThread()
            old_priority = kernel32.GetThreadPriority(thread)
            kernel32.SetThreadPriority(thread, 15)  # THREAD_PRIORITY_TIME_CRITICAL
        except (OSError, AttributeError):
            winmm = None
        try:
            yield
        finally:
            if kernel32 is not None and old_priority is not None:
                try:
                    kernel32.SetThreadPriority(kernel32.GetCurrentThread(), old_priority)
                except (OSError, AttributeError):
                    pass
            if winmm is not None:
                try:
                    winmm.timeEndPeriod(1)
                except (OSError, AttributeError):
                    pass
