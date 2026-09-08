from __future__ import annotations

from game_input_mcp import server


def test_list_targets_calls_daemon(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server, "_call", lambda method, **params: calls.append((method, params)) or {"success": True})

    assert server.list_targets() == {"success": True}
    assert calls == [("list_targets", {})]


def test_capture_passes_target_and_options(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server, "_call", lambda method, **params: calls.append((method, params)) or {"success": True})

    result = server.capture({"pid": 2}, backend="pillow", max_width=800)

    assert result == {"success": True}
    assert calls == [(
        "capture",
        {
            "target": {"pid": 2},
            "region": None,
            "scope": "client",
            "backend": "pillow",
            "max_width": 800,
        },
    )]


def test_focus_target_passes_target(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server, "_call", lambda method, **params: calls.append((method, params)) or {"success": True})

    assert server.focus_target({"pid": 2}) == {"success": True}
    assert calls == [("focus_target", {"target": {"pid": 2}})]


def test_compat_mouse_click_still_uses_pid(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server, "_call", lambda method, **params: calls.append((method, params)) or {"success": True})

    result = server.mouse_click(pid=2, x=1, y=2, activate=False)

    assert result == {"success": True}
    assert calls == [(
        "mouse_click",
        {
            "pid": 2,
            "target": {"pid": 2},
            "x": 1,
            "y": 2,
            "button": "left",
            "scope": "framebuffer",
            "clicks": 1,
            "frame_id": None,
            "framebuffer_width": None,
            "framebuffer_height": None,
            "activate": False,
        },
    )]


def test_keyboard_tools_call_new_daemon_methods(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server, "_call", lambda method, **params: calls.append((method, params)) or {"success": True})

    assert server.key_down({"pid": 2}, key="w")["success"] is True
    assert server.key_up({"pid": 2}, key="w", mode="vk")["success"] is True
    assert server.tap_key({"pid": 2}, key="space", hold_ms=5)["success"] is True
    assert server.hotkey({"pid": 2}, keys=["ctrl", "s"])["success"] is True
    assert server.type_text({"pid": 2}, text="hello")["success"] is True

    assert calls == [
        ("key_down", {"target": {"pid": 2}, "key": "w", "mode": "scancode", "activate": True}),
        ("key_up", {"target": {"pid": 2}, "key": "w", "mode": "vk", "activate": True}),
        ("tap_key", {"target": {"pid": 2}, "key": "space", "mode": "scancode", "hold_ms": 5, "activate": True}),
        ("hotkey", {"target": {"pid": 2}, "keys": ["ctrl", "s"], "mode": "vk", "activate": True}),
        ("type_text", {"target": {"pid": 2}, "text": "hello", "activate": True}),
    ]


def test_session_tools_call_daemon_methods(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server, "_call", lambda method, **params: calls.append((method, params)) or {"success": True})

    assert server.input_session_open({"pid": 2}, lease_ms=3000)["success"] is True
    assert server.set_keys("sid", down=["w", "a"], up=["s"])["success"] is True
    assert server.input_session_heartbeat("sid")["success"] is True
    assert server.input_session_state("sid")["success"] is True
    assert server.input_session_close("sid")["success"] is True

    assert calls == [
        (
            "session_open",
            {
                "target": {"pid": 2},
                "lease_ms": 3000,
                "focus": "acquire_once",
                "max_hold_ms": 30000,
                "takeover": False,
            },
        ),
        (
            "set_keys",
            {
                "session_id": "sid",
                "down": ["w", "a"],
                "up": ["s"],
                "buttons_down": None,
                "buttons_up": None,
                "mode": "scancode",
            },
        ),
        ("session_heartbeat", {"session_id": "sid", "lease_ms": None}),
        ("session_state", {"session_id": "sid"}),
        ("session_close", {"session_id": "sid"}),
    ]


def test_timeline_tools_call_daemon_methods(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server, "_call", lambda method, **params: calls.append((method, params)) or {"success": True})
    events = [{"t_ms": 0, "op": "down", "key": "w"}, {"t_ms": 100, "op": "up", "key": "w"}]

    assert server.run_timeline("sid", events, total_ms=200)["success"] is True
    assert server.abort_timeline("sid")["success"] is True

    assert calls == [
        ("run_timeline", {"session_id": "sid", "events": events, "total_ms": 200, "allow_dangling": False}),
        ("abort_timeline", {"session_id": "sid"}),
    ]


def test_mouse_move_relative_tool_calls_daemon(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(server, "_call", lambda method, **params: calls.append((method, params)) or {"success": True})

    assert server.mouse_move_relative("sid", dx=600, dy=-40, duration_ms=800)["success"] is True

    assert calls == [(
        "mouse_move_relative",
        {"session_id": "sid", "dx": 600, "dy": -40, "duration_ms": 800, "rate_hz": 250},
    )]
