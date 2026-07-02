from __future__ import annotations

from PIL import Image

from game_input_mcp import win32
from game_input_mcp.models import Rect

from .base import CaptureBackend

try:
    import dxcam
except Exception:
    dxcam = None


class DxcamBackend(CaptureBackend):
    name = "dxcam"
    priority = 10

    def __init__(self) -> None:
        self._cameras: dict[int, object] = {}

    def is_available(self, rect: Rect | None) -> bool:
        return dxcam is not None and rect is not None and self._resolve_region(rect) is not None

    def _resolve_region(self, rect: Rect) -> tuple[int, tuple[int, int, int, int] | None] | None:
        for index, monitor in enumerate(win32.get_monitor_rects()):
            if (
                monitor.left <= rect.left
                and monitor.top <= rect.top
                and monitor.right >= rect.right
                and monitor.bottom >= rect.bottom
            ):
                if monitor == rect:
                    return index, None
                return index, (
                    rect.left - monitor.left,
                    rect.top - monitor.top,
                    rect.right - monitor.left,
                    rect.bottom - monitor.top,
                )
        return None

    def _camera(self, output_idx: int) -> object:
        if output_idx not in self._cameras:
            self._cameras[output_idx] = dxcam.create(output_idx=output_idx, processor_backend="numpy")
        return self._cameras[output_idx]

    def capture(self, rect: Rect | None) -> Image.Image:
        if rect is None:
            raise ValueError("dxcam backend requires a region")
        resolved = self._resolve_region(rect)
        if resolved is None:
            raise ValueError("dxcam region must be contained in one monitor")
        output_idx, region = resolved
        frame = self._camera(output_idx).grab(region=region, copy=True, new_frame_only=False)
        if frame is None:
            raise RuntimeError("dxcam returned no frame")
        return Image.fromarray(frame)
