"""Daemon-owned input sessions: held key/button state, lease watchdog and
atomic state-change planning.

Everything here is pure logic with an injectable clock so it can be tested
without Win32. The daemon wires ``SessionRegistry`` + ``Watchdog`` to
``win32.send_edges``.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from . import keys
from .keys import ButtonSpec, KeyStroke

log = logging.getLogger("game-input-daemon.sessions")

FOCUS_POLICIES = ("acquire_once", "acquire_each", "none")
LEASE_MS_RANGE = (200, 60_000)
MAX_HOLD_MS_RANGE = (100, 300_000)


class SessionError(RuntimeError):
    pass


class SessionNotFound(SessionError):
    pass


class SessionExists(SessionError):
    def __init__(self, existing: "SessionRecord") -> None:
        super().__init__(f"session {existing.session_id} already owns hwnd {existing.hwnd}")
        self.existing = existing


@dataclass
class HeldEntry:
    stroke: KeyStroke | ButtonSpec | None
    since: float


@dataclass
class SessionRecord:
    session_id: str
    hwnd: int
    pid: int
    focus_policy: str
    lease_ms: int
    max_hold_ms: int
    opened_at: float
    last_heartbeat: float
    held_keys: dict[str, HeldEntry] = field(default_factory=dict)
    held_buttons: dict[str, HeldEntry] = field(default_factory=dict)
    status: str = "active"  # active | paused | expired | closed
    reason: str | None = None
    # While a daemon-executed timeline owns the session the client is blocked
    # in that call and cannot heartbeat; the lease check is suspended until
    # this deadline (max_hold_ms still applies).
    busy_until: float | None = None

    @property
    def live(self) -> bool:
        return self.status in ("active", "paused")

    def held_names(self) -> list[str]:
        return sorted(self.held_keys) + sorted(self.held_buttons)


@dataclass(frozen=True)
class Edge:
    kind: str  # "key" | "button"
    name: str
    down: bool
    stroke: KeyStroke | None = None
    button: ButtonSpec | None = None
    dx: int = 0      # kind == "move"
    dy: int = 0
    delta: int = 0   # kind == "wheel"


class SessionRegistry:
    """Thread-safe registry of live sessions keyed by session id."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[str, SessionRecord] = {}

    @property
    def clock(self) -> Callable[[], float]:
        return self._clock

    def now(self) -> float:
        return self._clock()

    # -- lifecycle ------------------------------------------------------------

    def open(
        self,
        hwnd: int,
        pid: int,
        *,
        focus_policy: str = "acquire_once",
        lease_ms: int = 2000,
        max_hold_ms: int = 30_000,
        takeover: bool = False,
    ) -> tuple[SessionRecord, SessionRecord | None]:
        if focus_policy not in FOCUS_POLICIES:
            raise ValueError(f"focus_policy must be one of {FOCUS_POLICIES}")
        if not LEASE_MS_RANGE[0] <= int(lease_ms) <= LEASE_MS_RANGE[1]:
            raise ValueError(f"lease_ms must be in {LEASE_MS_RANGE}")
        if not MAX_HOLD_MS_RANGE[0] <= int(max_hold_ms) <= MAX_HOLD_MS_RANGE[1]:
            raise ValueError(f"max_hold_ms must be in {MAX_HOLD_MS_RANGE}")
        with self._lock:
            replaced: SessionRecord | None = None
            for record in list(self._records.values()):
                if record.hwnd != hwnd:
                    continue
                if record.live and not takeover:
                    raise SessionExists(record)
                # Expired sessions never block; live ones are taken over.
                if record.live:
                    replaced = record
                record.status = "closed"
                del self._records[record.session_id]
            now = self._clock()
            record = SessionRecord(
                session_id=secrets.token_hex(8),
                hwnd=int(hwnd),
                pid=int(pid),
                focus_policy=focus_policy,
                lease_ms=int(lease_ms),
                max_hold_ms=int(max_hold_ms),
                opened_at=now,
                last_heartbeat=now,
            )
            self._records[record.session_id] = record
            return record, replaced

    def get(self, session_id: str) -> SessionRecord:
        with self._lock:
            record = self._records.get(str(session_id))
        if record is None:
            raise SessionNotFound(f"unknown session {session_id}")
        return record

    def heartbeat(self, session_id: str, lease_ms: int | None = None) -> SessionRecord:
        record = self.get(session_id)
        with self._lock:
            if lease_ms is not None:
                if not LEASE_MS_RANGE[0] <= int(lease_ms) <= LEASE_MS_RANGE[1]:
                    raise ValueError(f"lease_ms must be in {LEASE_MS_RANGE}")
                record.lease_ms = int(lease_ms)
            record.last_heartbeat = self._clock()
        return record

    def close(self, session_id: str) -> SessionRecord:
        with self._lock:
            record = self._records.pop(str(session_id), None)
        if record is None:
            raise SessionNotFound(f"unknown session {session_id}")
        record.status = "closed"
        return record

    def live_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._records.values() if r.live)

    def live_records(self) -> list[SessionRecord]:
        with self._lock:
            return [r for r in self._records.values() if r.live]

    # -- watchdog --------------------------------------------------------------

    def sweep(self) -> list[tuple[SessionRecord, str]]:
        """Mark sessions whose lease or hold budget ran out. Each expiry is
        reported exactly once; the caller releases the held state."""
        now = self._clock()
        expired: list[tuple[SessionRecord, str]] = []
        with self._lock:
            for record in self._records.values():
                if not record.live:
                    continue
                reason = None
                busy = record.busy_until is not None and now < record.busy_until
                if not busy and (now - record.last_heartbeat) * 1000.0 > record.lease_ms:
                    reason = "lease_expired"
                else:
                    for entry in list(record.held_keys.values()) + list(record.held_buttons.values()):
                        if (now - entry.since) * 1000.0 > record.max_hold_ms:
                            reason = "max_hold_exceeded"
                            break
                if reason is not None:
                    record.status = "expired"
                    record.reason = reason
                    expired.append((record, reason))
        return expired

    def expire_all(self, reason: str) -> list[SessionRecord]:
        with self._lock:
            records = [r for r in self._records.values() if r.live]
            for record in records:
                record.status = "expired"
                record.reason = reason
        return records


# --- planning ---------------------------------------------------------------------

def _dedupe(names: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for name in names or ():
        name = str(name)
        if name not in out:
            out.append(name)
    return out


def plan_state_change(
    record: SessionRecord,
    *,
    down: Sequence[str] | None = None,
    up: Sequence[str] | None = None,
    buttons_down: Sequence[str] | None = None,
    buttons_up: Sequence[str] | None = None,
    mode: str = "scancode",
) -> tuple[list[Edge], list[str]]:
    """Return (edges, skipped). Edges are ordered ups first, then downs, so a
    single SendInput batch never briefly holds opposed keys. Names already in
    the requested state are skipped. Raises ValueError on conflicts or
    unresolvable names before anything is planned."""
    down_keys = _dedupe(down)
    up_keys = _dedupe(up)
    down_buttons = _dedupe(buttons_down)
    up_buttons = _dedupe(buttons_up)

    resolved_down = {name: keys.resolve_key(name, mode) for name in down_keys}
    resolved_up = {name: keys.resolve_key(name, mode) for name in up_keys}
    both = {s.key for s in resolved_down.values()} & {s.key for s in resolved_up.values()}
    if both:
        raise ValueError(f"keys requested both down and up: {sorted(both)}")
    button_down_specs = {name: keys.resolve_button(name) for name in down_buttons}
    button_up_specs = {name: keys.resolve_button(name) for name in up_buttons}
    both_buttons = {s.name for s in button_down_specs.values()} & {s.name for s in button_up_specs.values()}
    if both_buttons:
        raise ValueError(f"buttons requested both down and up: {sorted(both_buttons)}")

    edges: list[Edge] = []
    skipped: list[str] = []

    for name, stroke in resolved_up.items():
        if stroke.key in record.held_keys:
            edges.append(Edge("key", stroke.key, False, stroke=stroke))
        else:
            skipped.append(name)
    for name, spec in button_up_specs.items():
        if spec.name in record.held_buttons:
            edges.append(Edge("button", spec.name, False, button=spec))
        else:
            skipped.append(name)
    for name, spec in button_down_specs.items():
        if spec.name in record.held_buttons:
            skipped.append(name)
        else:
            edges.append(Edge("button", spec.name, True, button=spec))
    for name, stroke in resolved_down.items():
        if stroke.key in record.held_keys:
            skipped.append(name)
        else:
            edges.append(Edge("key", stroke.key, True, stroke=stroke))
    return edges, sorted(skipped)


def plan_release(record: SessionRecord) -> list[Edge]:
    edges: list[Edge] = []
    for name, entry in record.held_keys.items():
        stroke = entry.stroke if isinstance(entry.stroke, KeyStroke) else keys.resolve_key(name)
        edges.append(Edge("key", name, False, stroke=stroke))
    for name, entry in record.held_buttons.items():
        spec = entry.stroke if isinstance(entry.stroke, ButtonSpec) else keys.resolve_button(name)
        edges.append(Edge("button", name, False, button=spec))
    return edges


def apply_edges(record: SessionRecord, edges: Sequence[Edge], now: float) -> None:
    for edge in edges:
        if edge.kind not in ("key", "button"):
            continue
        table = record.held_keys if edge.kind == "key" else record.held_buttons
        if edge.down:
            table[edge.name] = HeldEntry(stroke=edge.stroke or edge.button, since=now)
        else:
            table.pop(edge.name, None)


# --- watchdog -----------------------------------------------------------------------

class Watchdog:
    """Periodically sweeps the registry and hands expired sessions to
    ``on_expire(record, reason)``, which must release their held state."""

    def __init__(
        self,
        registry: SessionRegistry,
        on_expire: Callable[[SessionRecord, str], None],
        interval_s: float = 0.05,
    ) -> None:
        self._registry = registry
        self._on_expire = on_expire
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> int:
        expired = self._registry.sweep()
        for record, reason in expired:
            try:
                self._on_expire(record, reason)
            except Exception:  # noqa: BLE001 — watchdog must keep running
                log.exception("release failed for session %s (%s)", record.session_id, reason)
        return len(expired)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            self.run_once()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="input-watchdog", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
