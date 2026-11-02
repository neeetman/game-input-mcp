"""A running daemon's pipe never reads as "not found" between two calls."""
from __future__ import annotations

import pytest

from game_input_mcp import client as client_mod
from game_input_mcp import ipc

PIPE = r"\\.\pipe\x"


class FakeKernel:
    """CreateFileW fails with the given error codes in order, then succeeds."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.waits = 0

    def CreateFileW(self, *args):
        if self.errors:
            client_mod.ctypes.set_last_error(self.errors.pop(0))
            return client_mod._INVALID_HANDLE_VALUE
        return 1234

    def WaitNamedPipeW(self, name, ms):
        self.waits += 1


def transport(errors):
    t = client_mod._PipeTransport.__new__(client_mod._PipeTransport)
    t._k = FakeKernel(errors)
    return t


def test_a_briefly_missing_pipe_is_retried(monkeypatch):
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    t = transport([client_mod._ERROR_FILE_NOT_FOUND] * 5)
    assert t.open(PIPE) == 1234


def test_a_pipe_missing_past_the_grace_is_daemon_unavailable(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    t = transport([client_mod._ERROR_FILE_NOT_FOUND] * 10_000)
    with pytest.raises(client_mod.DaemonUnavailable, match="not found"):
        t.open(PIPE)
    assert now[0] >= client_mod._PipeTransport.NOT_FOUND_GRACE_S


def test_busy_pipe_waits_twice_then_gives_up():
    t = transport([client_mod._ERROR_PIPE_BUSY] * 3)
    with pytest.raises(client_mod.DaemonUnavailable, match="busy"):
        t.open(PIPE)
    assert t._k.waits == 2


def test_server_creates_the_next_instance_before_handing_one_off(monkeypatch):
    """The listening-instance count never drops to zero: instance n+1 exists before the worker
    for instance n can close it (2026-11-02: a fast call closed the only instance and the next
    client read a running daemon as "pipe not found")."""
    live = set()
    counter = iter(range(1, 100))
    server = ipc.Server(PIPE)
    connects = [0]
    handed = []

    def create(*args):
        h = next(counter)
        live.add(h)
        return h

    def connect(h, overlapped):
        connects[0] += 1
        if connects[0] >= 3:
            server.stop()

    def close(h):
        live.discard(h)

    class Thread:
        def __init__(self, target, args, daemon):
            self.args = args

        def start(self):
            # the worker runs, and closes its instance, at once
            handed.append(self.args[0])
            assert len(live) >= 2, f"only {live} live when handing off {self.args[0]}"
            close(self.args[0])

    monkeypatch.setattr(ipc.win32pipe, "CreateNamedPipe", create)
    monkeypatch.setattr(ipc.win32pipe, "ConnectNamedPipe", connect)
    monkeypatch.setattr(ipc.win32file, "CloseHandle", close)
    monkeypatch.setattr(ipc.threading, "Thread", Thread)
    server.serve_forever(None)
    assert handed == [1, 2, 3] and live == set()
