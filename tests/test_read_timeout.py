"""The clients stop waiting for a daemon that does not answer. These use a real
named pipe (neither client can time out a plain synchronous ReadFile; they poll
PeekNamedPipe), with a tiny server that replies after a chosen delay."""
from __future__ import annotations

import threading
import time
import uuid

import pytest
import pywintypes
import win32file
import win32pipe

from game_input_mcp import client as stdlib_client
from game_input_mcp import ipc


class PipeServer:
    """Answers one request per instance after ``delay_s`` (None = never answers)."""

    def __init__(self, delay_s: float | None) -> None:
        self.name = rf"\\.\pipe\game-input-test-{uuid.uuid4().hex}"
        self.delay_s = delay_s
        self.requests: list[dict] = []
        self._pipe = win32pipe.CreateNamedPipe(
            self.name,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1, 65536, 65536, 0, None,
        )
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            win32pipe.ConnectNamedPipe(self._pipe, None)
            request = ipc.read_frame(self._pipe)
            self.requests.append(request)
            if self.delay_s is None:
                self._release.wait(10)
                return
            time.sleep(self.delay_s)
            ipc.write_frame(
                self._pipe, {"id": request["id"], "ok": True, "result": {"success": True, "echo": request["params"]}}
            )
            self._release.wait(1)
        except (pywintypes.error, EOFError):
            pass  # the client gave up and closed its end

    def close(self) -> None:
        self._release.set()
        self._thread.join(2)
        try:
            win32file.CloseHandle(self._pipe)
        except pywintypes.error:
            pass


@pytest.fixture
def pipe():
    servers: list[PipeServer] = []

    def make(delay_s):
        server = PipeServer(delay_s)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def test_ipc_client_gives_up_on_a_silent_daemon(pipe) -> None:
    server = pipe(None)

    started = time.monotonic()
    with pytest.raises(ipc.DaemonTimeout):
        ipc.Client(server.name).call("hang", read_timeout_s=0.3)

    assert 0.25 <= time.monotonic() - started < 2.0
    assert server.requests[0]["method"] == "hang"
    assert "read_timeout_s" not in server.requests[0]["params"]  # never forwarded


def test_ipc_client_returns_a_prompt_answer(pipe) -> None:
    server = pipe(0.05)

    result = ipc.Client(server.name).call("quick", level=3, read_timeout_s=2.0)

    assert result == {"success": True, "echo": {"level": 3}}


def test_ipc_client_waits_for_a_slow_answer_when_given_enough_time(pipe) -> None:
    server = pipe(0.6)

    assert ipc.Client(server.name).call("slow", read_timeout_s=3.0)["success"] is True


def test_ipc_client_without_a_timeout_waits_as_before(pipe) -> None:
    server = pipe(0.4)

    assert ipc.Client(server.name).call("slow", read_timeout_s=None)["success"] is True


def test_ipc_default_bound_is_thirty_seconds_and_timelines_get_slack() -> None:
    assert ipc.Client.DEFAULT_READ_TIMEOUT_S == 30.0
    assert ipc.Client.LONG_CALL_SLACK_S > 0
    assert stdlib_client.Client.DEFAULT_READ_TIMEOUT_S == 30.0


def test_stdlib_client_reports_daemon_timeout_as_a_structured_error(pipe) -> None:
    server = pipe(None)

    started = time.monotonic()
    with pytest.raises(stdlib_client.InputError) as caught:
        stdlib_client.Client(server.name).call("hang", read_timeout_s=0.3)

    assert caught.value.code == "DAEMON_TIMEOUT"
    assert 0.25 <= time.monotonic() - started < 2.0
    assert "read_timeout_s" not in server.requests[0]["params"]


def test_stdlib_client_returns_a_prompt_answer_and_honours_a_longer_budget(pipe) -> None:
    fast = pipe(0.05)
    slow = pipe(0.6)

    assert stdlib_client.Client(fast.name).call("quick", read_timeout_s=2.0)["success"] is True
    assert stdlib_client.Client(slow.name).call("slow", read_timeout_s=3.0)["success"] is True


def test_session_helpers_budget_the_timeline_length() -> None:
    seen: list[dict] = []

    class Recorder:
        LONG_CALL_SLACK_S = stdlib_client.Client.LONG_CALL_SLACK_S

        def invoke(self, method, **params):
            seen.append({"method": method, **params})
            return {"success": True}

    session = stdlib_client.InputSession(Recorder(), {"session_id": "s", "lease_ms": 2000}, heartbeat=False)

    session.run_timeline([], total_ms=2000)
    session.look(1, 0, duration_ms=800)

    assert seen[0]["read_timeout_s"] == pytest.approx(2.0 + stdlib_client.Client.LONG_CALL_SLACK_S)
    assert seen[1]["read_timeout_s"] == pytest.approx(0.8 + stdlib_client.Client.LONG_CALL_SLACK_S)


def test_server_turns_a_daemon_timeout_into_a_structured_error(monkeypatch) -> None:
    from game_input_mcp import server

    def hang(method, **params):
        raise ipc.DaemonTimeout("no answer")

    monkeypatch.setattr(server._client, "call", hang)

    result = server._call("list_targets")

    assert result["success"] is False and result["error_code"] == "DAEMON_TIMEOUT"
    assert result["retryable"] is False and result["details"] == {"method": "list_targets"}
