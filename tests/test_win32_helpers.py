"""Smoke tests for the Win32 helpers added for guards and target metadata.
They call the real APIs against this test process, so they only assert shape."""
from __future__ import annotations

import os
import sys

import pytest

from game_input_mcp import win32


def test_exe_of_pid_names_this_interpreter() -> None:
    exe = win32.exe_of_pid(os.getpid())

    assert exe is not None
    assert exe.lower() == os.path.basename(sys.executable).lower()


def test_exe_of_pid_returns_none_for_a_missing_process() -> None:
    assert win32.exe_of_pid(0x7FFFFFF0) is None


def test_root_window_helpers_tolerate_nothing_there() -> None:
    assert win32.root_window(0) == 0
    assert isinstance(win32.root_window_at(-32000, -32000), int)


def test_describe_window_for_no_window() -> None:
    assert win32.describe_window(0) == {"hwnd": 0}


def test_every_listed_window_carries_exe_and_monitor_fields() -> None:
    infos = win32.list_window_infos()
    if not infos and (win32.current_desktop_name() or "Default").lower() != "default":
        pytest.skip(f"non-interactive desktop {win32.current_desktop_name()!r} has no visible windows")

    assert infos, "expected at least one visible window on an interactive desktop"
    for info in infos:
        assert info.exe is None or isinstance(info.exe, str)
        assert info.monitor is None or isinstance(info.monitor, int)


def test_process_cpu_ms_reports_this_process_and_none_for_a_missing_one() -> None:
    first = win32.process_cpu_ms(os.getpid())
    sum(i * i for i in range(200_000))  # burn a little CPU
    second = win32.process_cpu_ms(os.getpid())

    assert first is not None and second is not None and second >= first > 0
    assert win32.process_cpu_ms(0x7FFFFFF0) is None


def test_current_desktop_name_returns_non_empty_string() -> None:
    desk = win32.current_desktop_name()

    assert isinstance(desk, str) and len(desk) > 0


def test_launch_process_sets_interactive_desktop_and_closes_handles(monkeypatch) -> None:
    captured: dict = {}
    closed: list[int] = []

    def fake_create_process(exe, cmd_buf, pa, ta, inherit, flags, env, cwd, p_si, p_pi):
        si = getattr(p_si, "contents", None) or p_si._obj
        pi = getattr(p_pi, "contents", None) or p_pi._obj
        captured["exe"] = exe
        captured["cmd"] = cmd_buf.value
        captured["inherit"] = bool(inherit)
        captured["flags"] = int(flags)
        captured["cwd"] = cwd
        captured["desktop"] = si.lpDesktop
        pi.hProcess = 101
        pi.hThread = 102
        pi.dwProcessId = 4242
        pi.dwThreadId = 4243
        return 1

    monkeypatch.setattr(win32.kernel32, "CreateProcessW", fake_create_process)
    monkeypatch.setattr(win32.kernel32, "CloseHandle", lambda h: closed.append(int(h)) or 1)

    pid = win32.launch_process(r"C:\Games\Game.exe", ["-dx12", "-windowed"], cwd=r"C:\Games")

    assert pid == 4242
    assert captured["exe"] == r"C:\Games\Game.exe"
    assert "-dx12" in captured["cmd"] and "-windowed" in captured["cmd"]
    assert captured["inherit"] is False
    assert captured["cwd"] == r"C:\Games"
    assert captured["desktop"] == r"WinSta0\Default"
    assert sorted(closed) == [101, 102]

