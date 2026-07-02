from __future__ import annotations

from PIL import Image

from game_input_mcp import targets
from game_input_mcp.frames import FrameCache
from game_input_mcp.geometry import FrameGeometry
from game_input_mcp.models import Rect, TargetInfo, error_response, ok_response

from .base import CaptureResult, capture_region


def _capture_rect(target: TargetInfo, region: list[int] | None, scope: str) -> Rect:
    normalized_scope = scope.lower().strip()
    if normalized_scope not in {"client", "screen"}:
        raise ValueError("capture region scope must be client or screen")

    if region is None:
        return target.client_rect_screen

    rect = Rect.from_list(region)
    if normalized_scope == "client":
        return Rect(
            target.client_rect_screen.left + rect.left,
            target.client_rect_screen.top + rect.top,
            target.client_rect_screen.left + rect.right,
            target.client_rect_screen.top + rect.bottom,
        )

    return rect


def _resize_if_needed(image: Image.Image, max_width: int) -> tuple[Image.Image, float]:
    if max_width <= 0 or image.width <= max_width:
        return image, 1.0
    scale = max_width / image.width
    height = max(1, int(round(image.height * scale)))
    return image.resize((max_width, height), Image.LANCZOS), scale


def capture_target(
    target: int | dict,
    *,
    region: list[int] | None = None,
    scope: str = "client",
    backend: str = "auto",
    max_width: int = 1920,
    cache: FrameCache | None = None,
) -> dict:
    resolved = targets.resolve_target(target)
    if resolved is None:
        return error_response("TARGET_NOT_FOUND", "Target window was not found", retryable=True, target=target)
    if resolved.is_minimized:
        return error_response(
            "TARGET_MINIMIZED",
            "Target window is minimized and cannot be captured",
            retryable=True,
            **resolved.to_dict(),
        )

    try:
        rect = _capture_rect(resolved, region, scope)
    except Exception as exc:
        return error_response(
            "INVALID_REGION",
            "Invalid capture region or scope",
            retryable=False,
            reason=str(exc),
            region=region,
            scope=scope,
        )

    try:
        captured: CaptureResult = capture_region(rect, backend=backend)
    except Exception as exc:
        return error_response(
            "CAPTURE_FAILED",
            "Capture backend failed",
            retryable=True,
            backend=backend,
            reason=str(exc),
        )
    image, scale = _resize_if_needed(captured.image, max_width)
    frame_geometry = FrameGeometry(
        image_size=(image.width, image.height),
        capture_rect_screen=rect,
        client_rect_screen=resolved.client_rect_screen,
        scale=scale,
    )
    metadata = {
        "target": resolved.to_dict(),
        "image": {
            "width": image.width,
            "height": image.height,
            "format": "png",
            "scale": scale,
        },
        "geometry": {
            "window_rect_screen": resolved.window_rect.to_list(),
            "client_rect_screen": frame_geometry.client_rect_screen.to_list(),
            "capture_rect_screen": frame_geometry.capture_rect_screen.to_list(),
            "client_size": list(resolved.client_size),
            "dpi": resolved.dpi,
            "frame_image_size": list(frame_geometry.image_size),
        },
        "backend": {"name": captured.backend, "mode": captured.mode},
    }

    cache_instance = cache or FrameCache()
    record = cache_instance.store(image, metadata)
    return ok_response(
        frame_id=record.frame_id,
        image_path=str(record.image_path),
        target=record.metadata["target"],
        image=record.metadata["image"],
        geometry=record.metadata["geometry"],
        backend=record.metadata["backend"],
        metadata_path=str(record.metadata_path),
        created_at=record.metadata["created_at"],
    )
