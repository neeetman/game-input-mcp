from __future__ import annotations

import pytest

from game_input_mcp.input import keys
from game_input_mcp.input.keys import resolve_button, resolve_key


def test_resolve_wasd_as_scancode() -> None:
    key = resolve_key("w", "scancode")

    assert key.scan_code == 0x11
    assert key.vk is None
    assert key.extended is False


def test_resolve_arrow_as_extended_scancode() -> None:
    key = resolve_key("left", "scancode")

    assert key.scan_code == 0x4B
    assert key.extended is True


def test_resolve_enter_as_virtual_key() -> None:
    key = resolve_key("enter", "vk")

    assert key.vk == 0x0D
    assert key.scan_code is None


def test_unknown_key_raises_value_error(monkeypatch) -> None:
    monkeypatch.setattr(keys, "_map_vk_to_scancode", lambda vk: None)
    monkeypatch.setattr(keys, "_char_to_vk", lambda ch: None)
    with pytest.raises(ValueError, match="unsupported key"):
        resolve_key("not-a-key", "scancode")


@pytest.mark.parametrize(
    ("name", "scan_code", "extended"),
    [
        ("1", 0x02, False),
        ("0", 0x0B, False),
        ("e", 0x12, False),
        ("q", 0x10, False),
        ("f", 0x21, False),
        ("c", 0x2E, False),
        ("z", 0x2C, False),
        ("m", 0x32, False),
        ("-", 0x0C, False),
        ("[", 0x1A, False),
        ("`", 0x29, False),
        ("backspace", 0x0E, False),
        ("capslock", 0x3A, False),
        ("lshift", 0x2A, False),
        ("rshift", 0x36, False),
        ("lctrl", 0x1D, False),
        ("rctrl", 0x1D, True),
        ("lalt", 0x38, False),
        ("ralt", 0x38, True),
        ("numpad0", 0x52, False),
        ("numpad_enter", 0x1C, True),
        ("home", 0x47, True),
        ("end", 0x4F, True),
        ("pageup", 0x49, True),
        ("pagedown", 0x51, True),
        ("insert", 0x52, True),
        ("delete", 0x53, True),
        ("lwin", 0x5B, True),
        ("f11", 0x57, False),
        ("f12", 0x58, False),
    ],
)
def test_full_scan_code_table(name: str, scan_code: int, extended: bool) -> None:
    key = resolve_key(name, "scancode")

    assert (key.scan_code, key.extended) == (scan_code, extended)


def test_modifier_aliases_map_to_left_variants() -> None:
    assert resolve_key("ctrl", "scancode") == resolve_key("lctrl", "scancode")
    assert resolve_key("shift", "scancode") == resolve_key("lshift", "scancode")
    assert resolve_key("alt", "scancode") == resolve_key("lalt", "scancode")
    assert resolve_key("escape", "scancode") == resolve_key("esc", "scancode")
    assert resolve_key("return", "scancode") == resolve_key("enter", "scancode")


def test_scan_code_falls_back_to_map_virtual_key(monkeypatch) -> None:
    calls = []

    def fake_map(vk: int):
        calls.append(vk)
        return (0x45, True)

    monkeypatch.setattr(keys, "_map_vk_to_scancode", fake_map)

    key = resolve_key("pause", "scancode")

    assert calls == [0x13]
    assert key.scan_code == 0x45
    assert key.extended is True


def test_scan_code_fallback_uses_layout_for_unknown_characters(monkeypatch) -> None:
    monkeypatch.setattr(keys, "_char_to_vk", lambda ch: 0xDE if ch == "é" else None)
    monkeypatch.setattr(keys, "_map_vk_to_scancode", lambda vk: (0x28, False) if vk == 0xDE else None)

    key = resolve_key("é", "scancode")

    assert key.scan_code == 0x28


def test_vk_mode_knows_digits_letters_and_side_modifiers() -> None:
    assert resolve_key("1", "vk").vk == 0x31
    assert resolve_key("e", "vk").vk == 0x45
    assert resolve_key("lshift", "vk").vk == 0xA0
    assert resolve_key("rctrl", "vk").vk == 0xA3
    assert resolve_key("numpad5", "vk").vk == 0x65
    assert resolve_key("delete", "vk").vk == 0x2E


def test_resolve_mouse_buttons() -> None:
    left = resolve_button("left")
    x2 = resolve_button("x2")

    assert left.down_flag == 0x0002 and left.up_flag == 0x0004 and left.mouse_data == 0
    assert x2.down_flag == 0x0080 and x2.up_flag == 0x0100 and x2.mouse_data == 2
    with pytest.raises(ValueError, match="unsupported mouse button"):
        resolve_button("x9")
