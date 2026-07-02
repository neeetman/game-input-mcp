from __future__ import annotations

import pytest

from windows_input_mcp.geometry import FrameGeometry, point_to_screen
from windows_input_mcp.models import Rect, TargetInfo


def _target() -> TargetInfo:
    return TargetInfo(
        hwnd=1,
        pid=2,
        title="Game",
        window_rect=Rect(90, 80, 500, 400),
        client_rect_screen=Rect(100, 120, 420, 320),
        client_size=(320, 200),
        client_screen_origin=(100, 120),
        dpi=144,
        is_foreground=True,
    )


def test_client_to_screen() -> None:
    assert point_to_screen(10, 20, "client", _target()) == (110, 140)


def test_screen_passthrough() -> None:
    assert point_to_screen(-50, 25, "screen", _target()) == (-50, 25)


def test_capture_to_screen_uses_frame_geometry() -> None:
    frame = FrameGeometry(
        image_size=(160, 100),
        capture_rect_screen=Rect(100, 120, 420, 320),
        client_rect_screen=Rect(100, 120, 420, 320),
    )

    assert point_to_screen(80, 50, "capture", _target(), frame=frame) == (260, 220)


def test_normalized_to_screen_uses_frame_geometry() -> None:
    frame = FrameGeometry(
        image_size=(160, 100),
        capture_rect_screen=Rect(100, 120, 420, 320),
        client_rect_screen=Rect(100, 120, 420, 320),
    )

    assert point_to_screen(0.5, 0.5, "normalized", _target(), frame=frame) == (260, 220)


def test_framebuffer_alias_uses_explicit_size_without_frame() -> None:
    assert point_to_screen(80, 50, "framebuffer", _target(), framebuffer_size=(160, 100)) == (260, 220)


def test_capture_without_frame_raises_clear_error() -> None:
    with pytest.raises(ValueError, match="frame geometry"):
        point_to_screen(1, 2, "capture", _target())
