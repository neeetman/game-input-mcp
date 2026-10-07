from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from PIL import Image

from game_input_mcp.models import Rect, TargetInfo

log = logging.getLogger(__name__)

BACKEND_ALIASES = {"windows_graphics_capture": "wgc"}


class CaptureError(RuntimeError):
    """A capture failure with a structured error code (the service turns it
    into an error_response instead of the generic CAPTURE_FAILED)."""

    def __init__(self, code: str, message: str, *, retryable: bool = True, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details


@dataclass(frozen=True)
class CaptureResult:
    image: Image.Image
    backend: str
    mode: str
    warnings: tuple[dict, ...] = ()
    extra: dict = field(default_factory=dict)


class CaptureBackend:
    name: str
    priority: int
    registry: dict[str, type["CaptureBackend"]] = {}
    # Window-targeted backends need the resolved target, not just a rectangle.
    needs_window = False
    # Backends that only run when asked for by name, or when `auto` escalates
    # to them (see service.capture_target), are not part of the default order.
    auto_eligible = True

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

    # -- window-aware hooks; the defaults make rect-only backends work unchanged --

    def is_available_for(self, target: TargetInfo | None, rect: Rect | None) -> bool:
        return self.is_available(rect)

    def unavailable_reason(self, target: TargetInfo | None, rect: Rect | None) -> str:
        return "backend is not available for this request"

    def capture_for(
        self, target: TargetInfo | None, rect: Rect | None, timeout_ms: int | None = None
    ) -> Image.Image | CaptureResult:
        return self.capture(rect)


_backend_instances: dict[str, CaptureBackend] = {}


def normalize_backend_name(name: str) -> str:
    selected = name.strip().lower()
    return BACKEND_ALIASES.get(selected, selected)


def _get_backend(name: str) -> CaptureBackend:
    if name not in _backend_instances:
        cls = CaptureBackend.registry.get(name)
        if cls is None:
            raise ValueError(f"Unknown capture backend: {name}")
        _backend_instances[name] = cls()
    return _backend_instances[name]


def _candidate_classes(selected: str, prefer: tuple[str, ...] = ()) -> list[type[CaptureBackend]]:
    if selected == "auto":
        ordered = sorted(
            (cls for cls in CaptureBackend.registry.values() if cls.auto_eligible),
            key=lambda cls: cls.priority,
        )
        preferred = [CaptureBackend.registry[name] for name in prefer if name in CaptureBackend.registry]
        return preferred + [cls for cls in ordered if cls not in preferred]
    cls = CaptureBackend.registry.get(selected)
    if cls is None:
        raise ValueError(f"Unknown capture backend: {selected}")
    return [cls]


def capture_region(
    rect: Rect | None,
    *,
    backend: str = "auto",
    target: TargetInfo | None = None,
    timeout_ms: int | None = None,
    prefer: tuple[str, ...] = (),
) -> CaptureResult:
    selected = normalize_backend_name(backend)
    last_error: Exception | None = None
    skipped: dict[str, str] = {}
    for cls in _candidate_classes(selected, prefer):
        inst = _get_backend(cls.name)
        if not inst.is_available_for(target, rect):
            skipped[cls.name] = inst.unavailable_reason(target, rect)
            continue
        try:
            outcome = inst.capture_for(target, rect, timeout_ms)
        except Exception as exc:
            last_error = exc
            log.warning("capture backend %s failed", inst.name, exc_info=selected != "auto")
            continue
        if isinstance(outcome, CaptureResult):
            return outcome
        return CaptureResult(image=outcome, backend=inst.name, mode=inst.mode(rect))
    if isinstance(last_error, CaptureError) and selected != "auto":
        raise last_error
    if last_error is not None:
        raise RuntimeError(f"all capture backends failed; last error: {last_error}") from last_error
    if selected != "auto" and selected in skipped:
        raise CaptureError(
            "CAPTURE_BACKEND_UNAVAILABLE",
            f"Capture backend {selected!r} is not available: {skipped[selected]}",
            retryable=False,
            backend=selected,
            reason=skipped[selected],
        )
    raise RuntimeError("no capture backend is available")
