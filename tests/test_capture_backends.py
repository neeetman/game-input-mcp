from __future__ import annotations

import pytest
from PIL import Image

import windows_input_mcp.capture.base as base
from windows_input_mcp.capture.base import CaptureBackend, capture_region
from windows_input_mcp.models import Rect


@pytest.fixture(autouse=True)
def isolate_backend_registry(monkeypatch):
    original = dict(CaptureBackend.registry)
    monkeypatch.setattr(CaptureBackend, "registry", dict(original))


def test_backend_subclass_registers_by_name() -> None:
    class DummyBackend(CaptureBackend):
        name = "dummy"
        priority = 1

        def is_available(self, rect):
            return True

        def capture(self, rect):
            return Image.new("RGB", (1, 1), "white")

    assert CaptureBackend.registry["dummy"] is DummyBackend


def test_capture_package_import_registers_backends():
    import importlib

    capture = importlib.import_module("windows_input_mcp.capture")
    assert {"dxcam", "mss", "pillow"} <= set(capture.CaptureBackend.registry)
    assert capture.CaptureBackend is CaptureBackend
    assert capture.CaptureResult.__name__ == "CaptureResult"
    assert capture.capture_region is base.capture_region


def test_auto_capture_uses_priority_order(monkeypatch) -> None:
    monkeypatch.setattr(base.CaptureBackend, "registry", {})
    calls: list[str] = []

    class SlowBackend(CaptureBackend):
        name = "slow"
        priority = 20

        def is_available(self, rect):
            return True

        def capture(self, rect):
            calls.append("slow")
            return Image.new("RGB", (1, 1), "red")

    class FastBackend(CaptureBackend):
        name = "fast"
        priority = 10

        def is_available(self, rect):
            return True

        def capture(self, rect):
            calls.append("fast")
            return Image.new("RGB", (2, 2), "blue")

    monkeypatch.setattr(base, "_backend_instances", {})

    result = capture_region(Rect(0, 0, 10, 10), backend="auto")

    assert result.backend == "fast"
    assert result.image.size == (2, 2)
    assert calls == ["fast"]


def test_auto_capture_falls_back_after_failure(monkeypatch) -> None:
    monkeypatch.setattr(base.CaptureBackend, "registry", {})

    class BrokenBackend(CaptureBackend):
        name = "broken"
        priority = 1

        def is_available(self, rect):
            return True

        def capture(self, rect):
            raise Exception("broken")

    class GoodBackend(CaptureBackend):
        name = "good"
        priority = 2

        def is_available(self, rect):
            return True

        def capture(self, rect):
            return Image.new("RGB", (3, 3), "green")

    monkeypatch.setattr(base, "_backend_instances", {})

    result = capture_region(Rect(0, 0, 10, 10), backend="auto")

    assert result.backend == "good"
    assert result.image.size == (3, 3)


def test_unknown_backend_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown capture backend"):
        capture_region(Rect(0, 0, 10, 10), backend="missing")
