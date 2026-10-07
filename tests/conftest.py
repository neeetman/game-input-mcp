from __future__ import annotations

import pytest

from game_input_mcp import daemon, presence


class FakeTicks:
    """Deterministic stand-in for GetLastInputInfo/GetTickCount."""

    def __init__(self, now: int = 1_000_000, last_input: int | None = None) -> None:
        self.now = now
        self.last_input = last_input if last_input is not None else 0

    def last_input_tick(self) -> int | None:
        return self.last_input

    def tick_count(self) -> int:
        return self.now


@pytest.fixture(autouse=True)
def user_is_away(monkeypatch):
    """Daemon tests must not depend on whether a human is at this machine:
    by default the user last touched the keyboard 1000 s ago. Tests that care
    about presence take the `ticks` fixture and move the clock themselves."""
    ticks = FakeTicks(now=1_000_000, last_input=0)
    monitor = presence.PresenceMonitor(ticks.last_input_tick, ticks.tick_count)
    monkeypatch.setattr(daemon, "PRESENCE", monitor)
    return ticks


@pytest.fixture
def ticks(user_is_away):
    return user_is_away


class _NoHdr:
    def inspect(self, rect, exe):
        return {"display_hdr": False, "auto_hdr": None, "auto_hdr_scope": None}


@pytest.fixture(autouse=True)
def hermetic_capture_diagnostics(monkeypatch):
    """Capture results must not depend on this machine's HDR setting or on what
    an earlier test captured: no HDR, empty frame history."""
    from game_input_mcp.capture import diagnostics, service

    monkeypatch.setattr(service, "HDR", _NoHdr())
    monkeypatch.setattr(diagnostics, "HISTORY", diagnostics.FrameHistory())
