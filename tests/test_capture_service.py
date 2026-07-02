from __future__ import annotations

from PIL import Image

from windows_input_mcp import targets
from windows_input_mcp.capture import service
from windows_input_mcp.frames import FrameCache
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


def test_capture_target_stores_frame_and_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(
        service,
        "capture_region",
        lambda rect, backend: service.CaptureResult(Image.new("RGB", (320, 200), "red"), "fake", "region"),
    )
    cache = FrameCache(tmp_path, ttl_sec=60)

    result = service.capture_target({"pid": 2}, backend="fake", cache=cache)

    assert result["success"] is True
    assert result["frame_id"].startswith("frame_")
    assert result["image"]["width"] == 320
    assert result["image"]["height"] == 200
    assert result["geometry"]["capture_rect_screen"] == [100, 120, 420, 320]
    assert result["backend"]["name"] == "fake"
    assert cache.get(result["frame_id"]) is not None


def test_capture_target_builds_frame_geometry(monkeypatch, tmp_path) -> None:
    built = []

    class RecordingFrameGeometry(service.FrameGeometry):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(service, "FrameGeometry", RecordingFrameGeometry)
    monkeypatch.setattr(
        service,
        "capture_region",
        lambda rect, backend: service.CaptureResult(Image.new("RGB", (320, 200), "red"), "fake", "region"),
    )

    result = service.capture_target({"pid": 2}, backend="fake", cache=FrameCache(tmp_path))

    assert result["success"] is True
    assert len(built) == 1
    assert built[0].image_size == (320, 200)
    assert built[0].capture_rect_screen == Rect(100, 120, 420, 320)


def test_capture_target_returns_structured_error_for_missing_target(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(targets, "resolve_target", lambda target: None)

    result = service.capture_target({"pid": 2}, cache=FrameCache(tmp_path))

    assert result["success"] is False
    assert result["error_code"] == "TARGET_NOT_FOUND"


def test_capture_target_returns_structured_error_for_minimized_target(monkeypatch) -> None:
    minimized = TargetInfo(
        hwnd=1,
        pid=2,
        title="Game",
        window_rect=Rect(90, 80, 500, 400),
        client_rect_screen=Rect(100, 120, 420, 320),
        client_size=(320, 200),
        client_screen_origin=(100, 120),
        dpi=144,
        is_foreground=True,
        is_minimized=True,
    )
    monkeypatch.setattr(targets, "resolve_target", lambda target: minimized)

    result = service.capture_target({"pid": 2})

    assert result["success"] is False
    assert result["error_code"] == "TARGET_MINIMIZED"


def test_capture_target_returns_structured_error_for_invalid_region(monkeypatch) -> None:
    monkeypatch.setattr(targets, "resolve_target", lambda target: _target())

    result = service.capture_target({"pid": 2}, region=[0, 0, 10], scope="client")

    assert result["success"] is False
    assert result["error_code"] == "INVALID_REGION"


def test_capture_target_returns_structured_error_for_invalid_scope(monkeypatch) -> None:
    monkeypatch.setattr(targets, "resolve_target", lambda target: _target())

    result = service.capture_target({"pid": 2}, scope="desk")

    assert result["success"] is False
    assert result["error_code"] == "INVALID_REGION"
