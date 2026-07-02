from __future__ import annotations

from windows_input_mcp import server


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
