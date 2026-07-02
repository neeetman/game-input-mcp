from __future__ import annotations

from dataclasses import dataclass

from .models import Rect, TargetInfo


@dataclass(frozen=True)
class FrameGeometry:
    image_size: tuple[int, int]
    capture_rect_screen: Rect
    client_rect_screen: Rect
    scale: float = 1.0

    def capture_to_screen(self, x: float, y: float) -> tuple[int, int]:
        iw, ih = self.image_size
        if iw <= 0 or ih <= 0:
            raise ValueError("frame image size must be positive")
        sx = self.capture_rect_screen.left + (float(x) * self.capture_rect_screen.width / iw)
        sy = self.capture_rect_screen.top + (float(y) * self.capture_rect_screen.height / ih)
        return int(round(sx)), int(round(sy))

    def normalized_to_screen(self, x: float, y: float) -> tuple[int, int]:
        return self.capture_to_screen(float(x) * self.image_size[0], float(y) * self.image_size[1])


def point_to_screen(
    x: float,
    y: float,
    scope: str,
    target: TargetInfo,
    *,
    frame: FrameGeometry | None = None,
    framebuffer_size: tuple[int, int] | None = None,
) -> tuple[int, int]:
    normalized_scope = scope.lower().strip()
    if normalized_scope == "screen":
        return int(round(x)), int(round(y))
    if normalized_scope == "client":
        return (
            int(round(target.client_rect_screen.left + x)),
            int(round(target.client_rect_screen.top + y)),
        )
    if normalized_scope == "normalized":
        if frame is None:
            raise ValueError("normalized scope requires frame geometry")
        return frame.normalized_to_screen(x, y)
    if normalized_scope == "capture":
        if frame is None:
            raise ValueError("capture scope requires frame geometry")
        return frame.capture_to_screen(x, y)
    if normalized_scope == "framebuffer":
        if frame is not None:
            return frame.capture_to_screen(x, y)
        if framebuffer_size is None:
            return point_to_screen(x, y, "client", target)
        fw, fh = framebuffer_size
        if fw <= 0 or fh <= 0:
            raise ValueError("framebuffer size must be positive")
        cx = float(x) * target.client_rect_screen.width / fw
        cy = float(y) * target.client_rect_screen.height / fh
        return point_to_screen(cx, cy, "client", target)
    raise ValueError(f"unknown coordinate scope: {scope}")
