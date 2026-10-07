"""Frame hash, identical-frame cross-check, thumbnails, timing and HDR through
the capture service (Phase 4 of the 2026-10-07 spec)."""
from __future__ import annotations

import pytest
from PIL import Image

from game_input_mcp.capture import diagnostics, service
from game_input_mcp.capture.base import CaptureResult
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
        exe="Game.exe",
    )


def _image(color="red", size=(320, 200)) -> Image.Image:
    return Image.new("RGB", size, color)


@pytest.fixture
def env(monkeypatch, tmp_path):
    clock = [1000.0]
    cpu = [5000]
    state = type(
        "State",
        (),
        {
            "wgc": CaptureResult(_image("red"), "wgc", "window", extra={"frame_qpc_ns": 777}),
            "screen": CaptureResult(_image("red"), "dxcam", "region"),
            "calls": [],
            "discarded": [],
            "visibility": {"occluders": [], "offscreen": False},
            "clock": clock,
            "cpu": cpu,
            "cache": FrameCache(tmp_path),
        },
    )()
    monkeypatch.setattr(diagnostics, "HISTORY", diagnostics.FrameHistory(clock=lambda: clock[0]))

    def fake_capture_region(rect, *, backend="auto", target=None, timeout_ms=None, prefer=()):
        state.calls.append((backend, prefer))
        if backend == "wgc" or prefer == ("wgc",):
            return state.wgc
        if isinstance(state.screen, Exception):
            raise state.screen
        return state.screen

    monkeypatch.setattr(service, "capture_region", fake_capture_region)
    monkeypatch.setattr(service.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(service.diagnostics, "probe_visibility", lambda t, r: state.visibility)
    monkeypatch.setattr(service.diagnostics, "within_one_monitor", lambda r: True)
    monkeypatch.setattr(service.win32, "process_cpu_ms", lambda pid: state.cpu[0])
    monkeypatch.setattr(service, "_discard_wgc_session", lambda hwnd: state.discarded.append(hwnd))
    return state


def _capture(env, **kwargs):
    kwargs.setdefault("backend", "wgc")
    return service.capture_target({"pid": 2}, cache=env.cache, timeout_ms=500, **kwargs)


def _codes(result) -> list[str]:
    return [w["code"] for w in result.get("warnings", [])]


def test_frame_hash_is_stable_and_pixel_exact() -> None:
    a, b = _image("red"), _image("red")
    c = _image("red")
    c.putpixel((3, 3), (254, 0, 0))

    assert diagnostics.frame_hash(a) == diagnostics.frame_hash(b)
    assert diagnostics.frame_hash(a) != diagnostics.frame_hash(c)
    assert len(diagnostics.frame_hash(a)) == 16


def test_history_ignores_old_entries_and_is_bounded() -> None:
    now = [0.0]
    history = diagnostics.FrameHistory(ttl_s=60, max_entries=2, clock=lambda: now[0])
    history.record(1, "a", "f1", 10)
    assert history.previous(1).digest == "a"

    now[0] = 61.0
    assert history.previous(1) is None

    history.record(1, "a", "f1", 10)
    history.record(2, "b", "f2", 10)
    history.record(3, "c", "f3", 10)
    assert history.previous(1) is None and history.previous(3).digest == "c"


def test_first_capture_reports_hash_timing_and_hdr_fields(env) -> None:
    result = _capture(env)

    assert result["success"] is True
    assert result["frame_hash"] == diagnostics.frame_hash(_image("red"))
    assert result["hdr"] == {"display_hdr": False, "auto_hdr": None, "auto_hdr_scope": None}
    assert result["timing"]["frame_qpc_ns"] == 777
    assert isinstance(result["timing"]["capture_qpc_ns"], int) and result["timing"]["capture_qpc_ns"] > 0
    stored = env.cache.get(result["frame_id"]).metadata
    assert stored["frame_hash"] == result["frame_hash"] and stored["timing"] == result["timing"]
    assert "warnings" not in result


def test_capture_qpc_is_on_the_same_clock_as_input_edges(env) -> None:
    import time

    before = time.perf_counter_ns()
    result = _capture(env)
    after = time.perf_counter_ns()

    assert before <= result["timing"]["capture_qpc_ns"] <= after


def test_changed_frame_has_no_stale_warning(env) -> None:
    _capture(env)
    env.wgc = CaptureResult(_image("blue"), "wgc", "window")

    assert "warnings" not in _capture(env)


def test_wgc_identical_frame_confirmed_static_by_a_screen_capture_is_not_warned(env) -> None:
    _capture(env)

    result = _capture(env)

    assert "warnings" not in result
    assert env.discarded == []
    assert ("auto", ()) in env.calls  # the screen backend was asked


def test_wgc_identical_frame_that_a_screen_capture_contradicts_is_a_frozen_session(env) -> None:
    first = _capture(env)
    env.screen = CaptureResult(_image("green"), "dxcam", "region")
    env.clock[0] += 3.0
    env.cpu[0] += 2400

    result = _capture(env)

    assert env.discarded == [10]
    assert result["backend"]["name"] == "dxcam"  # the fresh screen frame is returned
    assert result["frame_hash"] == diagnostics.frame_hash(_image("green"))
    [warning] = result["warnings"]
    assert warning["code"] == "FRAME_IDENTICAL_TO_PREVIOUS"
    details = warning["details"]
    assert details["cross_check"] == "differs" and details["discarded_backend"] == "wgc"
    assert details["previous_frame_id"] == first["frame_id"]
    assert details["ms_since_previous"] == 3000 and details["cpu_ms_since_previous"] == 2400


def test_history_follows_the_replacement_frame(env) -> None:
    _capture(env)
    env.screen = CaptureResult(_image("green"), "dxcam", "region")
    _capture(env)  # replaced by green

    env.wgc = CaptureResult(_image("green"), "wgc", "window")  # wgc has caught up
    result = _capture(env)

    assert "warnings" not in result  # identical to green, and the screen agrees


def test_wgc_identical_frame_without_a_usable_screen_backend_is_flagged_unavailable(env) -> None:
    _capture(env)
    env.screen = RuntimeError("no backend")

    result = _capture(env)

    assert env.discarded == []
    assert result["warnings"][0]["details"]["cross_check"] == "unavailable"


def test_no_cross_check_when_the_window_is_covered(env) -> None:
    _capture(env)
    env.visibility = {"occluders": [{"hwnd": 99}], "offscreen": False}
    calls_before = len(env.calls)

    result = _capture(env)

    assert len(env.calls) == calls_before + 1  # only the WGC capture itself
    assert result["warnings"][0]["details"]["cross_check"] == "unavailable"


def test_screen_backend_identical_frame_warns_without_cross_check(env) -> None:
    env.screen = CaptureResult(_image("red"), "dxcam", "region")
    _capture(env, backend="dxcam")

    result = _capture(env, backend="dxcam")

    [warning] = result["warnings"]
    assert warning["code"] == "FRAME_IDENTICAL_TO_PREVIOUS"
    assert warning["details"]["cross_check"] == "not_applicable"
    assert env.discarded == []


def test_a_stale_history_entry_is_ignored(env) -> None:
    _capture(env)
    env.clock[0] += 61.0

    assert "warnings" not in _capture(env)
    assert ("auto", ()) not in env.calls


def test_unknown_cpu_time_is_simply_left_out(env, monkeypatch) -> None:
    monkeypatch.setattr(service.win32, "process_cpu_ms", lambda pid: None)
    env.screen = CaptureResult(_image("red"), "dxcam", "region")
    _capture(env, backend="dxcam")

    result = _capture(env, backend="dxcam")

    assert "cpu_ms_since_previous" not in result["warnings"][0]["details"]


def test_hdr_warning_is_added_when_the_probe_says_so(env, monkeypatch) -> None:
    class Hdr:
        def inspect(self, rect, exe):
            assert exe == "Game.exe" and rect == Rect(100, 120, 420, 320)
            return {"display_hdr": True, "auto_hdr": True, "auto_hdr_scope": "exe"}

    monkeypatch.setattr(service, "HDR", Hdr())

    result = _capture(env)

    assert result["hdr"]["display_hdr"] is True
    assert _codes(result) == ["HDR_COLOR_SHIFT_POSSIBLE"]
    assert result["warnings"][0]["details"]["exe"] == "Game.exe"


def test_thumbnail_is_a_scaled_copy_of_the_same_frame(env) -> None:
    result = _capture(env, thumb_width=80)

    assert result["thumb"] == {"width": 80, "height": 50, "scale": 0.25}
    assert Image.open(result["thumb_path"]).size == (80, 50)
    assert Image.open(result["image_path"]).size == (320, 200)
    assert result["thumb_path"].endswith(f"thumb_{result['frame_id']}.png")
    assert env.cache.get(result["frame_id"]).thumb_path is not None


def test_thumbnail_scale_is_relative_to_the_returned_image_after_max_width(env) -> None:
    result = _capture(env, thumb_width=80, max_width=160)

    assert result["image"]["width"] == 160
    assert result["thumb"] == {"width": 80, "height": 50, "scale": 0.5}


@pytest.mark.parametrize("thumb_width", [None, 0, -5, 320, 999])
def test_no_thumbnail_unless_it_is_actually_smaller(env, thumb_width) -> None:
    result = _capture(env, thumb_width=thumb_width)

    assert "thumb" not in result and "thumb_path" not in result
    assert env.cache.get(result["frame_id"]).thumb_path is None


def test_diff_fraction_counts_only_pixels_that_changed_noticeably() -> None:
    base = _image("red", (100, 100))
    noisy = _image("red", (100, 100))
    noisy.putpixel((0, 0), (255 - 5, 0, 0))     # within the per-channel threshold
    changed = _image("red", (100, 100))
    for x in range(10):
        changed.putpixel((x, 0), (0, 0, 0))     # 10 of 10 000 pixels

    assert diagnostics.diff_fraction(base, base) == 0.0
    assert diagnostics.diff_fraction(base, noisy) == 0.0
    assert diagnostics.diff_fraction(base, changed) == pytest.approx(0.001)
    assert diagnostics.diff_fraction(base, _image("red", (50, 50))) == 1.0


def test_a_cursor_sized_difference_is_not_a_frozen_capture(env) -> None:
    _capture(env)
    nearly_same = _image("red")
    for x in range(8):
        for y in range(8):
            nearly_same.putpixel((x, y), (0, 0, 255))  # 64 of 64 000 pixels = 0.1 %
    env.screen = CaptureResult(nearly_same, "dxcam", "region")

    result = _capture(env)

    assert "warnings" not in result
    assert env.discarded == []
    assert result["backend"]["name"] == "wgc"


def test_a_large_difference_reports_how_large(env) -> None:
    _capture(env)
    env.screen = CaptureResult(_image("green"), "dxcam", "region")

    result = _capture(env)

    assert result["warnings"][0]["details"]["diff_fraction"] == 1.0
    assert env.discarded == [10]
