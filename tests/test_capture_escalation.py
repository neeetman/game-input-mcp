"""`auto` escalates to WGC when a screen grab cannot be trusted (spec Design 3/4)."""
from __future__ import annotations

import pytest
from PIL import Image

from game_input_mcp.capture import service
from game_input_mcp.capture.base import CaptureError, CaptureResult
from game_input_mcp.frames import FrameCache
from game_input_mcp.models import Rect, TargetInfo


def _target() -> TargetInfo:
    return TargetInfo(
        hwnd=10,
        pid=2,
        title="Game",
        window_rect=Rect(90, 80, 430, 330),
        client_rect_screen=Rect(100, 120, 420, 320),
        client_size=(320, 200),
        client_screen_origin=(100, 120),
        dpi=96,
        is_foreground=True,
    )


def _image(color="red") -> Image.Image:
    return Image.new("RGB", (320, 200), color)


CLEAR = {"occluders": [], "offscreen": False}
COVERED = {"occluders": [{"hwnd": 20, "pid": 5, "exe": "chat.exe", "title": "Chat"}], "offscreen": False}


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = type(
        "State",
        (),
        {
            "visibility": CLEAR,
            "one_monitor": True,
            "calls": [],
            "results": {},     # backend name -> CaptureResult | Exception ; "auto" is the default screen result
            "probes": 0,
            "cache": FrameCache(tmp_path),
        },
    )()
    state.results["auto"] = CaptureResult(_image(), "dxcam", "region")

    def probe(target, rect):
        state.probes += 1
        return state.visibility

    def fake_capture_region(rect, *, backend="auto", target=None, timeout_ms=None, prefer=()):
        state.calls.append({"backend": backend, "prefer": prefer, "timeout_ms": timeout_ms})
        key = prefer[0] if prefer and prefer[0] in state.results else backend
        outcome = state.results.get(key, state.results["auto"])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(service, "capture_region", fake_capture_region)
    monkeypatch.setattr(service.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(service.diagnostics, "probe_visibility", probe)
    monkeypatch.setattr(service.diagnostics, "within_one_monitor", lambda rect: state.one_monitor)
    return state


def _capture(env, **kwargs):
    return service.capture_target({"pid": 2}, cache=env.cache, timeout_ms=1500, **kwargs)


def test_clear_window_stays_on_the_screen_backends(env) -> None:
    result = _capture(env, backend="auto")

    assert result["success"] is True and result["backend"]["name"] == "dxcam"
    assert env.calls == [{"backend": "auto", "prefer": (), "timeout_ms": 1500}]
    assert "warnings" not in result


def test_occluded_window_escalates_to_wgc_and_drops_the_occlusion_warning(env) -> None:
    env.visibility = COVERED
    env.results["wgc"] = CaptureResult(_image("blue"), "wgc", "window", extra={"session": "cold", "frame_age_ms": 4})

    result = _capture(env, backend="auto")

    assert env.calls[0]["prefer"] == ("wgc",)
    assert result["backend"] == {"name": "wgc", "mode": "window", "session": "cold", "frame_age_ms": 4}
    assert "warnings" not in result


def test_occluded_window_without_wgc_returns_a_warning_naming_the_occluder(env) -> None:
    env.visibility = COVERED  # fake_capture_region falls back to the dxcam result

    result = _capture(env, backend="auto")

    assert result["backend"]["name"] == "dxcam"
    [warning] = result["warnings"]
    assert warning["code"] == "TARGET_OCCLUDED"
    assert warning["details"]["by"][0]["exe"] == "chat.exe"


def test_offscreen_part_escalates_and_warns_when_not_served_by_wgc(env) -> None:
    env.visibility = {"occluders": [], "offscreen": True}

    result = _capture(env, backend="auto")

    assert env.calls[0]["prefer"] == ("wgc",)
    assert [w["code"] for w in result["warnings"]] == ["TARGET_OFFSCREEN"]


def test_window_spanning_monitors_escalates(env) -> None:
    env.one_monitor = False

    _capture(env, backend="auto")

    assert env.calls[0]["prefer"] == ("wgc",)


def test_explicit_backend_skips_the_probes_entirely(env) -> None:
    env.visibility = COVERED

    result = _capture(env, backend="dxcam")

    assert env.probes == 0 and env.calls[0]["prefer"] == ()
    assert "warnings" not in result


def test_black_first_frame_is_retried_on_wgc(env) -> None:
    env.results["auto"] = CaptureResult(_image("black"), "dxcam", "region")
    env.results["wgc"] = CaptureResult(_image("green"), "wgc", "window")

    result = _capture(env, backend="auto")

    assert [c["backend"] for c in env.calls] == ["auto", "wgc"]
    assert result["backend"]["name"] == "wgc" and "warnings" not in result


def test_black_frame_is_kept_with_a_warning_when_wgc_cannot_help(env) -> None:
    env.results["auto"] = CaptureResult(_image("black"), "dxcam", "region")
    env.results["wgc"] = CaptureError("CAPTURE_BACKEND_UNAVAILABLE", "no library", retryable=False)

    result = _capture(env, backend="auto")

    assert result["success"] is True and result["backend"]["name"] == "dxcam"
    assert [w["code"] for w in result["warnings"]] == ["BLACK_FRAME"]


def test_wgc_black_frame_is_not_retried_but_is_flagged(env) -> None:
    env.visibility = COVERED
    env.results["wgc"] = CaptureResult(_image("black"), "wgc", "window")

    result = _capture(env, backend="auto")

    assert [c["backend"] for c in env.calls] == ["auto"]
    assert [w["code"] for w in result["warnings"]] == ["BLACK_FRAME"]


def test_backend_warnings_are_passed_through_and_stored_with_the_frame(env) -> None:
    warning = {"code": "WGC_NO_NEW_FRAME", "message": "static", "details": {"frame_age_ms": 3000}}
    env.results["wgc"] = CaptureResult(_image(), "wgc", "window", warnings=(warning,))

    result = _capture(env, backend="wgc")

    assert result["warnings"] == [warning]
    stored = env.cache.get(result["frame_id"])
    assert stored.metadata["warnings"] == [warning]


def test_capture_error_codes_are_preserved(env) -> None:
    env.results["wgc"] = CaptureError("CAPTURE_TIMEOUT", "no frame", hwnd=10, timeout_ms=1500)

    result = _capture(env, backend="wgc")

    assert result["success"] is False
    assert result["error_code"] == "CAPTURE_TIMEOUT" and result["retryable"] is True
    assert result["details"]["timeout_ms"] == 1500 and result["details"]["backend"] == "wgc"


def test_capture_error_details_may_name_the_backend_themselves(env) -> None:
    env.results["wgc"] = CaptureError(
        "CAPTURE_BACKEND_UNAVAILABLE", "no library", retryable=False, backend="wgc", reason="not installed"
    )

    result = _capture(env, backend="wgc")

    assert result["error_code"] == "CAPTURE_BACKEND_UNAVAILABLE"
    assert result["details"] == {"backend": "wgc", "reason": "not installed"}


def test_unexpected_backend_exceptions_still_become_capture_failed(env) -> None:
    env.results["auto"] = RuntimeError("boom")

    result = _capture(env, backend="auto")

    assert result["error_code"] == "CAPTURE_FAILED" and result["details"]["reason"] == "boom"


def test_windows_graphics_capture_alias_is_treated_as_explicit(env) -> None:
    env.visibility = COVERED

    _capture(env, backend="windows_graphics_capture")

    assert env.probes == 0
