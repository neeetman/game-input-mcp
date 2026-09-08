from __future__ import annotations

import json
import struct
import time

import pytest

from game_input_mcp import client as client_mod
from game_input_mcp.client import Client, DaemonUnavailable, InputError, ProtocolError


def _frame(obj: dict) -> bytes:
    payload = json.dumps(obj).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


class FakeTransport:
    """In-memory pipe: parses each request frame and answers via `responder`."""

    def __init__(self, responder) -> None:
        self.responder = responder
        self.requests: list[dict] = []
        self.opened = 0
        self.closed = 0

    def open(self, name):
        self.opened += 1
        return {"in": bytearray(), "out": b""}

    def write(self, handle, data):
        handle["in"] += data
        (length,) = struct.unpack(">I", bytes(handle["in"][:4]))
        if len(handle["in"]) >= 4 + length:
            request = json.loads(bytes(handle["in"][4:4 + length]))
            self.requests.append(request)
            handle["out"] = _frame(self.responder(request))

    def read(self, handle, n):
        chunk = handle["out"][:n]
        handle["out"] = handle["out"][n:]
        if not chunk:
            raise EOFError("closed")
        return chunk

    def close(self, handle):
        self.closed += 1


def _ok(result):
    return lambda req: {"id": req["id"], "ok": True, "result": result}


def _client(responder) -> tuple[Client, FakeTransport]:
    transport = FakeTransport(responder)
    return Client(name=r"\\.\pipe\test", transport=transport), transport


# --- transport / framing ------------------------------------------------------------

def test_call_round_trip_uses_one_connection() -> None:
    client, transport = _client(_ok({"success": True, "targets": []}))

    result = client.call("list_targets", verbose=True)

    assert result == {"success": True, "targets": []}
    assert transport.requests == [{"id": 1, "method": "list_targets", "params": {"verbose": True}}]
    assert transport.opened == transport.closed == 1


def test_call_detects_id_mismatch_and_daemon_errors() -> None:
    client, _ = _client(lambda req: {"id": 999, "ok": True, "result": {}})
    with pytest.raises(ProtocolError):
        client.call("x")

    client, _ = _client(lambda req: {"id": req["id"], "ok": False, "error": "server crash: boom"})
    with pytest.raises(InputError) as info:
        client.call("x")
    assert info.value.code == "DAEMON_ERROR" and "boom" in str(info.value)


def test_invoke_raises_structured_errors_with_code_and_details() -> None:
    client, _ = _client(_ok({
        "success": False,
        "error_code": "FOCUS_LOST",
        "message": "not foreground",
        "retryable": True,
        "details": {"released": ["w"]},
    }))

    with pytest.raises(InputError) as info:
        client.invoke("set_keys", session_id="s", down=["a"])

    assert info.value.code == "FOCUS_LOST"
    assert info.value.details == {"released": ["w"]}
    assert "not foreground" in str(info.value)


def test_daemon_unavailable_propagates() -> None:
    class Missing(FakeTransport):
        def open(self, name):
            raise DaemonUnavailable("no pipe")

    client = Client(name=r"\\.\pipe\test", transport=Missing(_ok({})))
    with pytest.raises(DaemonUnavailable):
        client.call("list_targets")


def test_pipe_name_matches_pywin32_ipc_module() -> None:
    ipc = pytest.importorskip("game_input_mcp.ipc")

    assert client_mod.pipe_name() == ipc.pipe_name()
    assert client_mod.current_user_sid_str().startswith("S-1-5-")


def test_real_transport_reports_daemon_unavailable_for_missing_pipe() -> None:
    client = Client(name=r"\\.\pipe\game-input-mcp.does-not-exist")
    with pytest.raises(DaemonUnavailable):
        client.call("list_targets")


# --- sessions -----------------------------------------------------------------------

def _session_responder(state: dict):
    def respond(req):
        method, params = req["method"], req["params"]
        if method == "session_open":
            return {"id": req["id"], "ok": True, "result": {"success": True, "session_id": "sid1", "lease_ms": params["lease_ms"], "held_keys": []}}
        if method == "set_keys":
            return {"id": req["id"], "ok": True, "result": {"success": True, "held_keys": params.get("down") or []}}
        if method == "run_timeline":
            return {"id": req["id"], "ok": True, "result": {"success": True, "stopped_reason": "completed", "batches": []}}
        if method == "mouse_move_relative":
            return {"id": req["id"], "ok": True, "result": {"success": True, "steps": 3}}
        if method == "session_heartbeat":
            state["heartbeats"] = state.get("heartbeats", 0) + 1
            return {"id": req["id"], "ok": True, "result": {"success": True}}
        if method == "session_close":
            state["closed"] = state.get("closed", 0) + 1
            return {"id": req["id"], "ok": True, "result": {"success": True, "released": ["w"]}}
        if method == "session_state":
            return {"id": req["id"], "ok": True, "result": {"success": True, "status": "active"}}
        if method == "abort_timeline":
            return {"id": req["id"], "ok": True, "result": {"success": True, "aborted": True}}
        return {"id": req["id"], "ok": False, "error": f"unknown {method}"}
    return respond


def test_session_context_manager_opens_drives_and_closes() -> None:
    state: dict = {}
    client, transport = _client(_session_responder(state))

    with client.session({"pid": 7}, lease_ms=3000, heartbeat=False) as s:
        assert s.session_id == "sid1" and s.lease_ms == 3000
        assert s.hold("w")["held_keys"] == ["w"]
        s.set_keys(down=["a"], up=["w"], buttons_down=["left"])
        s.tap("space", hold_ms=120)
        s.look(600, 0, duration_ms=800)
        s.state()
        s.abort()

    methods = [(r["method"], r["params"]) for r in transport.requests]
    assert methods[0] == ("session_open", {
        "target": {"pid": 7}, "lease_ms": 3000, "focus": "acquire_once", "max_hold_ms": 30000, "takeover": False,
    })
    assert methods[1] == ("set_keys", {
        "session_id": "sid1", "down": ["w"], "up": None, "buttons_down": None, "buttons_up": None, "mode": "scancode",
    })
    assert methods[2][1]["down"] == ["a"] and methods[2][1]["up"] == ["w"] and methods[2][1]["buttons_down"] == ["left"]
    assert methods[3] == ("run_timeline", {
        "session_id": "sid1",
        "events": [{"t_ms": 0, "op": "down", "key": "space"}, {"t_ms": 120, "op": "up", "key": "space"}],
        "total_ms": 120,
        "allow_dangling": False,
    })
    assert methods[4] == ("mouse_move_relative", {"session_id": "sid1", "dx": 600, "dy": 0, "duration_ms": 800, "rate_hz": 250})
    assert methods[5][0] == "session_state" and methods[6][0] == "abort_timeline"
    assert methods[-1] == ("session_close", {"session_id": "sid1"})
    assert state["closed"] == 1


def test_session_closes_on_exception_and_close_is_idempotent() -> None:
    state: dict = {}
    client, transport = _client(_session_responder(state))

    with pytest.raises(RuntimeError, match="user error"):
        with client.session(7, heartbeat=False) as s:
            s.hold("w")
            raise RuntimeError("user error")
    assert state["closed"] == 1
    assert s.closed is True
    assert s.close() is None
    assert state["closed"] == 1
    with pytest.raises(InputError) as info:
        s.hold("w")
    assert info.value.code == "SESSION_CLOSED"


def test_session_open_failure_raises_before_any_session_exists() -> None:
    client, _ = _client(_ok({"success": False, "error_code": "SESSION_EXISTS", "message": "taken", "details": {}}))

    with pytest.raises(InputError) as info:
        client.open_session({"pid": 7}, heartbeat=False)

    assert info.value.code == "SESSION_EXISTS"


def test_session_heartbeat_thread_extends_lease_until_close() -> None:
    state: dict = {}
    client, _ = _client(_session_responder(state))

    session = client.open_session({"pid": 7}, lease_ms=300)  # heartbeat every ~100 ms
    time.sleep(0.45)
    session.close()
    beats = state.get("heartbeats", 0)
    time.sleep(0.25)

    assert beats >= 2
    assert state.get("heartbeats", 0) == beats  # thread stopped with the session
    assert session.heartbeat_error is None
