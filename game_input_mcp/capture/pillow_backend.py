from __future__ import annotations

from PIL import Image, ImageGrab

from game_input_mcp.models import Rect

from .base import CaptureBackend


class PillowBackend(CaptureBackend):
    name = "pillow"
    priority = 100

    def capture(self, rect: Rect | None) -> Image.Image:
        kwargs: dict[str, object] = {"all_screens": True}
        if rect is not None:
            kwargs["bbox"] = (rect.left, rect.top, rect.right, rect.bottom)
        return ImageGrab.grab(**kwargs)
