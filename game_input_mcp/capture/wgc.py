"""Windows.Graphics.Capture backend: the pixels of one window, even when it is
covered or partly off screen.

Sessions are kept warm for ``IDLE_TTL_S`` after the last request so repeated
captures are cheap (cold start measured at ~270 ms) and the OS capture
indicator is not toggled on every call. A session is a ``WgcSource`` per hwnd
(see ``_wgc_native``).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from PIL import Image

from game_input_mcp import win32
from game_input_mcp.models import Rect, TargetInfo

from . import _wgc_native
from .base import CaptureBackend, CaptureError, CaptureResult

log = logging.getLogger(__name__)

IDLE_TTL_S = 5.0
MAX_SESSIONS = 4
DEFAULT_TIMEOUT_MS = 1500
# A warm session waits this long for a frame newer than the request before
# settling for the latest one (a static window may legitimately not redraw).
FRESH_GRACE_S = 0.25

# Swapped by tests.
open_source: Callable[[int], _wgc_native.WgcSource] = _wgc_native.open_source
library_available: Callable[[], bool] = _wgc_native.library_available
clock: Callable[[], float] = time.monotonic
qpc_ns: Callable[[], int] = time.perf_counter_ns


def configure(*, idle_ttl_s: float | None = None) -> None:
    global IDLE_TTL_S
    if idle_ttl_s is not None:
        IDLE_TTL_S = float(idle_ttl_s)


@dataclass
class _Session:
    hwnd: int
    source: _wgc_native.WgcSource
    last_used: float


def _warning(code: str, message: str, **details) -> dict:
    return {"code": code, "message": message, "details": details}


class WgcBackend(CaptureBackend):
    name = "wgc"
    priority = 5
    needs_window = True
    auto_eligible = False  # opt-in, or chosen by `auto` escalation (service.py)

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[int, _Session] = {}
        self._gc_started = False

    # -- availability ---------------------------------------------------------------

    def unavailable_reason(self, target: TargetInfo | None, rect: Rect | None) -> str:
        if not library_available():
            return "the windows-capture package is not installed (pip install game-input-mcp[wgc])"
        if target is None:
            return "a window target is required"
        if target.is_minimized:
            return "the target window is minimized"
        if rect is not None and not _inside(rect, target.client_rect_screen):
            return "the requested region is outside the window's client area"
        return "backend is not available for this request"

    def is_available_for(self, target: TargetInfo | None, rect: Rect | None) -> bool:
        if target is None or target.is_minimized or not library_available():
            return False
        return rect is None or _inside(rect, target.client_rect_screen)

    def is_available(self, rect: Rect | None) -> bool:
        return False  # never usable without a window target

    def mode(self, rect: Rect | None) -> str:
        return "window"

    # -- sessions -------------------------------------------------------------------

    def _session(self, hwnd: int, *, fresh: bool = False) -> tuple[_Session, bool]:
        """(session, cold). ``fresh`` discards an existing session first."""
        with self._lock:
            session = self._sessions.get(hwnd)
            if session is not None and (fresh or session.source.closed):
                self._drop(hwnd)
                session = None
            if session is not None:
                session.last_used = clock()
                return session, False
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda s: s.last_used)
                self._drop(oldest.hwnd)
            try:
                source = open_source(hwnd)
            except CaptureError:
                raise
            except Exception as exc:
                raise CaptureError(
                    "CAPTURE_FAILED",
                    f"Could not start Windows Graphics Capture for this window: {exc}",
                    hwnd=hwnd,
                    reason=str(exc),
                ) from exc
            session = _Session(hwnd=hwnd, source=source, last_used=clock())
            self._sessions[hwnd] = session
            self._ensure_gc()
            return session, True

    def _drop(self, hwnd: int) -> None:
        session = self._sessions.pop(hwnd, None)
        if session is not None:
            try:
                session.source.close()
            except Exception:  # noqa: BLE001
                log.exception("closing WGC session for hwnd %s failed", hwnd)

    def discard(self, hwnd: int) -> None:
        """Forget a session (used when a frozen capture is detected)."""
        with self._lock:
            self._drop(hwnd)

    def sweep(self, now: float | None = None) -> int:
        """Close sessions idle longer than the TTL; returns how many."""
        now = clock() if now is None else now
        with self._lock:
            stale = [h for h, s in self._sessions.items() if now - s.last_used > IDLE_TTL_S or s.source.closed]
            for hwnd in stale:
                self._drop(hwnd)
        return len(stale)

    def close_all(self) -> None:
        with self._lock:
            for hwnd in list(self._sessions):
                self._drop(hwnd)

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def _ensure_gc(self) -> None:
        if self._gc_started:
            return
        self._gc_started = True

        def loop() -> None:
            while True:
                time.sleep(1.0)
                try:
                    self.sweep()
                except Exception:  # noqa: BLE001
                    log.exception("WGC session sweep failed")

        threading.Thread(target=loop, name="wgc-gc", daemon=True).start()

    # -- capture --------------------------------------------------------------------

    def capture(self, rect: Rect | None) -> Image.Image:
        raise CaptureError("CAPTURE_BACKEND_UNAVAILABLE", "wgc needs a window target", retryable=False)

    def capture_for(
        self, target: TargetInfo | None, rect: Rect | None, timeout_ms: int | None = None
    ) -> CaptureResult:
        if target is None:
            raise CaptureError("CAPTURE_BACKEND_UNAVAILABLE", "wgc needs a window target", retryable=False)
        timeout_s = (timeout_ms or DEFAULT_TIMEOUT_MS) / 1000.0
        want = rect or target.client_rect_screen
        warnings: list[dict] = []
        for attempt in range(2):
            session, cold = self._session(target.hwnd, fresh=attempt == 1)
            frame = self._wait_frame(session, cold, timeout_s)
            if frame is None:
                raise CaptureError(
                    "CAPTURE_TIMEOUT",
                    "Windows Graphics Capture delivered no frame in time",
                    hwnd=target.hwnd,
                    timeout_ms=int(timeout_s * 1000),
                    session="cold" if cold else "warm",
                )
            ext = win32.extended_frame_bounds(target.hwnd)
            if ext is None:
                raise CaptureError("CAPTURE_FAILED", "Could not read the window's frame bounds", hwnd=target.hwnd)
            if (frame.width, frame.height) == (ext[2] - ext[0], ext[3] - ext[1]):
                break
            # The window was resized since this frame: drop the session and try once more.
            self.discard(target.hwnd)
        else:
            raise CaptureError(
                "CAPTURE_FAILED",
                "Frame size does not match the window bounds (window resized during capture)",
                hwnd=target.hwnd,
                frame_size=[frame.width, frame.height],
                window_bounds=list(ext),
            )

        crop = (want.left - ext[0], want.top - ext[1], want.right - ext[0], want.bottom - ext[1])
        if crop[0] < 0 or crop[1] < 0 or crop[2] > frame.width or crop[3] > frame.height or crop[2] <= crop[0] or crop[3] <= crop[1]:
            raise CaptureError(
                "CAPTURE_REGION_UNSUPPORTED",
                "The requested region is outside the captured window",
                retryable=False,
                hwnd=target.hwnd,
                crop=list(crop),
                frame_size=[frame.width, frame.height],
            )
        pixels = frame.pixels[crop[1]:crop[3], crop[0]:crop[2], :3][:, :, ::-1]  # BGRA -> RGB
        image = Image.fromarray(pixels.copy())

        now = qpc_ns()
        age_ms = max(0, int((now - frame.qpc_ns) / 1e6))
        if age_ms >= int(FRESH_GRACE_S * 1000):
            warnings.append(
                _warning(
                    "WGC_NO_NEW_FRAME",
                    "The window has not redrawn recently; this is its latest frame (a static scene, or a stalled capture)",
                    frame_age_ms=age_ms,
                )
            )
        current = win32.get_window_info_by_hwnd(target.hwnd)
        if current is not None and (
            tuple(current.window_rect) != tuple(target.window_rect.to_list())
            or tuple(current.client_screen_origin) != tuple(target.client_screen_origin)
        ):
            warnings.append(
                _warning(
                    "WINDOW_MOVED_DURING_CAPTURE",
                    "The window moved or resized while it was being captured; capture again before clicking",
                )
            )
        return CaptureResult(
            image=image,
            backend=self.name,
            mode="window",
            warnings=tuple(warnings),
            extra={
                "session": "cold" if cold else "warm",
                "frame_age_ms": age_ms,
                "frame_qpc_ns": frame.qpc_ns,
                "client_crop": list(crop),
            },
        )

    def _wait_frame(self, session: _Session, cold: bool, timeout_s: float):
        """Cold sessions need a first frame (full timeout). Warm ones want a
        frame newer than this request but settle for the latest after a short
        grace, because a static window may not redraw."""
        latest = session.source.latest()
        if cold or latest is None:
            return session.source.wait_newer(0, timeout_s)
        return session.source.wait_newer(latest.seq, min(timeout_s, FRESH_GRACE_S))


def _inside(inner: Rect, outer: Rect) -> bool:
    return (
        inner.left >= outer.left
        and inner.top >= outer.top
        and inner.right <= outer.right
        and inner.bottom <= outer.bottom
    )
