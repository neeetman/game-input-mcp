"""User-presence detection that tells the human's input from our own.

``GetLastInputInfo`` reports the last input event on the desktop session, and
our own ``SendInput`` moves it too (verified on this machine in spike S1), so a
naive idle check would see the daemon as "the user". The monitor therefore
attributes every change of ``dwTime`` to us or to someone else:

* ``injecting()`` brackets each injection: probe before, note after, all under
  one lock so a concurrent injection is never misread as human input;
* a change of ``dwTime`` that is not our post-injection value is human (or
  another injector, which we treat the same way).

Pure logic with injectable tick sources; the daemon wires the Win32 calls and
installs ``injecting`` as the ``win32`` injection hook.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator

TICK_MASK = 0xFFFFFFFF
SOURCE = "GetLastInputInfo"


class PresenceMonitor:
    def __init__(
        self,
        last_input_tick: Callable[[], int | None],
        now_tick: Callable[[], int],
    ) -> None:
        self._last_input_tick = last_input_tick
        self._now_tick = now_tick
        self._lock = threading.RLock()
        self._own_tick: int | None = None
        self._human_tick: int | None = None

    # -- attribution ------------------------------------------------------------

    def _probe_locked(self) -> None:
        cur = self._last_input_tick()
        if cur is None:
            return
        if cur != self._own_tick and cur != self._human_tick:
            self._human_tick = cur

    @contextmanager
    def injecting(self) -> Iterator[None]:
        """Bracket one injection (SendInput / keybd_event)."""
        with self._lock:
            self._probe_locked()
            try:
                yield
            finally:
                cur = self._last_input_tick()
                if cur is not None:
                    self._own_tick = cur

    # -- reading ----------------------------------------------------------------

    def idle_ms(self) -> int | None:
        """Milliseconds since the last input that was not ours, or None when
        the signal is unavailable."""
        with self._lock:
            self._probe_locked()
            human = self._human_tick
        if human is None:
            return None
        return (self._now_tick() - human) & TICK_MASK

    def reading(self, policy: str, threshold_s: float) -> dict:
        threshold_ms = int(round(float(threshold_s) * 1000.0))
        idle = self.idle_ms()
        if idle is None:
            state = "unknown"
        elif idle < threshold_ms:
            state = "present"
        else:
            state = "away"
        return {
            "state": state,
            "user_idle_ms": idle,
            "threshold_ms": threshold_ms,
            "policy": policy,
            "source": SOURCE,
        }
