r"""Named-pipe IPC between the (non-admin) MCP server and the (admin)
game-input-daemon.

Wire format: 4-byte big-endian unsigned length prefix, then UTF-8 JSON.

Request shape:
    {"id": <int>, "method": "<name>", "params": {...}}

Response shape (success):
    {"id": <int>, "ok": true,  "result": {...}}

Response shape (error):
    {"id": <int>, "ok": false, "error": "<message>"}

The pipe name is per-user: \\.\pipe\game-input-mcp.<user-sid>. Daemon and
client both derive it from the current user's token, so they always agree
even when daemon runs at high IL via a scheduled task — elevation does not
change the user SID, only the integrity level.
"""
from __future__ import annotations

import itertools
import json
import struct
import threading
from typing import Any, Callable

import pywintypes
import win32api
import win32con
import win32file
import win32pipe
import win32security
import winerror

PIPE_PREFIX = r"\\.\pipe\game-input-mcp"
_LENGTH_HEADER = struct.Struct(">I")  # 4-byte big-endian unsigned
_MAX_FRAME_BYTES = 8 * 1024 * 1024     # 8 MiB safety cap


# === SID + pipe naming =====================================================

def current_user_sid_str() -> str:
    """SID string of the user owning the current process token.

    Same for elevated and non-elevated processes of the same user — elevation
    changes the integrity level, not the user SID. Daemon (high IL) and client
    (medium IL) of the same logged-in user produce the same string, so they
    pick the same pipe name.
    """
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    try:
        sid, _attrs = win32security.GetTokenInformation(
            token, win32security.TokenUser
        )
    finally:
        win32api.CloseHandle(token)
    return win32security.ConvertSidToStringSid(sid)


def pipe_name(sid: str | None = None) -> str:
    return f"{PIPE_PREFIX}.{sid or current_user_sid_str()}"


# === Framed read/write =====================================================

def _read_exact(handle, n: int) -> bytes:
    """Read exactly n bytes from a pipe handle, or raise EOFError on close."""
    buf = b""
    while len(buf) < n:
        try:
            hr, chunk = win32file.ReadFile(handle, n - len(buf))
        except pywintypes.error as e:
            if e.winerror in (winerror.ERROR_BROKEN_PIPE,
                              winerror.ERROR_PIPE_NOT_CONNECTED):
                raise EOFError("pipe closed by peer") from e
            raise
        if not chunk:
            raise EOFError("pipe closed by peer (empty read)")
        buf += chunk
    return buf


def read_frame(handle) -> dict:
    header = _read_exact(handle, _LENGTH_HEADER.size)
    (length,) = _LENGTH_HEADER.unpack(header)
    if length == 0:
        return {}
    if length > _MAX_FRAME_BYTES:
        raise ValueError(f"frame too large: {length} bytes")
    payload = _read_exact(handle, length)
    return json.loads(payload.decode("utf-8"))


def write_frame(handle, obj: dict) -> None:
    payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    win32file.WriteFile(handle, _LENGTH_HEADER.pack(len(payload)) + payload)


# === Client =================================================================

class DaemonUnavailable(RuntimeError):
    """Pipe not present — daemon is not running. Hint user to run install."""


class Client:
    """Thread-safe blocking client. Each call() opens a fresh pipe handle.

    Opening per-call is simpler than maintaining a persistent connection and
    avoids reconnect/heartbeat logic. The pipe is local so connect cost is
    sub-millisecond — negligible vs SendInput latency.
    """

    def __init__(self, name: str | None = None) -> None:
        self._name = name or pipe_name()
        self._id_counter = itertools.count(1)
        self._id_lock = threading.Lock()

    def call(self, method: str, **params: Any) -> Any:
        with self._id_lock:
            req_id = next(self._id_counter)

        handle = self._connect()
        try:
            write_frame(handle, {"id": req_id, "method": method, "params": params})
            resp = read_frame(handle)
        finally:
            try:
                win32file.CloseHandle(handle)
            except pywintypes.error:
                pass

        if resp.get("id") != req_id:
            raise RuntimeError(
                f"protocol error: response id {resp.get('id')} != request id {req_id}"
            )
        if resp.get("ok"):
            return resp.get("result")
        raise RuntimeError(resp.get("error") or "daemon returned error without message")

    def _connect(self):
        for attempt in range(3):
            try:
                return win32file.CreateFile(
                    self._name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0, None,
                    win32file.OPEN_EXISTING, 0, None,
                )
            except pywintypes.error as e:
                if e.winerror == winerror.ERROR_FILE_NOT_FOUND:
                    raise DaemonUnavailable(
                        f"daemon pipe {self._name} not found. "
                        f"Run: python -m game_input_mcp.install"
                    ) from e
                if e.winerror == winerror.ERROR_PIPE_BUSY and attempt < 2:
                    win32pipe.WaitNamedPipe(self._name, 1000)
                    continue
                raise
        raise RuntimeError(f"pipe {self._name} busy after retries")


# === Server (used by daemon.py) ============================================

Handler = Callable[[dict], Any]


class Server:
    """Multi-instance named-pipe server. Each ConnectNamedPipe spawns a worker
    thread that handles one request and disconnects.

    The daemon process owns one Server instance and registers handlers via
    register(). The thread model is intentionally simple: one request per
    connection means no in-flight state to manage on disconnect, and matches
    Client which also opens-per-call.
    """

    def __init__(self, name: str | None = None) -> None:
        self._name = name or pipe_name()
        self._handlers: dict[str, Handler] = {}
        self._stop = threading.Event()

    def register(self, method: str, handler: Handler) -> None:
        self._handlers[method] = handler

    def stop(self) -> None:
        self._stop.set()

    def serve_forever(self, security_attributes) -> None:
        """Block accepting connections until stop() is called.

        `security_attributes` is a win32security.SECURITY_ATTRIBUTES carrying
        the DACL — built in install.py / daemon.py so this module stays
        policy-free.
        """
        while not self._stop.is_set():
            pipe = win32pipe.CreateNamedPipe(
                self._name,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                win32pipe.PIPE_UNLIMITED_INSTANCES,
                65536, 65536,
                0,
                security_attributes,
            )
            try:
                win32pipe.ConnectNamedPipe(pipe, None)
            except pywintypes.error:
                win32file.CloseHandle(pipe)
                continue

            t = threading.Thread(target=self._handle_one, args=(pipe,), daemon=True)
            t.start()

    def _handle_one(self, pipe) -> None:
        try:
            req = read_frame(pipe)
            resp = self._dispatch(req)
            write_frame(pipe, resp)
        except EOFError:
            pass  # client disconnected mid-request, nothing to send
        except Exception as e:  # noqa: BLE001 — last-resort: never crash the worker
            try:
                write_frame(pipe, {
                    "id": 0, "ok": False,
                    "error": f"server crash: {type(e).__name__}: {e}",
                })
            except Exception:
                pass
        finally:
            # Block until the client drains the response, otherwise
            # DisconnectNamedPipe discards the pending write buffer.
            try:
                win32file.FlushFileBuffers(pipe)
            except pywintypes.error:
                pass
            try:
                win32pipe.DisconnectNamedPipe(pipe)
            except pywintypes.error:
                pass
            try:
                win32file.CloseHandle(pipe)
            except pywintypes.error:
                pass

    def _dispatch(self, req: dict) -> dict:
        req_id = req.get("id", 0)
        method = req.get("method")
        params = req.get("params") or {}
        handler = self._handlers.get(method)
        if handler is None:
            return {"id": req_id, "ok": False, "error": f"unknown method: {method}"}
        try:
            result = handler(params)
            return {"id": req_id, "ok": True, "result": result}
        except Exception as e:  # noqa: BLE001 — pipe is user-DACL locked, verbatim is OK
            import traceback
            tb = traceback.format_exc(limit=8)
            return {
                "id": req_id, "ok": False,
                "error": f"{type(e).__name__}: {e}\n{tb}",
            }
