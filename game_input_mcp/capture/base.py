from __future__ import annotations

import logging
from dataclasses import dataclass

from PIL import Image

from game_input_mcp.models import Rect

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptureResult:
    image: Image.Image
    backend: str
    mode: str


class CaptureBackend:
    name: str
    priority: int
    registry: dict[str, type["CaptureBackend"]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "name" in cls.__dict__ and "priority" in cls.__dict__:
            existing = CaptureBackend.registry.get(cls.name)
            if existing is not None and existing is not cls:
                raise ValueError(f"Duplicate capture backend name: {cls.name!r}")
            CaptureBackend.registry[cls.name] = cls

    def is_available(self, rect: Rect | None) -> bool:
        return True

    def capture(self, rect: Rect | None) -> Image.Image:
        raise NotImplementedError

    def mode(self, rect: Rect | None) -> str:
        return "region" if rect is not None else "virtual_screen"


_backend_instances: dict[str, CaptureBackend] = {}


def _get_backend(name: str) -> CaptureBackend:
    if name not in _backend_instances:
        cls = CaptureBackend.registry.get(name)
        if cls is None:
            raise ValueError(f"Unknown capture backend: {name}")
        _backend_instances[name] = cls()
    return _backend_instances[name]


def _candidate_classes(selected: str) -> list[type[CaptureBackend]]:
    if selected == "auto":
        return sorted(CaptureBackend.registry.values(), key=lambda cls: cls.priority)
    cls = CaptureBackend.registry.get(selected)
    if cls is None:
        raise ValueError(f"Unknown capture backend: {selected}")
    return [cls]


def capture_region(rect: Rect | None, *, backend: str = "auto") -> CaptureResult:
    selected = backend.strip().lower()
    last_error: Exception | None = None
    for cls in _candidate_classes(selected):
        inst = _get_backend(cls.name)
        if not inst.is_available(rect):
            continue
        try:
            image = inst.capture(rect)
            return CaptureResult(image=image, backend=inst.name, mode=inst.mode(rect))
        except Exception as exc:
            last_error = exc
            log.warning("capture backend %s failed", inst.name, exc_info=selected != "auto")
    if last_error is not None:
        raise RuntimeError(f"all capture backends failed; last error: {last_error}") from last_error
    raise RuntimeError("no capture backend is available")
