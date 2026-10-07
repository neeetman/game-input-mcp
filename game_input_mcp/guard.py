"""Injection guards for one-shot input tools.

A SendInput never reaches a background window: keys go to the foreground
window and a click lands on whatever is under the pointer. So a one-shot tool
that is not sure the target is in front can only leak input into another app.
These checks run before every one-shot injection; sessions keep their own
``FOCUS_LOST`` policy (daemon ``_ensure_foreground``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import win32
from .geometry import FrameGeometry
from .models import TargetInfo, error_response

KINDS = ("keyboard", "mouse_rel", "mouse_abs")


@dataclass
class Outcome:
    """Result of a guard decision. ``error`` is a ready error_response when the
    input was refused; ``warnings`` and ``guard`` annotate a response that went
    through (``warn`` mode, or the no-foreground pointer exception)."""

    error: dict | None = None
    warnings: list[dict] = field(default_factory=list)
    guard: dict | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def warning(code: str, message: str, **details: Any) -> dict:
    return {"code": code, "message": message, "details": details}


def gate_foreground(
    target_hwnd: int,
    kind: str,
    *,
    point: tuple[int, int] | None = None,
    mode: str = "strict",
) -> Outcome:
    """Decide whether a one-shot injection of ``kind`` may be sent now.

    keyboard / mouse_rel: only while the target is the foreground window.
    mouse_abs: that, or (nothing is foreground and the root window under the
    destination point is the target's) - the case windowed games create when
    they drop the foreground on a click.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown input kind: {kind}")
    if mode == "off":
        return Outcome()
    foreground = win32.get_foreground_hwnd()
    if foreground == target_hwnd:
        return Outcome()
    if kind == "mouse_abs" and foreground == 0 and point is not None:
        if win32.root_window_at(point[0], point[1]) == win32.root_window(target_hwnd):
            return Outcome(guard={"exception": "no_foreground_pointer_over_target"})
    details = {
        "foreground": win32.describe_window(foreground),
        "target_hwnd": target_hwnd,
        "kind": kind,
    }
    if mode == "warn":
        return Outcome(
            warnings=[
                warning(
                    "TARGET_NOT_FOREGROUND",
                    "Input was sent although the target is not the foreground window",
                    **details,
                )
            ]
        )
    return Outcome(
        error=error_response(
            "TARGET_NOT_FOREGROUND",
            "Target window is not foreground; input was not sent",
            retryable=True,
            hint="call focus_target first or pass activate=true",
            **details,
        )
    )


PRESENCE_OPS = ("focus", "inject")


def gate_presence(reading: dict, op: str) -> Outcome:
    """Apply the presence policy to an operation.

    op "focus": would change the foreground window (the documented incident:
    stealing focus from a typing human). op "inject": injection while the
    target is already foreground.

        policy  focus            inject
        off     allow            allow
        warn    warn             warn
        focus   USER_PRESENT     warn
        strict  USER_PRESENT     USER_PRESENT

    There is intentionally no per-call way to relax this.
    """
    if op not in PRESENCE_OPS:
        raise ValueError(f"unknown presence op: {op}")
    policy = reading["policy"]
    if policy == "off" or reading["state"] != "present":
        return Outcome()
    idle = reading["user_idle_ms"]
    details = {
        "user_idle_ms": idle,
        "threshold_ms": reading["threshold_ms"],
        "policy": policy,
        "op": op,
        "retry_after_ms": max(0, reading["threshold_ms"] - int(idle or 0)),
    }
    refuse = policy == "strict" or (policy == "focus" and op == "focus")
    if refuse:
        return Outcome(
            error=error_response(
                "USER_PRESENT",
                "A user is active at this machine; input was not sent. Wait until they are idle.",
                retryable=True,
                **details,
            )
        )
    return Outcome(
        warnings=[
            warning(
                "USER_PRESENT",
                "A user is active at this machine; their input may collide with the agent's",
                **details,
            )
        ]
    )


def check_frame_geometry(
    frame: FrameGeometry | None,
    frame_dpi: int | None,
    target: TargetInfo,
    *,
    mode: str = "strict",
) -> Outcome:
    """Refuse (or warn about) a frame-mapped click when the window moved,
    resized or changed DPI since the frame was captured."""
    if frame is None or mode == "off":
        return Outcome()
    moved = frame.client_rect_screen != target.client_rect_screen
    dpi_changed = frame_dpi is not None and int(frame_dpi) != int(target.dpi)
    if not (moved or dpi_changed):
        return Outcome()
    details = {
        "frame_client_rect": frame.client_rect_screen.to_list(),
        "current_client_rect": target.client_rect_screen.to_list(),
        "frame_dpi": frame_dpi,
        "current_dpi": target.dpi,
    }
    if mode == "warn":
        return Outcome(
            warnings=[
                warning(
                    "FRAME_GEOMETRY_CHANGED",
                    "The window moved or resized since this frame was captured",
                    **details,
                )
            ]
        )
    return Outcome(
        error=error_response(
            "FRAME_GEOMETRY_CHANGED",
            "The window moved or resized since this frame was captured; capture again",
            retryable=True,
            **details,
        )
    )


def merge(*outcomes: Outcome) -> Outcome:
    """Combine outcomes; the first error wins, warnings accumulate."""
    merged = Outcome()
    for outcome in outcomes:
        if outcome.error is not None and merged.error is None:
            merged.error = outcome.error
        merged.warnings.extend(outcome.warnings)
        if outcome.guard is not None:
            merged.guard = {**(merged.guard or {}), **outcome.guard}
    return merged


def annotate(response: dict, *outcomes: Outcome) -> dict:
    """Attach warnings/guard info to a successful response, only when present."""
    merged = merge(*outcomes)
    if merged.warnings:
        response["warnings"] = merged.warnings
    if merged.guard is not None:
        response["guard"] = merged.guard
    return response
