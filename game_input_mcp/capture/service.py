from __future__ import annotations

import logging
import time

from PIL import Image

from game_input_mcp import targets, win32
from game_input_mcp.frames import FrameCache
from game_input_mcp.geometry import FrameGeometry
from game_input_mcp.models import Rect, TargetInfo, error_response, ok_response

from . import base as capture_base
from . import diagnostics, hdr
from .base import CaptureError, CaptureResult, capture_region, normalize_backend_name

HDR = hdr.HdrProbe()
log = logging.getLogger(__name__)

# More than this fraction of changed pixels between WGC and a screen grab of the
# same window means the WGC frame is stale; less is noise (cursor, caret, flash).
CROSS_CHECK_DIFF_FRACTION = 0.005


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


def _warning(code: str, message: str, **details) -> dict:
    return {"code": code, "message": message, "details": details}


def _capture_with_escalation(
    resolved: TargetInfo, rect: Rect, backend: str, timeout_ms: int | None, wgc_first: bool = False
) -> tuple[CaptureResult, list[dict]]:
    """Capture, escalating `auto` to the window-targeted backend when a screen
    grab cannot be trusted: another window covers the target, part of it is off
    screen or spans monitors, or the first grab came back black. Returns the
    result plus the warnings that still apply to the pixels returned."""
    warnings: list[dict] = []
    selected = normalize_backend_name(backend)
    visibility = {"occluders": [], "offscreen": False}
    prefer: tuple[str, ...] = ()
    if selected == "auto":
        visibility = diagnostics.probe_visibility(resolved, rect)
        spans_monitors = not diagnostics.within_one_monitor(rect)
        if wgc_first or visibility["occluders"] or visibility["offscreen"] or spans_monitors:
            prefer = ("wgc",)

    captured = capture_region(rect, backend=backend, target=resolved, timeout_ms=timeout_ms, prefer=prefer)

    if selected == "auto" and captured.backend != "wgc" and diagnostics.is_black(captured.image):
        try:
            retry = capture_region(rect, backend="wgc", target=resolved, timeout_ms=timeout_ms)
        except Exception:  # noqa: BLE001 - WGC absent or failed: keep the black frame, warn below
            retry = None
        if retry is not None and not diagnostics.is_black(retry.image):
            captured = retry

    warnings.extend(captured.warnings)
    if captured.backend != "wgc":
        if visibility["occluders"]:
            warnings.append(
                _warning(
                    "TARGET_OCCLUDED",
                    "Another window covers part of the target; these pixels may not be the game's",
                    by=visibility["occluders"],
                )
            )
        if visibility["offscreen"]:
            warnings.append(
                _warning("TARGET_OFFSCREEN", "Part of the target is off screen; those pixels are not the game's")
            )
    if diagnostics.is_black(captured.image):
        warnings.append(
            _warning("BLACK_FRAME", "The captured frame is (almost) black", backend=captured.backend)
        )
    return captured, warnings


def _discard_wgc_session(hwnd: int) -> None:
    capture_base._get_backend("wgc").discard(hwnd)


def _identical_to_previous(
    resolved: TargetInfo,
    rect: Rect,
    captured: CaptureResult,
    digest: str,
    previous: diagnostics.PreviousFrame,
    cpu_ms: int | None,
    timeout_ms: int | None,
) -> tuple[CaptureResult, str, dict | None]:
    """The frame is pixel-identical to this window's previous capture. That is
    either a static scene or a frozen capture (WGC replays its last frame under
    ReShade / independent flip), and a heuristic cannot tell them apart, so when
    WGC produced it we ask a screen backend. Returns (result, digest, warning);
    the result is replaced by the screen frame when the two differ."""
    details: dict = {
        "previous_frame_id": previous.frame_id,
        "ms_since_previous": int((diagnostics.HISTORY.now() - previous.at) * 1000),
    }
    if cpu_ms is not None and previous.cpu_ms is not None:
        details["cpu_ms_since_previous"] = max(0, cpu_ms - previous.cpu_ms)
    message = (
        "This frame is pixel-identical to the previous capture of this window: nothing changed, "
        "or the capture is stalled"
    )

    if captured.backend != "wgc":
        return captured, digest, _warning("FRAME_IDENTICAL_TO_PREVIOUS", message, cross_check="not_applicable", **details)

    screen: CaptureResult | None = None
    visibility = diagnostics.probe_visibility(resolved, rect)
    if not visibility["occluders"] and not visibility["offscreen"] and diagnostics.within_one_monitor(rect):
        try:
            screen = capture_region(rect, backend="auto", target=resolved, timeout_ms=timeout_ms)
        except Exception:  # noqa: BLE001 - no usable screen backend: report as unavailable below
            screen = None
    if screen is None:
        return captured, digest, _warning("FRAME_IDENTICAL_TO_PREVIOUS", message, cross_check="unavailable", **details)
    screen_digest = diagnostics.frame_hash(screen.image)
    if screen_digest == digest:
        return captured, digest, None  # both agree: a genuinely static scene
    changed = diagnostics.diff_fraction(captured.image, screen.image)
    log.info("identical-frame cross-check: %.4f of the pixels differ", changed)
    if changed <= CROSS_CHECK_DIFF_FRACTION:
        # A cursor, a blinking caret or a capture-indicator flash is not a
        # frozen capture; a stalled one differs across most of the frame.
        return captured, digest, None
    details["diff_fraction"] = round(changed, 4)
    _discard_wgc_session(resolved.hwnd)
    return (
        screen,
        screen_digest,
        _warning(
            "FRAME_IDENTICAL_TO_PREVIOUS",
            message + "; a screen capture differs, so the Windows Graphics Capture session was discarded",
            cross_check="differs",
            discarded_backend="wgc",
            **details,
        ),
    )


def _make_thumb(image: Image.Image, thumb_width: int | None) -> Image.Image | None:
    if not thumb_width or thumb_width <= 0 or thumb_width >= image.width:
        return None
    height = max(1, int(round(image.height * thumb_width / image.width)))
    return image.resize((int(thumb_width), height), Image.LANCZOS)


def capture_target(
    target: int | dict,
    *,
    region: list[int] | None = None,
    scope: str = "client",
    backend: str = "auto",
    max_width: int = 1920,
    cache: FrameCache | None = None,
    timeout_ms: int | None = None,
    thumb_width: int | None = None,
    wgc_first: bool = False,
) -> dict:
    try:
        resolved = targets.resolve_target(target)
    except targets.TargetAmbiguous as exc:
        return error_response(
            "TARGET_AMBIGUOUS",
            "The target matches windows of more than one process; pass pid or hwnd, or narrow exe/title",
            retryable=False,
            candidates=exc.candidates,
        )
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
        captured, warnings = _capture_with_escalation(resolved, rect, backend, timeout_ms, wgc_first)
    except CaptureError as exc:
        return error_response(exc.code, str(exc), retryable=exc.retryable, **{"backend": backend, **exc.details})
    except Exception as exc:
        return error_response(
            "CAPTURE_FAILED",
            "Capture backend failed",
            retryable=True,
            backend=backend,
            reason=str(exc),
        )
    capture_qpc_ns = time.perf_counter_ns()

    digest = diagnostics.frame_hash(captured.image)
    previous = diagnostics.HISTORY.previous(resolved.hwnd)
    cpu_ms = win32.process_cpu_ms(resolved.pid)
    if previous is not None and previous.digest == digest:
        captured, digest, stale = _identical_to_previous(
            resolved, rect, captured, digest, previous, cpu_ms, timeout_ms
        )
        if stale is not None:
            warnings.append(stale)

    hdr_info = HDR.inspect(rect, resolved.exe)
    hdr_warning = hdr.warning_for(hdr_info, resolved.exe)
    if hdr_warning is not None:
        warnings.append(hdr_warning)

    image, scale = _resize_if_needed(captured.image, max_width)
    thumb = _make_thumb(image, thumb_width)
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
        "backend": {"name": captured.backend, "mode": captured.mode, **captured.extra},
        "frame_hash": digest,
        "hdr": hdr_info,
        # QPC clock, comparable with the qpc_ns of input edges: when the frame was
        # handed over, and (WGC only) when the OS composed it.
        "timing": {"capture_qpc_ns": capture_qpc_ns, "frame_qpc_ns": captured.extra.get("frame_qpc_ns")},
    }
    if thumb is not None:
        metadata["thumb"] = {
            "width": thumb.width,
            "height": thumb.height,
            "scale": thumb.width / image.width,  # relative to the full image
        }
    if warnings:
        metadata["warnings"] = warnings

    cache_instance = cache or FrameCache()
    record = cache_instance.store(image, metadata, thumb=thumb)
    diagnostics.HISTORY.record(resolved.hwnd, digest, record.frame_id, cpu_ms)
    fields = dict(
        frame_id=record.frame_id,
        image_path=str(record.image_path),
        target=record.metadata["target"],
        image=record.metadata["image"],
        geometry=record.metadata["geometry"],
        backend=record.metadata["backend"],
        metadata_path=str(record.metadata_path),
        created_at=record.metadata["created_at"],
        frame_hash=digest,
        hdr=hdr_info,
        timing=metadata["timing"],
    )
    if thumb is not None:
        fields["thumb_path"] = str(record.thumb_path)
        fields["thumb"] = metadata["thumb"]
    if warnings:
        fields["warnings"] = warnings
    return ok_response(**fields)
