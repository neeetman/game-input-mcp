from __future__ import annotations

from pathlib import Path


README = Path("README.md").read_text(encoding="utf-8")


def test_readme_describes_general_game_io_positioning() -> None:
    assert "general Windows game screenshot and input MCP" in README
    assert "Unity" not in README.split("## Tools", 1)[0]


def test_readme_documents_capture_and_frame_id_tools() -> None:
    assert "capture(target" in README
    assert "frame_id" in README
    assert 'scope="capture"' in README


def test_readme_documents_scan_code_keys() -> None:
    assert "key_down" in README
    assert "key_up" in README
    assert "scancode" in README


def test_readme_documents_input_sessions() -> None:
    assert "input_session_open" in README
    assert "set_keys" in README
    assert "lease" in README


def test_readme_documents_timelines() -> None:
    assert "run_timeline" in README
    assert "abort_timeline" in README
    assert "qpc_ns" in README
