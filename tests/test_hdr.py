from __future__ import annotations

import pytest

from game_input_mcp.capture import hdr
from game_input_mcp.models import Rect

GAME = r"D:\Games\Foo\Binaries\Win64\Foo.exe"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("AppStatus=1;AutoHDREnable=2097;", True),
        ("AppStatus=1;AutoHDREnable=2096;", False),
        ("AutoHDREnable=1", True),
        ("AppStatus=4096;", None),
        ("AutoHDREnable=abc;", None),
        ("", None),
    ],
)
def test_auto_hdr_flag_parity_rule(data, expected) -> None:
    assert hdr._flag(data) is expected


def test_exe_setting_wins_over_the_global_one() -> None:
    prefs = {
        GAME: "AppStatus=1;AutoHDREnable=2096;",
        hdr.GLOBAL_PREFS_NAME: "AutoHDREnable=2097;",
    }

    assert hdr.auto_hdr_for_exe(prefs, "foo.exe") == (False, "exe")
    assert hdr.auto_hdr_for_exe(prefs, "Foo") == (False, "exe")          # .exe is optional
    assert hdr.auto_hdr_for_exe(prefs, "other.exe") == (True, "global")  # falls back


def test_exe_entry_without_the_flag_falls_through_to_global() -> None:
    prefs = {GAME: "AppStatus=4096;", hdr.GLOBAL_PREFS_NAME: "AutoHDREnable=2097;"}

    assert hdr.auto_hdr_for_exe(prefs, "Foo.exe") == (True, "global")


def test_paths_with_forward_slashes_and_no_settings() -> None:
    assert hdr.auto_hdr_for_exe({"D:/Games/Foo.exe": "AutoHDREnable=2097;"}, "foo.exe") == (True, "exe")
    assert hdr.auto_hdr_for_exe({}, "foo.exe") == (None, None)
    assert hdr.auto_hdr_for_exe({}, None) == (None, None)


def test_registry_is_opened_read_only_on_the_one_fixed_key(monkeypatch) -> None:
    import winreg

    seen = {}

    class FakeKey:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(root, path, reserved=0, access=winreg.KEY_READ):
        seen.update(root=root, path=path, access=access)
        return FakeKey()

    values = [("a.exe", "AutoHDREnable=2097;", 1)]

    def enum_value(key, index):
        if index >= len(values):
            raise OSError("no more data")  # what the real EnumValue does at the end
        return values[index]

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "EnumValue", enum_value)

    assert hdr.read_gpu_prefs() == {"a.exe": "AutoHDREnable=2097;"}
    assert seen == {
        "root": winreg.HKEY_CURRENT_USER,
        "path": r"Software\Microsoft\DirectX\UserGpuPreferences",
        "access": winreg.KEY_READ,
    }


def test_missing_registry_key_reads_as_empty(monkeypatch) -> None:
    import winreg

    def boom(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(winreg, "OpenKey", boom)

    assert hdr.read_gpu_prefs() == {}


MONITORS = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 3840, 1080)]
PATHS = [
    {"x": 0, "y": 0, "supported": True, "enabled": False},
    {"x": 1920, "y": 0, "supported": True, "enabled": True},
]


def test_display_state_is_matched_by_monitor_position() -> None:
    assert hdr.display_hdr_for(Rect(100, 100, 500, 400), MONITORS, PATHS) is False
    assert hdr.display_hdr_for(Rect(2000, 100, 2400, 400), MONITORS, PATHS) is True


def test_window_straddling_monitors_uses_the_one_under_its_centre() -> None:
    assert hdr.display_hdr_for(Rect(1800, 0, 2300, 400), MONITORS, PATHS) is True   # centre x=2050
    assert hdr.display_hdr_for(Rect(1500, 0, 2000, 400), MONITORS, PATHS) is False  # centre x=1750


def test_unknown_when_no_path_matches_or_off_every_monitor() -> None:
    assert hdr.display_hdr_for(Rect(100, 100, 200, 200), MONITORS, []) is None
    assert hdr.display_hdr_for(Rect(5000, 5000, 5100, 5100), MONITORS, PATHS) is None


def _info(display, auto, scope="exe"):
    return {"display_hdr": display, "auto_hdr": auto, "auto_hdr_scope": scope}


def test_warning_only_for_hdr_displays_or_unknown_plus_auto_hdr() -> None:
    sdr = hdr.warning_for(_info(False, True), "Foo.exe")
    hdr_on = hdr.warning_for(_info(True, True), "Foo.exe")
    hdr_native = hdr.warning_for(_info(True, None), "Foo.exe")
    unknown_auto = hdr.warning_for(_info(None, True), "Foo.exe")
    unknown_nothing = hdr.warning_for(_info(None, None, None), "Foo.exe")

    assert sdr is None and unknown_nothing is None
    assert hdr_on["code"] == hdr_native["code"] == unknown_auto["code"] == "HDR_COLOR_SHIFT_POSSIBLE"
    assert "Auto HDR is on" in hdr_on["message"] and "Auto HDR" not in hdr_native["message"].split(":")[0]
    assert "unknown" in unknown_auto["message"]
    assert hdr_on["details"] == {"display_hdr": True, "auto_hdr": True, "auto_hdr_scope": "exe", "exe": "Foo.exe"}


def test_probe_combines_both_inputs_and_caches_briefly() -> None:
    calls = {"paths": 0, "prefs": 0}
    now = [0.0]

    def query():
        calls["paths"] += 1
        return PATHS

    def prefs():
        calls["prefs"] += 1
        return {GAME: "AutoHDREnable=2097;"}

    probe = hdr.HdrProbe(read_prefs=prefs, query_paths=query, monitors=lambda: MONITORS, clock=lambda: now[0])

    first = probe.inspect(Rect(2000, 100, 2400, 400), "Foo.exe")
    probe.inspect(Rect(100, 100, 500, 400), "Foo.exe")
    assert first == {"display_hdr": True, "auto_hdr": True, "auto_hdr_scope": "exe"}
    assert calls == {"paths": 1, "prefs": 1}

    now[0] += hdr.CACHE_TTL_S + 0.1
    probe.inspect(Rect(100, 100, 500, 400), "Foo.exe")
    assert calls == {"paths": 2, "prefs": 2}


def test_display_query_never_raises_on_this_machine() -> None:
    paths = hdr.query_display_paths()

    assert isinstance(paths, list)
    for path in paths:
        assert set(path) == {"x", "y", "supported", "enabled"}
