from __future__ import annotations

import pytest

from windows_input_mcp.models import (
    Rect,
    TargetInfo,
    TargetSpec,
    error_response,
    ok_response,
)


def test_rect_width_height_and_list() -> None:
    rect = Rect(left=-10, top=20, right=90, bottom=70)

    assert rect.width == 100
    assert rect.height == 50
    assert rect.to_list() == [-10, 20, 90, 70]


def test_target_spec_accepts_legacy_pid() -> None:
    spec = TargetSpec.from_value(1234)

    assert spec.pid == 1234
    assert spec.hwnd is None


def test_target_spec_accepts_target_dict() -> None:
    spec = TargetSpec.from_value({"pid": 1234, "hwnd": 5678})

    assert spec.pid == 1234
    assert spec.hwnd == 5678


def test_target_spec_rejects_empty_target() -> None:
    with pytest.raises(ValueError, match="pid or hwnd"):
        TargetSpec.from_value({})


def test_response_helpers_have_stable_shape() -> None:
    assert ok_response(pid=1) == {"success": True, "pid": 1}
    assert error_response(
        "TARGET_NOT_FOUND",
        "No target",
        retryable=True,
        pid=1,
    ) == {
        "success": False,
        "error_code": "TARGET_NOT_FOUND",
        "message": "No target",
        "retryable": True,
        "details": {"pid": 1},
    }
    assert error_response("TARGET_NOT_FOUND", "No target", True, pid=1) == {
        "success": False,
        "error_code": "TARGET_NOT_FOUND",
        "message": "No target",
        "retryable": True,
        "details": {"pid": 1},
    }


def test_target_info_to_dict_is_json_friendly() -> None:
    info = TargetInfo(
        hwnd=10,
        pid=20,
        title="Game",
        window_rect=Rect(1, 2, 301, 202),
        client_rect_screen=Rect(11, 22, 291, 182),
        client_size=(280, 160),
        client_screen_origin=(11, 22),
        dpi=144,
        is_foreground=True,
        is_minimized=False,
    )

    assert info.to_dict() == {
        "hwnd": 10,
        "pid": 20,
        "title": "Game",
        "window_rect": [1, 2, 301, 202],
        "client_rect_screen": [11, 22, 291, 182],
        "client_size": [280, 160],
        "client_screen_origin": [11, 22],
        "dpi": 144,
        "is_foreground": True,
        "is_minimized": False,
    }
