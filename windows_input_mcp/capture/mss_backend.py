from __future__ import annotations

from PIL import Image

from windows_input_mcp.models import Rect

from .base import CaptureBackend

try:
    import mss
except ImportError:
    mss = None


class MssBackend(CaptureBackend):
    name = "mss"
    priority = 20

    def is_available(self, rect: Rect | None) -> bool:
        return mss is not None

    def capture(self, rect: Rect | None) -> Image.Image:
        if mss is None:
            raise RuntimeError("mss is not available")
        with mss.mss() as sct:
            monitor = sct.monitors[0] if rect is None else {
                "left": rect.left,
                "top": rect.top,
                "width": rect.width,
                "height": rect.height,
            }
            raw = sct.grab(monitor)
            return Image.frombytes("RGB", raw.size, raw.rgb)
