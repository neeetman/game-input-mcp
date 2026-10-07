"""Smoke tests for the Win32 helpers added for guards and target metadata.
They call the real APIs against this test process, so they only assert shape."""
from __future__ import annotations

import os
import sys

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
