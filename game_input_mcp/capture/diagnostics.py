"""Cheap checks that say when screen-region pixels may not be the target's.

A screen grab returns whatever is on screen in the window's rectangle: another
window on top, the desktop where the window hangs off the edge, a black
rectangle where a GPU surface could not be read. These probes find that out so
the service can escalate to a window-targeted backend or warn the caller.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from PIL import Image, ImageChops

from game_input_mcp import win32
from game_input_mcp.models import Rect, TargetInfo

BLACK_MAX_CHANNEL = 8


def sample_points(rect: Rect) -> list[tuple[int, int]]:
    """Corners, edge midpoints and centre, inset one pixel so they stay inside."""
    if rect.width <= 0 or rect.height <= 0:
        return []
    xs = (rect.left, (rect.left + rect.right - 1) // 2, rect.right - 1)
    ys = (rect.top, (rect.top + rect.bottom - 1) // 2, rect.bottom - 1)
    return [(x, y) for y in ys for x in xs]


def probe_visibility(target: TargetInfo, rect: Rect) -> dict:
    """{"occluders": [window, ...], "offscreen": bool} for the sample points.

    A point is occluded when its top-level window is neither the target nor
    another window of the target's process (its own popups/overlays), and
    off-screen when no window is there at all. WindowFromPoint skips hidden
    and click-through windows, so a click-through HUD overlay does not count.
    """
    target_root = win32.root_window(target.hwnd)
    occluders: dict[int, dict] = {}
    offscreen = False
    for x, y in sample_points(rect):
        root = win32.root_window_at(x, y)
        if root == 0:
            offscreen = True
            continue
        if root == target_root or root in occluders:
            continue
        described = win32.describe_window(root)
        if described.get("pid") == target.pid:
            continue
        occluders[root] = described
    return {"occluders": list(occluders.values()), "offscreen": offscreen}


def within_one_monitor(rect: Rect) -> bool:
    return any(
        m.left <= rect.left and m.top <= rect.top and m.right >= rect.right and m.bottom >= rect.bottom
        for m in win32.get_monitor_rects()
    )


def frame_hash(image: Image.Image) -> str:
    """Exact-pixel fingerprint (blake2b, 8 bytes) of the image as captured."""
    return hashlib.blake2b(image.convert("RGB").tobytes(), digest_size=8).hexdigest()


PIXEL_DIFF_THRESHOLD = 8  # per-channel difference that counts as "this pixel changed"


def diff_fraction(a: Image.Image, b: Image.Image, threshold: int = PIXEL_DIFF_THRESHOLD) -> float:
    """Fraction of pixels whose largest channel difference exceeds ``threshold``
    (1.0 when the sizes differ)."""
    if a.size != b.size:
        return 1.0
    red, green, blue = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).split()
    largest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    changed = largest.point(lambda v: 255 if v > threshold else 0).histogram()[255]
    return changed / (a.width * a.height) if a.width * a.height else 0.0


@dataclass(frozen=True)
class PreviousFrame:
    digest: str
    frame_id: str | None
    at: float          # clock() when it was captured
    cpu_ms: int | None  # target process CPU time then


class FrameHistory:
    """The last capture of each window, for 'is this the same frame again?'.
    Entries older than ``ttl_s`` are ignored (an hour-old identical frame says
    nothing), and the table is bounded."""

    def __init__(self, *, ttl_s: float = 60.0, max_entries: int = 32, clock=time.monotonic) -> None:
        self._ttl_s = ttl_s
        self._max = max_entries
        self._clock = clock
        self._entries: dict[int, PreviousFrame] = {}

    def previous(self, hwnd: int) -> PreviousFrame | None:
        entry = self._entries.get(hwnd)
        if entry is None or self._clock() - entry.at > self._ttl_s:
            return None
        return entry

    def record(self, hwnd: int, digest: str, frame_id: str | None, cpu_ms: int | None) -> None:
        self._entries.pop(hwnd, None)
        self._entries[hwnd] = PreviousFrame(digest, frame_id, self._clock(), cpu_ms)
        while len(self._entries) > self._max:
            self._entries.pop(next(iter(self._entries)))

    def now(self) -> float:
        return self._clock()


HISTORY = FrameHistory()


def is_black(image: Image.Image) -> bool:
    """True when every channel of a 16x9 downscale stays under the threshold."""
    small = image.convert("RGB").resize((16, 9), Image.BILINEAR)
    return all(high < BLACK_MAX_CHANNEL for _, high in small.getextrema())
