from __future__ import annotations

import numpy as np
import pytest

from game_input_mcp.capture import _wgc_native, base, wgc
from game_input_mcp.capture.base import CaptureError, capture_region
from game_input_mcp.models import Rect, TargetInfo

# Window geometry used throughout: the visible frame (DWM extended bounds) is a
# little bigger than the client area, like a real captioned window.
CLIENT = Rect(100, 120, 420, 320)            # 320x200
EXTENDED = (99, 90, 421, 321)                # 322x231 -> client starts at (1, 30)
FRAME_W, FRAME_H = 322, 231


def _target(**overrides) -> TargetInfo:
    fields = dict(
        hwnd=5,
        pid=9,
        title="Game",
        window_rect=Rect(90, 80, 430, 330),
        client_rect_screen=CLIENT,
        client_size=(320, 200),
        client_screen_origin=(100, 120),
        dpi=96,
        is_foreground=True,
    )
    fields.update(overrides)
    return TargetInfo(**fields)


def _pixels(width=FRAME_W, height=FRAME_H) -> np.ndarray:
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[..., 3] = 255
    # marker at client (10, 5) -> frame (11, 35); BGRA = (10, 20, 30)
    pixels[35, 11] = (10, 20, 30, 255)
    return pixels


class FakeSource:
    """A WgcSource whose frames the test pushes by hand."""

    def __init__(self) -> None:
        self.frames: list[_wgc_native.WgcFrame] = []
        self.waits: list[tuple[int, float]] = []
        self.is_closed = False
        self.close_calls = 0

    def push(self, pixels=None, qpc_ns=1_000_000_000, width=FRAME_W, height=FRAME_H) -> None:
        pixels = _pixels(width, height) if pixels is None else pixels
        self.frames.append(
            _wgc_native.WgcFrame(pixels=pixels, width=width, height=height, qpc_ns=qpc_ns, seq=len(self.frames) + 1)
        )

    def latest(self):
        return self.frames[-1] if self.frames else None

    def wait_newer(self, seq, timeout_s):
        self.waits.append((seq, timeout_s))
        return self.latest()

    @property
    def closed(self) -> bool:
        return self.is_closed

    def close(self) -> None:
        self.close_calls += 1
        self.is_closed = True


@pytest.fixture
def env(monkeypatch):
    state = type(
        "State",
        (),
        {
            "sources": [],
            "opened": [],
            "now": 100.0,
            "qpc": 1_000_000_000,
            "extended": EXTENDED,
            "current_window": None,
        },
    )()

    def open_source(hwnd):
        source = FakeSource()
        source.push(qpc_ns=state.qpc)
        state.sources.append(source)
        state.opened.append(hwnd)
        return source

    monkeypatch.setattr(wgc, "open_source", open_source)
    monkeypatch.setattr(wgc, "library_available", lambda: True)
    monkeypatch.setattr(wgc, "clock", lambda: state.now)
    monkeypatch.setattr(wgc, "qpc_ns", lambda: state.qpc)
    monkeypatch.setattr(wgc, "IDLE_TTL_S", 5.0)
    monkeypatch.setattr(wgc.win32, "extended_frame_bounds", lambda hwnd: state.extended)
    monkeypatch.setattr(wgc.win32, "get_window_info_by_hwnd", lambda hwnd: state.current_window)
    state.backend = wgc.WgcBackend()
    return state


def test_crop_to_client_area_with_rgb_channel_order(env) -> None:
    result = env.backend.capture_for(_target(), None)

    assert result.backend == "wgc" and result.mode == "window"
    assert result.image.size == (320, 200)
    assert result.image.getpixel((10, 5)) == (30, 20, 10)  # BGRA -> RGB
    assert result.extra["client_crop"] == [1, 30, 321, 230]
    assert result.extra["session"] == "cold"
    assert result.extra["frame_qpc_ns"] == 1_000_000_000


def test_region_capture_crops_inside_the_client_area(env) -> None:
    result = env.backend.capture_for(_target(), Rect(110, 125, 130, 135))

    assert result.image.size == (20, 10)
    assert result.image.getpixel((0, 0)) == (30, 20, 10)
    assert result.extra["client_crop"] == [11, 35, 31, 45]


def test_negative_origin_monitor_geometry(env) -> None:
    env.extended = (-1801, 90, -1479, 321)
    target = _target(
        client_rect_screen=Rect(-1800, 120, -1480, 320),
        client_screen_origin=(-1800, 120),
        window_rect=Rect(-1810, 80, -1470, 330),
    )

    result = env.backend.capture_for(target, None)

    assert result.image.size == (320, 200)
    assert result.extra["client_crop"] == [1, 30, 321, 230]


def test_warm_session_is_reused_and_only_waits_a_short_grace(env) -> None:
    env.backend.capture_for(_target(), None, 1500)
    env.backend.capture_for(_target(), None, 1500)

    assert env.opened == [5]
    cold_wait, warm_wait = env.sources[0].waits
    assert cold_wait == (0, 1.5)
    assert warm_wait[1] == pytest.approx(wgc.FRESH_GRACE_S)


def test_stale_latest_frame_is_returned_with_a_warning(env) -> None:
    env.backend.capture_for(_target(), None)
    env.qpc += 3_000_000_000  # 3 s later, nothing has redrawn

    result = env.backend.capture_for(_target(), None)

    warning = next(w for w in result.warnings if w["code"] == "WGC_NO_NEW_FRAME")
    assert warning["details"]["frame_age_ms"] == 3000
    assert result.extra["session"] == "warm"


def test_fresh_frame_has_no_warning(env) -> None:
    env.qpc += 50_000_000  # 50 ms

    assert env.backend.capture_for(_target(), None).warnings == ()


def test_no_frame_at_all_is_a_timeout(env, monkeypatch) -> None:
    source = FakeSource()  # never produces a frame
    monkeypatch.setattr(wgc, "open_source", lambda hwnd: source)

    with pytest.raises(CaptureError) as caught:
        env.backend.capture_for(_target(), None, 800)

    assert caught.value.code == "CAPTURE_TIMEOUT"
    assert caught.value.details["timeout_ms"] == 800
    assert caught.value.details["session"] == "cold"


def test_resized_window_reopens_the_session_once(env, monkeypatch) -> None:
    stale = FakeSource()
    stale.push(width=200, height=100)  # frame from before the resize
    good = FakeSource()
    good.push()
    sources = iter([stale, good])
    monkeypatch.setattr(wgc, "open_source", lambda hwnd: next(sources))

    result = env.backend.capture_for(_target(), None)

    assert result.image.size == (320, 200)
    assert stale.close_calls == 1


def test_persistent_size_mismatch_fails_with_details(env, monkeypatch) -> None:
    monkeypatch.setattr(wgc, "open_source", lambda hwnd: _always_wrong())

    with pytest.raises(CaptureError) as caught:
        env.backend.capture_for(_target(), None)

    assert caught.value.code == "CAPTURE_FAILED"
    assert caught.value.details["frame_size"] == [200, 100]


def _always_wrong() -> FakeSource:
    source = FakeSource()
    source.push(width=200, height=100)
    return source


def test_region_outside_the_captured_window_is_rejected(env) -> None:
    with pytest.raises(CaptureError) as caught:
        env.backend.capture_for(_target(), Rect(100, 120, 430, 320))  # 10 px past the right edge

    assert caught.value.code == "CAPTURE_REGION_UNSUPPORTED"
    assert caught.value.retryable is False


def test_extended_bounds_unreadable_is_a_capture_failure(env) -> None:
    env.extended = None

    with pytest.raises(CaptureError) as caught:
        env.backend.capture_for(_target(), None)

    assert caught.value.code == "CAPTURE_FAILED"


def test_window_moved_during_capture_is_reported(env) -> None:
    env.current_window = type(
        "Info", (), {"window_rect": (95, 80, 435, 330), "client_screen_origin": (105, 120)}
    )()

    result = env.backend.capture_for(_target(), None)

    assert [w["code"] for w in result.warnings] == ["WINDOW_MOVED_DURING_CAPTURE"]


def test_unchanged_window_reports_no_move(env) -> None:
    env.current_window = type(
        "Info", (), {"window_rect": (90, 80, 430, 330), "client_screen_origin": (100, 120)}
    )()

    assert env.backend.capture_for(_target(), None).warnings == ()


def test_idle_sessions_expire_and_closed_ones_are_replaced(env) -> None:
    env.backend.capture_for(_target(), None)
    env.now += 4.0
    assert env.backend.sweep() == 0 and env.backend.session_count() == 1

    env.now += 2.0
    assert env.backend.sweep() == 1
    assert env.sources[0].close_calls == 1 and env.backend.session_count() == 0

    env.backend.capture_for(_target(), None)
    env.sources[1].is_closed = True  # the window went away / capture closed
    env.backend.capture_for(_target(), None)
    assert env.opened == [5, 5, 5]


def test_at_most_four_warm_sessions(env) -> None:
    for hwnd in range(1, 7):
        env.now += 1
        env.backend.capture_for(_target(hwnd=hwnd), None)

    assert env.backend.session_count() == wgc.MAX_SESSIONS
    assert env.sources[0].close_calls == 1 and env.sources[1].close_calls == 1


def test_discard_and_close_all(env) -> None:
    env.backend.capture_for(_target(hwnd=1), None)
    env.backend.capture_for(_target(hwnd=2), None)

    env.backend.discard(1)
    assert env.backend.session_count() == 1
    env.backend.close_all()
    assert env.backend.session_count() == 0 and all(s.close_calls == 1 for s in env.sources)


def test_availability_rules(env, monkeypatch) -> None:
    backend = env.backend
    assert backend.is_available_for(_target(), None)
    assert backend.is_available_for(_target(), Rect(110, 130, 200, 200))
    assert not backend.is_available_for(_target(), Rect(0, 0, 50, 50))
    assert not backend.is_available_for(_target(is_minimized=True), None)
    assert not backend.is_available_for(None, None)
    assert not backend.is_available(None)

    monkeypatch.setattr(wgc, "library_available", lambda: False)
    assert not backend.is_available_for(_target(), None)
    assert "windows-capture" in backend.unavailable_reason(_target(), None)


def test_unavailable_reasons_are_specific(env) -> None:
    backend = env.backend

    assert "minimized" in backend.unavailable_reason(_target(is_minimized=True), None)
    assert "outside" in backend.unavailable_reason(_target(), Rect(0, 0, 50, 50))
    assert "window target" in backend.unavailable_reason(None, None)


def test_wgc_is_registered_but_not_in_the_default_order() -> None:
    assert base.CaptureBackend.registry["wgc"] is wgc.WgcBackend
    auto = [cls.name for cls in base._candidate_classes("auto")]
    assert "wgc" not in auto and auto[:3] == ["dxcam", "mss", "pillow"]
    assert [cls.name for cls in base._candidate_classes("auto", prefer=("wgc",))][0] == "wgc"
    assert [cls.name for cls in base._candidate_classes(base.normalize_backend_name("windows_graphics_capture"))] == ["wgc"]


def test_explicit_wgc_without_the_library_is_a_structured_error(env, monkeypatch) -> None:
    monkeypatch.setattr(wgc, "library_available", lambda: False)
    monkeypatch.setitem(base._backend_instances, "wgc", env.backend)

    with pytest.raises(CaptureError) as caught:
        capture_region(CLIENT, backend="wgc", target=_target())

    assert caught.value.code == "CAPTURE_BACKEND_UNAVAILABLE"
    assert caught.value.retryable is False
    assert "windows-capture" in caught.value.details["reason"]


def test_explicit_wgc_timeout_surfaces_its_own_code(env, monkeypatch) -> None:
    monkeypatch.setattr(wgc, "open_source", lambda hwnd: FakeSource())
    monkeypatch.setitem(base._backend_instances, "wgc", env.backend)

    with pytest.raises(CaptureError) as caught:
        capture_region(CLIENT, backend="wgc", target=_target(), timeout_ms=300)

    assert caught.value.code == "CAPTURE_TIMEOUT"


def test_auto_falls_through_when_wgc_fails_on_escalation(env, monkeypatch) -> None:
    from PIL import Image

    class Screen(base.CaptureBackend):
        name = "fake_screen"
        priority = 50

        def is_available(self, rect):
            return True

        def capture(self, rect):
            return Image.new("RGB", (4, 4), "white")

    monkeypatch.setattr(base.CaptureBackend, "registry", {**base.CaptureBackend.registry, "fake_screen": Screen})
    monkeypatch.setattr(base, "_backend_instances", {"wgc": env.backend})
    monkeypatch.setattr(wgc, "open_source", lambda hwnd: FakeSource())  # never delivers

    result = capture_region(CLIENT, backend="auto", target=_target(), timeout_ms=200, prefer=("wgc",))

    assert result.backend in {"dxcam", "mss", "pillow", "fake_screen"}
