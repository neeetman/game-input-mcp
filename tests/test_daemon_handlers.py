from __future__ import annotations

from windows_input_mcp import daemon
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


def _frame_cache_record():
    return type(
        "Record",
        (),
        {
            "metadata": {
                "geometry": {
                    "capture_rect_screen": [100, 120, 420, 320],
                    "client_rect_screen": [100, 120, 420, 320],
                },
                "image": {"width": 160, "height": 100},
            },
        },
    )()


def test_list_targets_handler(monkeypatch) -> None:
    monkeypatch.setattr(daemon.targets, "list_targets", lambda: [_target()])

    result = daemon._h_list_targets({})

    assert result["success"] is True
    assert result["targets"][0]["pid"] == 2


def test_get_target_info_handler_resolves_pid_or_target(monkeypatch) -> None:
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())

    assert daemon._h_get_target_info({"pid": 2})["target"]["pid"] == 2
    assert daemon._h_get_target_info({"target": {"pid": 2}})["target"]["pid"] == 2


def test_new_target_handlers_return_structured_target_error(monkeypatch) -> None:
    def raise_value_error(target):
        raise ValueError("target must include pid or hwnd")

    monkeypatch.setattr(daemon.targets, "resolve_target", raise_value_error)

    assert daemon._h_get_target_info({"target": {}})["error_code"] == "TARGET_NOT_FOUND"
    assert daemon._h_focus_target({"target": {}})["error_code"] == "TARGET_NOT_FOUND"
    assert daemon._h_capture({"target": {}})["error_code"] == "TARGET_NOT_FOUND"


def test_capture_handler_resolves_target_before_delegating(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(
        daemon.capture_service,
        "capture_target",
        lambda **kwargs: calls.append(kwargs) or {"success": True, "frame_id": "frame_1"},
    )

    result = daemon._h_capture({"target": {"pid": 2}, "backend": "fake"})

    assert result == {"success": True, "frame_id": "frame_1"}
    assert calls[0]["target"] == {"pid": 2}


def test_focus_target_handler_resolves_target(monkeypatch) -> None:
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(
        daemon.win32,
        "focus_window_detailed",
        lambda pid: {"success": True, "hwnd": 1},
    )

    result = daemon._h_focus_target({"target": {"pid": 2}})

    assert result["success"] is True
    assert result["pid"] == 2


def test_capture_handler_delegates_to_capture_service(monkeypatch) -> None:
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(
        daemon.capture_service,
        "capture_target",
        lambda **kwargs: {"success": True, "frame_id": "frame_1"},
    )

    result = daemon._h_capture({"target": {"pid": 2}, "backend": "fake"})

    assert result == {"success": True, "frame_id": "frame_1"}


def test_mouse_click_uses_frame_geometry_when_frame_id_present(monkeypatch) -> None:
    calls: list[tuple[int, int, str, int]] = []

    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(daemon.win32, "focus_window", lambda pid: True)
    monkeypatch.setattr(daemon.win32, "send_mouse_click", lambda sx, sy, button, clicks: calls.append((sx, sy, button, clicks)) or True)
    monkeypatch.setattr(daemon, "FRAME_CACHE", type("Cache", (), {"get": lambda self, frame_id: _frame_cache_record()})())

    result = daemon._h_mouse_click(
        {
            "target": {"pid": 2},
            "x": 80,
            "y": 50,
            "scope": "capture",
            "frame_id": "frame_1",
        },
    )

    assert result["success"] is True
    assert result["screen_coords"] == [260, 220]
    assert result["client_size"] == [320, 200]
    assert result["client_origin"] == [100, 120]
    assert calls == [(260, 220, "left", 1)]


def test_mouse_click_with_missing_frame_id_returns_error(monkeypatch) -> None:
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(daemon, "FRAME_CACHE", type("Cache", (), {"get": lambda self, frame_id: None})())

    result = daemon._h_mouse_click(
        {
            "target": {"pid": 2},
            "x": 10,
            "y": 10,
            "scope": "capture",
            "frame_id": "frame_1",
        }
    )

    assert result["success"] is False
    assert result["error_code"] == "FRAME_NOT_FOUND"


def test_capture_scope_without_frame_id_returns_error(monkeypatch) -> None:
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())

    result = daemon._h_mouse_click(
        {
            "target": {"pid": 2},
            "x": 10,
            "y": 10,
            "scope": "capture",
        }
    )

    assert result["success"] is False
    assert result["error_code"] == "FRAME_NOT_FOUND"


def test_missing_target_param_returns_structured_error(monkeypatch) -> None:
    def raise_value_error(target):
        raise ValueError("target must include pid or hwnd")

    monkeypatch.setattr(daemon.targets, "resolve_target", raise_value_error)

    result = daemon._h_mouse_click({"x": 10, "y": 10})

    assert result["success"] is False
    assert result["error_code"] == "TARGET_NOT_FOUND"


def test_mouse_drag_and_scroll_are_frame_aware(monkeypatch) -> None:
    recorded_drag = {}
    recorded_scroll = {}

    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(daemon.win32, "focus_window", lambda pid: True)
    monkeypatch.setattr(
        daemon.win32,
        "send_mouse_drag",
        lambda from_screen, to_screen, button="left", steps=10: (
            recorded_drag.setdefault("from", from_screen),
            recorded_drag.setdefault("to", to_screen),
            recorded_drag.setdefault("button", button),
            recorded_drag.setdefault("steps", steps),
            True
        )[4],
    )
    monkeypatch.setattr(
        daemon.win32,
        "send_scroll",
        lambda sx, sy, delta: (
            recorded_scroll.setdefault("coords", (sx, sy)),
            recorded_scroll.setdefault("delta", delta),
            True
        )[2],
    )
    monkeypatch.setattr(daemon, "FRAME_CACHE", type("Cache", (), {"get": lambda self, frame_id: _frame_cache_record()})())

    drag = daemon._h_mouse_drag(
        {
            "target": {"pid": 2},
            "from_x": 40,
            "from_y": 20,
            "to_x": 120,
            "to_y": 60,
            "scope": "capture",
            "frame_id": "frame_1",
        },
    )
    scroll = daemon._h_scroll(
        {
            "target": {"pid": 2},
            "x": 80,
            "y": 50,
            "scope": "capture",
            "frame_id": "frame_1",
            "delta": 120,
        }
    )

    assert drag["success"] is True
    assert drag["from_screen"] == [180, 160]
    assert drag["to_screen"] == [340, 240]
    assert scroll["success"] is True
    assert scroll["screen_coords"] == [260, 220]


def test_mouse_drag_uses_legacy_pid_and_failed_send_is_reflected(monkeypatch) -> None:
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(daemon.win32, "focus_window", lambda pid: True)
    monkeypatch.setattr(daemon.win32, "send_mouse_drag", lambda *args, **kwargs: False)

    result = daemon._h_mouse_drag(
        {
            "pid": 2,
            "from_x": 1,
            "from_y": 2,
            "to_x": 3,
            "to_y": 4,
            "scope": "client",
        },
    )

    assert result["success"] is False


def test_scroll_uses_legacy_pid_and_target_spec(monkeypatch) -> None:
    sends: list[tuple[int, int, int]] = []
    monkeypatch.setattr(daemon.targets, "resolve_target", lambda target: _target())
    monkeypatch.setattr(daemon.win32, "focus_window", lambda pid: True)
    monkeypatch.setattr(
        daemon.win32,
        "send_scroll",
        lambda sx, sy, delta: (sends.append((sx, sy, delta)), True)[1],
    )

    assert daemon._h_scroll({"pid": 2, "x": 2, "y": 3, "scope": "client"})["success"] is True
    assert sends == [(102, 123, 120)]
    assert daemon._h_scroll({"target": {"pid": 2}, "x": 2, "y": 3, "scope": "client"})["success"] is True


def test_send_keys_still_pid_compatible(monkeypatch) -> None:
    monkeypatch.setattr(daemon.win32, "focus_window", lambda pid: True)
    monkeypatch.setattr(daemon.win32, "send_keys", lambda keys: True)

    result = daemon._h_send_keys({"pid": 2, "keys": "abc"})

    assert result == {"success": True, "keys": "abc"}


def test_get_window_info_and_focus_window_compatibility(monkeypatch) -> None:
    # Smoke coverage to ensure legacy handlers still render deterministic shapes.
    monkeypatch.setattr(
        daemon.win32,
        "focus_window_detailed",
        lambda pid: {"success": True, "hwnd": pid},
    )
    monkeypatch.setattr(
        daemon.win32,
        "get_window_info",
        lambda pid: type(
            "Info",
            (),
            {
                "hwnd": 1,
                "pid": pid,
                "title": "x",
                "window_rect": (1, 2, 3, 4),
                "client_size": (10, 20),
                "client_screen_origin": (5, 6),
                "dpi": 96,
                "is_foreground": True,
            },
        )(),
    )

    info = daemon._h_get_window_info({"pid": 999})
    assert info["found"] is True
    assert info["pid"] == 999

    focus = daemon._h_focus_window({"pid": 1})
    assert focus["success"] is True

    monkeypatch.setattr(daemon.win32, "get_window_info", lambda pid: None)
    assert daemon._h_get_window_info({"pid": 999})["found"] is False
