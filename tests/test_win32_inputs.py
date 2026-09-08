from __future__ import annotations

from game_input_mcp import win32
from game_input_mcp.input.keys import resolve_button, resolve_key
from game_input_mcp.input.state import Edge


def test_build_inputs_covers_every_edge_kind() -> None:
    edges = [
        Edge("key", "w", True, stroke=resolve_key("w")),
        Edge("key", "left", False, stroke=resolve_key("left")),
        Edge("button", "x2", True, button=resolve_button("x2")),
        Edge("move", "look", True, dx=-12, dy=5),
        Edge("wheel", "wheel", True, delta=-120),
    ]

    inputs = win32.build_inputs(edges)

    assert len(inputs) == 5
    assert inputs[0].type == win32.INPUT_KEYBOARD
    assert inputs[0].ki.wScan == 0x11 and inputs[0].ki.dwFlags == win32.KEYEVENTF_SCANCODE
    assert inputs[1].ki.wScan == 0x4B
    assert inputs[1].ki.dwFlags == win32.KEYEVENTF_SCANCODE | win32.KEYEVENTF_EXTENDEDKEY | win32.KEYEVENTF_KEYUP
    assert inputs[2].type == win32.INPUT_MOUSE
    assert inputs[2].mi.dwFlags == 0x0080 and inputs[2].mi.mouseData == 2
    assert inputs[3].mi.dwFlags == win32.MOUSEEVENTF_MOVE
    assert (inputs[3].mi.dx, inputs[3].mi.dy) == (-12, 5)
    assert inputs[4].mi.dwFlags == win32.MOUSEEVENTF_WHEEL
    # mouseData is a DWORD; negative wheel deltas are stored two's-complement.
    assert inputs[4].mi.mouseData == (-120) & 0xFFFFFFFF


def test_send_edges_with_nothing_returns_zero(monkeypatch) -> None:
    monkeypatch.setattr(win32, "_send_inputs", lambda inputs: (_ for _ in ()).throw(AssertionError("must not call")))
    assert win32.send_edges([]) == 0
