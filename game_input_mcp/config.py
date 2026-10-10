"""Daemon-level safety and capture settings.

Read once at daemon start from ``%LOCALAPPDATA%\\game-input-mcp\\config.json``;
``GAME_INPUT_<KEY>`` environment variables override the file. Relaxing a guard
is only possible here, never per MCP call (see the 2026-10-07 design spec).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger("game-input-daemon.config")

GUARD_MODES = ("strict", "warn", "off")
PRESENCE_MODES = ("off", "warn")
# Policies that used to refuse input while the user was active. They no longer
# exist; an old config.json keeps working and is read as "warn".
LEGACY_PRESENCE_MODES = ("focus", "strict")
CAPTURE_BACKENDS = ("auto", "dxcam", "mss", "pillow", "wgc")


@dataclass(frozen=True)
class Config:
    foreground_guard: str = "strict"
    frame_geometry_check: str = "strict"
    presence: str = "warn"
    presence_idle_s: float = 30.0
    capture_backend: str = "auto"
    capture_timeout_ms: int = 1500
    wgc_idle_ttl_s: float = 5.0
    # backend="auto" tries wgc before the screen backends (it still falls through
    # to them when wgc is not installed or fails). Off until it has soaked.
    auto_wgc_first: bool = False
    allow_window_mutation: bool = False


def default_config_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "game-input-mcp"
    return base / "config.json"


def _choice(allowed: tuple[str, ...]):
    def convert(value: Any) -> str:
        text = str(value).strip().lower()
        if text not in allowed:
            raise ValueError(f"must be one of {allowed}")
        return text

    return convert


def _presence(value: Any) -> str:
    text = str(value).strip().lower()
    if text in LEGACY_PRESENCE_MODES:
        log.warning("presence=%s no longer refuses input; treating it as 'warn'", text)
        return "warn"
    return _choice(PRESENCE_MODES)(text)


def _positive_float(value: Any) -> float:
    number = float(value)
    if number < 0:
        raise ValueError("must be >= 0")
    return number


def _positive_int(value: Any) -> int:
    number = int(value)
    if number <= 0:
        raise ValueError("must be > 0")
    return number


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError("must be a boolean")


_CONVERTERS = {
    "foreground_guard": _choice(GUARD_MODES),
    "frame_geometry_check": _choice(GUARD_MODES),
    "presence": _presence,
    "presence_idle_s": _positive_float,
    "capture_backend": _choice(CAPTURE_BACKENDS),
    "capture_timeout_ms": _positive_int,
    "wgc_idle_ttl_s": _positive_float,
    "auto_wgc_first": _boolean,
    "allow_window_mutation": _boolean,
}


def parse(values: Mapping[str, Any]) -> Config:
    """Build a Config from raw values. Unknown keys and invalid values are
    logged and ignored so a typo can never stop the daemon from starting."""
    clean: dict[str, Any] = {}
    for key, raw in values.items():
        convert = _CONVERTERS.get(key)
        if convert is None:
            log.warning("ignoring unknown config key %r", key)
            continue
        try:
            clean[key] = convert(raw)
        except (TypeError, ValueError) as exc:
            log.warning("ignoring config %s=%r: %s", key, raw, exc)
    return Config(**clean)


def load(path: str | Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """File first, then GAME_INPUT_* environment overrides."""
    source = Path(path) if path is not None else default_config_path()
    values: dict[str, Any] = {}
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            values.update(data)
        else:
            log.warning("config %s is not a JSON object; ignored", source)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read config %s: %s", source, exc)
    environment = os.environ if env is None else env
    for field_ in fields(Config):
        override = environment.get(f"GAME_INPUT_{field_.name.upper()}")
        if override is not None:
            values[field_.name] = override
    return parse(values)


def write_value(key: str, value: str, path: str | Path | None = None) -> Config:
    """Validate and persist one setting (used by ``install --set``)."""
    if key not in _CONVERTERS:
        raise ValueError(f"unknown config key {key!r}; known: {sorted(_CONVERTERS)}")
    converted = _CONVERTERS[key](value)
    target = Path(path) if path is not None else default_config_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data[key] = converted
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return parse(data)
