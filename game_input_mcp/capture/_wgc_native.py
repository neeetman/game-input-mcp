"""The only module that imports the WGC library (``windows-capture``, spike S2).

Everything above it works with the small ``WgcSource`` interface so the
library can be swapped (pywinrt, hand-rolled COM) without touching the
backend, and so tests can substitute a fake source.
"""
from __future__ import annotations

import importlib.util
import threading
from dataclasses import dataclass
from typing import Any, Protocol

# windows-capture throttles delivery to one frame per interval; the callback
# has to copy each frame out of native memory, so do not ask for 60 fps of 4K.
MIN_UPDATE_INTERVAL_MS = 100


@dataclass(frozen=True)
class WgcFrame:
    pixels: Any      # numpy uint8 array (height, width, 4), BGRA, owned copy
    width: int
    height: int
    qpc_ns: int      # SystemRelativeTime of the frame, same clock as perf_counter_ns
    seq: int


class WgcSource(Protocol):
    def latest(self) -> WgcFrame | None: ...

    def wait_newer(self, seq: int, timeout_s: float) -> WgcFrame | None:
        """Block until a frame with a sequence number above ``seq`` exists (or
        the timeout/closure), then return the newest frame (None if there is none)."""

    @property
    def closed(self) -> bool: ...

    def close(self) -> None: ...


def library_available() -> bool:
    return importlib.util.find_spec("windows_capture") is not None


class NativeWgcSource:
    """One free-threaded capture of one window, keeping the newest frame."""

    def __init__(self, hwnd: int) -> None:
        import numpy as np
        from windows_capture import WindowsCapture

        self._np = np
        self._cond = threading.Condition()
        self._latest: WgcFrame | None = None
        self._seq = 0
        self._closed = False

        def build(draw_border):
            return WindowsCapture(
                cursor_capture=False,
                draw_border=draw_border,
                minimum_update_interval=MIN_UPDATE_INTERVAL_MS,
                window_hwnd=int(hwnd),
            )

        try:
            capture = build(False)
        except Exception:  # older OS: the border cannot be turned off
            capture = build(None)

        @capture.event
        def on_frame_arrived(frame, control):  # runs on the library's capture thread
            pixels = np.array(frame.frame_buffer, copy=True)  # buffer is only valid in here
            with self._cond:
                self._seq += 1
                self._latest = WgcFrame(
                    pixels=pixels,
                    width=int(frame.width),
                    height=int(frame.height),
                    qpc_ns=int(frame.timespan) * 100,
                    seq=self._seq,
                )
                self._cond.notify_all()

        @capture.event
        def on_closed():
            with self._cond:
                self._closed = True
                self._cond.notify_all()

        self._control = capture.start_free_threaded()

    def latest(self) -> WgcFrame | None:
        with self._cond:
            return self._latest

    def wait_newer(self, seq: int, timeout_s: float) -> WgcFrame | None:
        with self._cond:
            self._cond.wait_for(
                lambda: self._closed or (self._latest is not None and self._latest.seq > seq),
                timeout=max(0.0, timeout_s),
            )
            return self._latest

    @property
    def closed(self) -> bool:
        with self._cond:
            return self._closed

    def close(self) -> None:
        """Ask the capture to stop and return immediately: a frozen game must
        never be able to block the request thread (the library's wait() can)."""
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        control = self._control

        def stop() -> None:
            try:
                control.stop()
            except Exception:  # noqa: BLE001 - already stopped / window gone
                pass

        threading.Thread(target=stop, name="wgc-stop", daemon=True).start()


def open_source(hwnd: int) -> WgcSource:
    return NativeWgcSource(hwnd)
