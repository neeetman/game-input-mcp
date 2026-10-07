"""Targets by exe / title, and TARGET_AMBIGUOUS (Phase 5 of the 2026-10-07 spec)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from game_input_mcp import daemon, targets
from game_input_mcp.capture import service
from game_input_mcp.models import TargetSpec


@dataclass
class W:
    hwnd: int
    pid: int
    title: str
    exe: str | None
    client_size: tuple[int, int] = (100, 100)
    window_rect: tuple[int, int, int, int] = (0, 0, 100, 100)
    client_screen_origin: tuple[int, int] = (0, 0)
    dpi: int = 96
    is_foreground: bool = False
    is_minimized: bool = False
    monitor: int | None = 0


WINDOWS = [
    W(1, 10, "Foo Game", "Foo.exe", (1920, 1080)),
    W(2, 10, "Foo Launcher", "Foo.exe", (400, 300)),          # same process, smaller
    W(3, 20, "Notepad", "notepad.exe"),
    W(4, 30, "Foo Editor", "FooEditor.exe"),
    W(5, 40, "Bar", "Bar.exe"),
    W(6, 41, "Bar (2)", "BAR.EXE"),
    W(7, 50, "no exe known", None),
]


@pytest.fixture(autouse=True)
def desktop(monkeypatch):
    monkeypatch.setattr(targets.win32, "list_window_infos", lambda: WINDOWS)
    monkeypatch.setattr(targets.win32, "get_window_info_by_hwnd", lambda hwnd: next((w for w in WINDOWS if w.hwnd == hwnd), None))
    monkeypatch.setattr(targets.win32, "get_window_info", lambda pid: next((w for w in WINDOWS if w.pid == pid), None))


def test_spec_accepts_exe_and_title_and_rejects_nothing_at_all() -> None:
    assert TargetSpec.from_value({"exe": "Foo.exe"}).exe == "Foo.exe"
    assert TargetSpec.from_value({"title": " Foo "}).title == "Foo"
    assert TargetSpec.from_value({"exe": "  "}.copy() | {"pid": 1}).exe is None
    assert TargetSpec.from_value({"pid": 3, "exe": "x"}).to_dict() == {"pid": 3, "exe": "x"}
    with pytest.raises(ValueError):
        TargetSpec.from_value({"exe": "", "title": ""})


def test_exe_match_is_case_insensitive_and_extension_optional() -> None:
    assert targets.resolve_target({"exe": "notepad"}).hwnd == 3
    assert targets.resolve_target({"exe": "NOTEPAD.EXE"}).hwnd == 3


def test_exe_must_match_the_whole_name_not_a_prefix() -> None:
    assert targets.resolve_target({"exe": "FooEditor"}).hwnd == 4  # not swallowed by "Foo"


def test_several_windows_of_one_process_resolve_to_the_largest() -> None:
    assert targets.resolve_target({"exe": "Foo.exe"}).hwnd == 1


def test_title_is_a_case_insensitive_substring() -> None:
    assert targets.resolve_target({"title": "launcher"}).hwnd == 2
    assert targets.resolve_target({"title": "NOTE"}).hwnd == 3


def test_exe_and_title_must_both_match() -> None:
    assert targets.resolve_target({"exe": "Foo", "title": "launcher"}).hwnd == 2
    assert targets.resolve_target({"exe": "Notepad", "title": "launcher"}) is None


def test_no_match_is_none() -> None:
    assert targets.resolve_target({"exe": "ghost.exe"}) is None


def test_windows_of_several_processes_are_ambiguous_and_listed() -> None:
    with pytest.raises(targets.TargetAmbiguous) as caught:
        targets.resolve_target({"exe": "bar"})

    assert caught.value.candidates == [
        {"pid": 40, "hwnd": 5, "exe": "Bar.exe", "title": "Bar"},
        {"pid": 41, "hwnd": 6, "exe": "BAR.EXE", "title": "Bar (2)"},
    ]


def test_a_title_can_be_ambiguous_across_processes_too() -> None:
    with pytest.raises(targets.TargetAmbiguous) as caught:
        targets.resolve_target({"title": "foo"})

    assert [c["pid"] for c in caught.value.candidates] == [10, 30]


def test_candidates_are_capped() -> None:
    many = [W(100 + i, 100 + i, "Dup", "dup.exe") for i in range(25)]
    targets.win32.list_window_infos = lambda: many  # restored by monkeypatch teardown of the fixture

    with pytest.raises(targets.TargetAmbiguous) as caught:
        targets.resolve_target({"exe": "dup"})

    assert len(caught.value.candidates) == targets.MAX_CANDIDATES


def test_pid_and_hwnd_still_win_over_exe_and_title() -> None:
    assert targets.resolve_target({"hwnd": 3, "exe": "Bar"}).hwnd == 3
    assert targets.resolve_target({"pid": 20, "title": "Foo"}).hwnd == 3


def test_exe_target_is_resolved_with_its_metadata() -> None:
    resolved = targets.resolve_target({"exe": "notepad"})

    assert (resolved.pid, resolved.exe, resolved.monitor) == (20, "notepad.exe", 0)


def test_handlers_return_target_ambiguous_with_candidates() -> None:
    result = daemon._h_get_target_info({"target": {"exe": "bar"}})

    assert result["success"] is False and result["error_code"] == "TARGET_AMBIGUOUS"
    assert result["retryable"] is False
    assert [c["pid"] for c in result["details"]["candidates"]] == [40, 41]


def test_handlers_resolve_an_exe_target() -> None:
    assert daemon._h_get_target_info({"target": {"exe": "notepad"}})["target"]["hwnd"] == 3


def test_unknown_exe_is_target_not_found() -> None:
    assert daemon._h_get_target_info({"target": {"exe": "ghost"}})["error_code"] == "TARGET_NOT_FOUND"


def test_capture_service_reports_ambiguity_too() -> None:
    result = service.capture_target({"exe": "bar"})

    assert result["error_code"] == "TARGET_AMBIGUOUS" and len(result["details"]["candidates"]) == 2


def test_list_targets_rows_carry_exe_for_picking_one() -> None:
    assert {t.exe for t in targets.list_targets()} >= {"Foo.exe", "notepad.exe"}
