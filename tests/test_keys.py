from __future__ import annotations

import pytest

from game_input_mcp.input.keys import resolve_key


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


def test_unknown_key_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unsupported key"):
        resolve_key("not-a-key", "scancode")
