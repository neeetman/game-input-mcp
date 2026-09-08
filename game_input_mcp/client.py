"""Stdlib-only client for the elevated game-input daemon.

Same wire protocol as ``ipc.py`` (4-byte big-endian length prefix + UTF-8
JSON over the per-user named pipe) but built on ``ctypes`` so UECapture tools
and other callers need neither pywin32 nor the MCP server. ``InputSession``
wraps the daemon session API (``set_keys`` / ``run_timeline`` /
``mouse_move_relative``) with a heartbeat thread and a guaranteed close.

    from game_input_mcp.client import Client

    with Client().session({"pid": pid}, lease_ms=2000) as s:
        s.hold("w")
        s.run_timeline([...], total_ms=2000)
        s.look(dx=600, dy=0, duration_ms=800)
    # __exit__ -> session_close -> everything released, even on exception
"""
from __future__ import annotations

import ctypes
import itertools
import json
import struct
import threading
from contextlib import contextmanager
from ctypes import wintypes
from typing import Any, Iterator, Sequence

PIPE_PREFIX = r"\\.\pipe\game-input-mcp"
_LENGTH_HEADER = struct.Struct(">I")
_MAX_FRAME_BYTES = 8 * 1024 * 1024

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_FILE_NOT_FOUND = 2
_ERROR_BROKEN_PIPE = 109
_ERROR_PIPE_BUSY = 231
_ERROR_PIPE_NOT_CONNECTED = 233
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1


class DaemonUnavailable(RuntimeError):
    """Pipe not present: the daemon is not running (python -m game_input_mcp.install)."""


class ProtocolError(RuntimeError):
    """Malformed or mismatched response frame."""


class InputError(RuntimeError):
    """Structured daemon error (``success: false``). ``code`` carries the
    daemon error_code (FOCUS_LOST, SESSION_EXPIRED, INVALID_TIMELINE, ...)."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.code = str(response.get("error_code") or "ERROR")
        self.details: dict[str, Any] = response.get("details") or {}
        message = response.get("message") or response.get("error") or "input request failed"
        super().__init__(f"{self.code}: {message}")


# --- SID / pipe naming ----------------------------------------------------------------

def current_user_sid_str() -> str:
    """SID string of the current process token's user (same for elevated and
    non-elevated processes of one user, so client and daemon agree)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        buf = ctypes.create_string_buffer(max(needed.value, 8))
        if not advapi32.GetTokenInformation(token, _TOKEN_USER, buf, len(buf), ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER = { SID_AND_ATTRIBUTES User } ; User.Sid is the first pointer.
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        out = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(psid, ctypes.byref(out)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(out.value)
        finally:
            kernel32.LocalFree(out)
    finally:
        kernel32.CloseHandle(token)


def pipe_name(sid: str | None = None) -> str:
    return f"{PIPE_PREFIX}.{sid or current_user_sid_str()}"


# --- transport ------------------------------------------------------------------------

class _PipeTransport:
    """Blocking named-pipe client transport on kernel32."""

    def __init__(self) -> None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k.CreateFileW.restype = wintypes.HANDLE
        k.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k.ReadFile.restype = wintypes.BOOL
        k.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        k.WriteFile.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        self._k = k

    def open(self, name: str):
        for attempt in range(3):
            handle = self._k.CreateFileW(
                name, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING, 0, None
            )
            if handle is not None and handle != _INVALID_HANDLE_VALUE:
                return handle
            err = ctypes.get_last_error()
            if err == _ERROR_FILE_NOT_FOUND:
                raise DaemonUnavailable(
                    f"daemon pipe {name} not found. Run: python -m game_input_mcp.install"
                )
            if err == _ERROR_PIPE_BUSY and attempt < 2:
                self._k.WaitNamedPipeW(name, 1000)
                continue
            raise ctypes.WinError(err)
        raise DaemonUnavailable(f"pipe {name} busy after retries")

    def read(self, handle, n: int) -> bytes:
        buf = ctypes.create_string_buffer(n)
        got = wintypes.DWORD(0)
        if not self._k.ReadFile(handle, buf, n, ctypes.byref(got), None):
            err = ctypes.get_last_error()
            if err in (_ERROR_BROKEN_PIPE, _ERROR_PIPE_NOT_CONNECTED):
                raise EOFError("pipe closed by peer")
            raise ctypes.WinError(err)
        if got.value == 0:
            raise EOFError("pipe closed by peer (empty read)")
        return buf.raw[: got.value]

    def write(self, handle, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = wintypes.DWORD(0)
            chunk = bytes(view)
            if not self._k.WriteFile(handle, chunk, len(chunk), ctypes.byref(written), None):
                raise ctypes.WinError(ctypes.get_last_error())
            view = view[written.value:]

    def close(self, handle) -> None:
        self._k.CloseHandle(handle)


# --- client ---------------------------------------------------------------------------

class Client:
    """Thread-safe request/response client; one pipe connection per call."""

    def __init__(self, name: str | None = None, transport: Any | None = None) -> None:
        self._name = name or pipe_name()
        self._transport = transport or _PipeTransport()
        self._ids = itertools.count(1)
        self._id_lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    def _read_exact(self, handle, n: int) -> bytes:
        out = b""
        while len(out) < n:
            out += self._transport.read(handle, n - len(out))
        return out

    def _read_frame(self, handle) -> dict:
        (length,) = _LENGTH_HEADER.unpack(self._read_exact(handle, _LENGTH_HEADER.size))
        if length == 0:
            return {}
        if length > _MAX_FRAME_BYTES:
            raise ProtocolError(f"frame too large: {length} bytes")
        return json.loads(self._read_exact(handle, length).decode("utf-8"))

    def _write_frame(self, handle, obj: dict) -> None:
        payload = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self._transport.write(handle, _LENGTH_HEADER.pack(len(payload)) + payload)

    def call(self, method: str, **params: Any) -> Any:
        """Raw daemon call. Returns the handler result verbatim (including
        structured ``success: false`` responses); raises InputError only for
        transport-level daemon failures."""
        with self._id_lock:
            req_id = next(self._ids)
        handle = self._transport.open(self._name)
        try:
            self._write_frame(handle, {"id": req_id, "method": method, "params": params})
            resp = self._read_frame(handle)
        finally:
            try:
                self._transport.close(handle)
            except Exception:  # noqa: BLE001
                pass
        if resp.get("id") != req_id:
            raise ProtocolError(f"response id {resp.get('id')} != request id {req_id}")
        if resp.get("ok"):
            return resp.get("result")
        raise InputError({"error_code": "DAEMON_ERROR", "message": resp.get("error") or "daemon returned error"})

    def invoke(self, method: str, **params: Any) -> dict:
        """Like call() but raises InputError on structured handler errors."""
        result = self.call(method, **params)
        if isinstance(result, dict) and result.get("success") is False:
            raise InputError(result)
        return result if isinstance(result, dict) else {"success": True, "result": result}

    def open_session(
        self,
        target: dict[str, Any] | int,
        *,
        lease_ms: int = 2000,
        focus: str = "acquire_once",
        max_hold_ms: int = 30000,
        takeover: bool = False,
        heartbeat: bool = True,
    ) -> "InputSession":
        if isinstance(target, int):
            target = {"pid": target}
        info = self.invoke(
            "session_open",
            target=target,
            lease_ms=lease_ms,
            focus=focus,
            max_hold_ms=max_hold_ms,
            takeover=takeover,
        )
        return InputSession(self, info, heartbeat=heartbeat)

    @contextmanager
    def session(self, target: dict[str, Any] | int, **kwargs: Any) -> Iterator["InputSession"]:
        session = self.open_session(target, **kwargs)
        try:
            yield session
        finally:
            session.close()


class InputSession:
    """Handle on a daemon-owned input session. All methods raise InputError
    with the daemon error code on failure; close() is idempotent."""

    def __init__(self, client: Client, info: dict[str, Any], *, heartbeat: bool = True) -> None:
        self._client = client
        self.info = info
        self.session_id: str = str(info["session_id"])
        self.lease_ms: int = int(info.get("lease_ms", 2000))
        self._closed = False
        self._lock = threading.Lock()
        self.heartbeat_error: Exception | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if heartbeat:
            self._thread = threading.Thread(
                target=self._heartbeat_loop, name=f"input-session-{self.session_id}", daemon=True
            )
            self._thread.start()

    # -- lifecycle ----------------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        interval = max(0.05, self.lease_ms / 3000.0)
        while not self._stop.wait(interval):
            try:
                self._client.invoke("session_heartbeat", session_id=self.session_id)
            except Exception as exc:  # noqa: BLE001 — surface via heartbeat_error, stop
                self.heartbeat_error = exc
                return

    def _invoke(self, method: str, **params: Any) -> dict:
        if self._closed:
            raise InputError({"error_code": "SESSION_CLOSED", "message": "session already closed"})
        return self._client.invoke(method, session_id=self.session_id, **params)

    def close(self) -> dict | None:
        with self._lock:
            if self._closed:
                return None
            self._closed = True
        self._stop.set()
        try:
            return self._client.invoke("session_close", session_id=self.session_id)
        except InputError as exc:
            if exc.code == "SESSION_NOT_FOUND":
                return None
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> "InputSession":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- input ---------------------------------------------------------------------

    def set_keys(
        self,
        down: Sequence[str] | None = None,
        up: Sequence[str] | None = None,
        buttons_down: Sequence[str] | None = None,
        buttons_up: Sequence[str] | None = None,
        mode: str = "scancode",
    ) -> dict:
        return self._invoke(
            "set_keys",
            down=list(down) if down else None,
            up=list(up) if up else None,
            buttons_down=list(buttons_down) if buttons_down else None,
            buttons_up=list(buttons_up) if buttons_up else None,
            mode=mode,
        )

    def hold(self, *keys: str) -> dict:
        return self.set_keys(down=keys)

    def release(self, *keys: str) -> dict:
        return self.set_keys(up=keys)

    def tap(self, key: str, hold_ms: int = 30) -> dict:
        hold_ms = max(1, int(hold_ms))
        return self.run_timeline(
            [{"t_ms": 0, "op": "down", "key": key}, {"t_ms": hold_ms, "op": "up", "key": key}],
            total_ms=hold_ms,
        )

    def run_timeline(
        self, events: Sequence[dict[str, Any]], total_ms: float, allow_dangling: bool = False
    ) -> dict:
        return self._invoke(
            "run_timeline", events=list(events), total_ms=total_ms, allow_dangling=allow_dangling
        )

    def look(self, dx: int, dy: int, duration_ms: float = 0, rate_hz: float = 250) -> dict:
        return self._invoke(
            "mouse_move_relative", dx=int(dx), dy=int(dy), duration_ms=duration_ms, rate_hz=rate_hz
        )

    def abort(self) -> dict:
        return self._invoke("abort_timeline")

    def heartbeat(self, lease_ms: int | None = None) -> dict:
        return self._invoke("session_heartbeat", lease_ms=lease_ms)

    def state(self) -> dict:
        return self._invoke("session_state")
