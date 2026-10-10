from __future__ import annotations

import json

import pytest

from game_input_mcp import config


def test_defaults_match_the_design_spec() -> None:
    cfg = config.Config()

    assert cfg.foreground_guard == "strict"
    assert cfg.frame_geometry_check == "strict"
    assert cfg.presence == "warn"
    assert cfg.presence_idle_s == 30.0
    assert cfg.capture_timeout_ms == 1500
    assert cfg.allow_window_mutation is False


def test_missing_file_gives_defaults(tmp_path) -> None:
    assert config.load(tmp_path / "nope.json", env={}) == config.Config()


def test_file_values_are_validated_and_bad_ones_ignored(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "presence": "off",
                "presence_idle_s": 12,
                "foreground_guard": "banana",
                "frame_geometry_check": "warn",
                "no_such_key": 1,
                "capture_timeout_ms": -5,
            }
        )
    )

    cfg = config.load(path, env={})

    assert cfg.presence == "off"
    assert cfg.presence_idle_s == 12.0
    assert cfg.foreground_guard == "strict"  # invalid -> default
    assert cfg.frame_geometry_check == "warn"
    assert cfg.capture_timeout_ms == 1500


def test_environment_overrides_file(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"presence": "warn"}))

    cfg = config.load(path, env={"GAME_INPUT_PRESENCE": "off", "GAME_INPUT_ALLOW_WINDOW_MUTATION": "true"})

    assert cfg.presence == "off"
    assert cfg.allow_window_mutation is True


def test_corrupt_file_never_stops_startup(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json")

    assert config.load(path, env={}) == config.Config()


def test_write_value_validates_and_persists(tmp_path) -> None:
    path = tmp_path / "sub" / "config.json"

    cfg = config.write_value("presence", "WARN", path)
    config.write_value("presence_idle_s", "45", path)

    assert cfg.presence == "warn"
    assert json.loads(path.read_text())["presence"] == "warn"
    assert config.load(path, env={}).presence_idle_s == 45.0
    with pytest.raises(ValueError):
        config.write_value("presence", "loud", path)
    with pytest.raises(ValueError):
        config.write_value("nope", "1", path)


def test_auto_wgc_first_is_off_by_default_and_settable(tmp_path) -> None:
    assert config.Config().auto_wgc_first is False
    path = tmp_path / "c.json"

    config.write_value("auto_wgc_first", "true", path)

    assert config.load(path, env={}).auto_wgc_first is True
    assert config.load(path, env={"GAME_INPUT_AUTO_WGC_FIRST": "0"}).auto_wgc_first is False


@pytest.mark.parametrize("legacy", ["focus", "strict", "STRICT"])
def test_the_old_refusing_presence_policies_are_read_as_warn(tmp_path, legacy) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"presence": legacy}))

    assert config.load(path, env={}).presence == "warn"
    assert config.load(tmp_path / "none.json", env={"GAME_INPUT_PRESENCE": legacy}).presence == "warn"
    assert config.write_value("presence", legacy, tmp_path / "w.json").presence == "warn"
    assert json.loads((tmp_path / "w.json").read_text())["presence"] == "warn"


def test_presence_only_knows_off_and_warn() -> None:
    assert config.PRESENCE_MODES == ("off", "warn")
