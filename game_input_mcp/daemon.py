"""Elevated background daemon. Runs as a per-user scheduled task at high IL
(see install.py), listens on the user-scoped named pipe defined in ipc.py,
and executes the win32.py primitives on behalf of the (medium-IL) MCP server.

Run manually for development:
    python -m game_input_mcp.daemon

The handlers below absorb the focus+translate+send orchestration that used
to live in server.py, so the MCP tool side becomes one IPC call per tool.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import win32security

from . import config as config_module
from . import guard, ipc, presence, targets, win32
from .capture import base as capture_base
from .capture import service as capture_service
from .capture import wgc as wgc_backend
from .frames import FrameCache
from .input import keys
from .input.timeline import TimelineRunner, compile_timeline
from .input.state import (
    Edge,
    SessionExists,
    SessionNotFound,
    SessionRecord,
    SessionRegistry,
    Watchdog,
    apply_edges,
    plan_release,
    plan_state_change,
)
from .geometry import FrameGeometry, point_to_screen
from .models import Rect, TargetInfo, error_response, ok_response

log = logging.getLogger("game-input-daemon")

FRAME_CACHE = FrameCache()
SESSIONS = SessionRegistry()
# Replaced by config_module.load() in main(); defaults keep tests hermetic.
CONFIG = config_module.Config()
PRESENCE = presence.PresenceMonitor(win32.last_input_tick, win32.tick_count)


def _presence_reading() -> dict:
    return PRESENCE.reading(CONFIG.presence, CONFIG.presence_idle_s)


def _gate_focus_change(hwnd: int) -> guard.Outcome:
    """Presence gate for anything that is about to take (or hold) the
    foreground: stealing focus is class A, already being in front is class B."""
    op = "inject" if win32.get_foreground_hwnd() == hwnd else "focus"
    return guard.gate_presence(_presence_reading(), op)


# === Handlers ===============================================================
# Each handler takes a params dict and returns a JSON-serializable dict.
# The shapes mirror the current MCP tool return values verbatim, so the
# server.py refactor is a straight pass-through.

def _h_get_window_info(p: dict) -> dict:
    info = win32.get_window_info(p["pid"])
    if info is None:
        return {"found": False, "pid": p["pid"]}
    return {
        "found": True,
        "hwnd": info.hwnd,
        "pid": info.pid,
        "title": info.title,
        "window_rect": list(info.window_rect),
        "client_size": list(info.client_size),
        "client_screen_origin": list(info.client_screen_origin),
        "dpi": info.dpi,
        "is_foreground": info.is_foreground,
    }


def _h_focus_window(p: dict) -> dict:
    info = win32.get_window_info(p["pid"])
    outcome = guard.Outcome()
    if info is not None:
        outcome = _gate_focus_change(info.hwnd)
        if not outcome.ok:
            return outcome.error
    out = win32.focus_window_detailed(p["pid"])
    out["pid"] = p["pid"]
    return guard.annotate(out, outcome)


def _h_list_targets(p: dict) -> dict:
    del p
    return ok_response(targets=[target.to_dict() for target in targets.list_targets()])


def _h_get_target_info(p: dict) -> dict:
    target, error = _resolve_target(p)
    if error is not None:
        return error
    return ok_response(target=target.to_dict(), presence=_presence_reading())


def _h_focus_target(p: dict) -> dict:
    target, error = _resolve_target(p)
    if error is not None:
        return error
    outcome = _gate_focus_change(target.hwnd)
    if not outcome.ok:
        return outcome.error
    out = win32.focus_window_detailed(target.pid)
    out["pid"] = target.pid
    out["target"] = target.to_dict()
    return guard.annotate(out, outcome)


MIN_CLIENT_SIZE = 64
MAX_CLIENT_SIZE = 16384


def _pair(value, name: str, low: int, high: int) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} must be [x, y]")
    a, b = int(value[0]), int(value[1])
    if not (low <= a <= high and low <= b <= high):
        raise ValueError(f"{name} values must be within [{low}, {high}]")
    return a, b


def _h_set_window_geometry(p: dict) -> dict:
    """Resize the target's client area and/or move it. Mutates the target window,
    so it is off unless the daemon config sets allow_window_mutation."""
    if not CONFIG.allow_window_mutation:
        return error_response(
            "WINDOW_MUTATION_DISABLED",
            "Changing a window's size or position is disabled for this daemon",
            retryable=False,
            hint="set allow_window_mutation=true with `python -m game_input_mcp.install --set allow_window_mutation=true` and restart the daemon",
        )
    target, error = _resolve_target(p)
    if error is not None:
        return error
    try:
        size = _pair(p["client_size"], "client_size", MIN_CLIENT_SIZE, MAX_CLIENT_SIZE) if p.get("client_size") is not None else None
        position = _pair(p["position"], "position", -32768, 32767) if p.get("position") is not None else None
    except (ValueError, TypeError) as exc:
        return error_response("INVALID_PARAMS", str(exc))
    if size is None and position is None:
        return error_response("INVALID_PARAMS", "pass client_size and/or position")

    kind = win32.window_frame_kind(target.hwnd)
    refusal = None
    if kind["minimized"]:
        refusal = "the window is minimized"
    elif kind["maximized"]:
        refusal = "the window is maximized"
    elif kind["fullscreen"]:
        refusal = "the window covers a whole monitor (fullscreen or borderless fullscreen)"
    elif size is not None and not kind["has_frame"]:
        refusal = "the window has no caption frame, so its client size is not something to resize"
    if refusal is not None:
        return error_response(
            "WINDOW_RESIZE_FAILED", f"Not changed: {refusal}", retryable=False, target=target.to_dict(), kind=kind
        )

    ok, last_error = win32.set_window_geometry(target.hwnd, size, position)
    if not ok:
        return error_response(
            "WINDOW_RESIZE_FAILED",
            "SetWindowPos failed",
            retryable=True,
            target=target.to_dict(),
            win32_error=last_error,
        )
    after, error = _resolve_target({"target": {"hwnd": target.hwnd}})
    if error is not None:
        return error
    warnings: list[dict] = []
    if size is not None and tuple(after.client_size) != size:
        warnings.append(
            guard.warning(
                "WINDOW_SIZE_ADJUSTED",
                "The window did not take exactly the requested client size (a minimum or maximum size, or DPI rounding)",
                requested=list(size),
                actual=list(after.client_size),
            )
        )
    return guard.annotate(
        ok_response(
            target=after.to_dict(),
            requested={"client_size": list(size) if size else None, "position": list(position) if position else None},
            hint="earlier frames no longer match this window; capture again before clicking",
        ),
        guard.Outcome(warnings=warnings),
    )


def _h_capture(p: dict) -> dict:
    _, error = _resolve_target(p)
    if error is not None:
        return error
    backend = str(p.get("backend", "auto"))
    if backend.strip().lower() == "auto":
        backend = CONFIG.capture_backend  # daemon-level default, "auto" unless configured
    return capture_service.capture_target(
        target=p.get("target", p.get("pid")),
        region=p.get("region"),
        scope=p.get("scope", "client"),
        backend=backend,
        max_width=p.get("max_width", 1920),
        cache=FRAME_CACHE,
        timeout_ms=CONFIG.capture_timeout_ms,
        thumb_width=p.get("thumb_width"),
        wgc_first=CONFIG.auto_wgc_first,
    )


def _fb_size(p: dict):
    fw, fh = p.get("framebuffer_width"), p.get("framebuffer_height")
    return (fw, fh) if fw is not None and fh is not None else None


def _target_param(p: dict) -> dict | int:
    return p.get("target", p.get("pid"))


def _frame_record_geometry(frame_id: str | None) -> tuple[FrameGeometry | None, int | None]:
    if not frame_id:
        return None, None
    record = FRAME_CACHE.get(frame_id)
    if record is None:
        raise KeyError(frame_id)
    geometry = record.metadata["geometry"]
    image = record.metadata["image"]
    frame = FrameGeometry(
        image_size=(int(image["width"]), int(image["height"])),
        capture_rect_screen=Rect.from_list(geometry["capture_rect_screen"]),
        client_rect_screen=Rect.from_list(geometry["client_rect_screen"]),
        scale=float(image.get("scale", 1.0)),
    )
    dpi = geometry.get("dpi")
    return frame, int(dpi) if dpi is not None else None


def _frame_geometry(frame_id: str | None) -> FrameGeometry | None:
    return _frame_record_geometry(frame_id)[0]


def _ambiguous_error(exc: "targets.TargetAmbiguous") -> dict:
    return error_response(
        "TARGET_AMBIGUOUS",
        "The target matches windows of more than one process; pass pid or hwnd, or narrow exe/title",
        retryable=False,
        candidates=exc.candidates,
    )


def _resolve_target(p: dict) -> tuple[TargetInfo | None, dict | None]:
    try:
        target = targets.resolve_target(_target_param(p))
    except targets.TargetAmbiguous as exc:
        return None, _ambiguous_error(exc)
    except ValueError:
        target = None
    if target is None:
        return None, error_response(
            "TARGET_NOT_FOUND",
            "Target window was not found",
            retryable=True,
            params=p,
        )
    return target, None


def _resolve_frame(scope: str, frame_id: str | None) -> tuple[FrameGeometry | None, int | None, dict | None]:
    try:
        frame, dpi = _frame_record_geometry(frame_id)
    except (KeyError, TypeError, ValueError):
        frame, dpi = None, None
    if scope in {"capture", "normalized"} and frame is None:
        return None, None, error_response(
            "FRAME_NOT_FOUND",
            "Frame metadata was not found",
            retryable=True,
            frame_id=frame_id,
        )
    return frame, dpi, None


def _activate_target(target: TargetInfo, activate: bool, settle_s: float) -> dict | None:
    """Focus the target when asked. A failed focus is an error (the mouse tools
    used to ignore it and send anyway). Stealing the foreground from an active
    user is refused first when the presence policy says so."""
    if not activate:
        return None
    if win32.get_foreground_hwnd() != target.hwnd:
        gate = guard.gate_presence(_presence_reading(), "focus")
        if not gate.ok:
            return gate.error
    if not win32.focus_window(target.pid):
        return error_response(
            "FOCUS_FAILED",
            "Target window could not be focused",
            retryable=True,
            target=target.to_dict(),
        )
    if settle_s:
        time.sleep(settle_s)
    return None


def _preflight_keyboard(p: dict, settle_s: float = 0.0) -> tuple[bool, guard.Outcome | None, dict | None]:
    """Resolve, optionally focus, then require the target to be foreground.
    Returns (focused, outcome, error); activate=False no longer means "send to
    whatever is in front"."""
    target, error = _resolve_target(p)
    if error is not None:
        return False, None, error
    error = _activate_target(target, bool(p.get("activate", True)), settle_s)
    if error is not None:
        return False, None, error
    outcome = guard.gate_foreground(target.hwnd, "keyboard", mode=CONFIG.foreground_guard)
    if not outcome.ok:
        return False, None, outcome.error
    outcome = guard.merge(outcome, guard.gate_presence(_presence_reading(), "inject"))
    if not outcome.ok:
        return False, None, outcome.error
    return True, outcome, None


def _preflight_mouse(
    p: dict,
    points: list[tuple[float, float]],
) -> tuple[TargetInfo | None, list[tuple[int, int]], guard.Outcome | None, dict | None]:
    """Everything a scope-mapped mouse tool does before touching input:
    validate scope and frame, refuse stale geometry, map the points, focus,
    and require the target (or the pointer destination) to be foreground."""
    target, error = _resolve_target(p)
    if error is not None:
        return None, [], None, error
    scope = p.get("scope", "capture" if p.get("frame_id") else "framebuffer").lower().strip()
    frame, frame_dpi, error = _resolve_frame(scope, p.get("frame_id"))
    if error is not None:
        return None, [], None, error
    geometry = guard.Outcome()
    if scope in {"capture", "normalized", "framebuffer"}:
        geometry = guard.check_frame_geometry(frame, frame_dpi, target, mode=CONFIG.frame_geometry_check)
        if not geometry.ok:
            return None, [], None, geometry.error
    mapped = [
        point_to_screen(x, y, scope, target, frame=frame, framebuffer_size=_fb_size(p))
        for x, y in points
    ]
    error = _activate_target(target, bool(p.get("activate", True)), 0.05)
    if error is not None:
        return None, [], None, error
    gate = guard.gate_foreground(target.hwnd, "mouse_abs", point=mapped[0], mode=CONFIG.foreground_guard)
    if not gate.ok:
        return None, [], None, gate.error
    merged = guard.merge(geometry, gate, guard.gate_presence(_presence_reading(), "inject"))
    if not merged.ok:
        return None, [], None, merged.error
    return target, mapped, merged, None


def _scope_of(p: dict) -> str:
    return p.get("scope", "capture" if p.get("frame_id") else "framebuffer").lower().strip()


def _h_mouse_click(p: dict) -> dict:
    target, mapped, outcome, error = _preflight_mouse(p, [(p["x"], p["y"])])
    if error is not None:
        return error
    sx, sy = mapped[0]
    ok = win32.send_mouse_click(
        sx, sy, button=p.get("button", "left"), clicks=p.get("clicks", 1)
    )
    return guard.annotate(
        {
            "success": ok,
            "screen_coords": [sx, sy],
            "scope": _scope_of(p),
            "translated_from": [p["x"], p["y"]],
            "client_size": list(target.client_size),
            "client_origin": list(target.client_screen_origin),
            "target": target.to_dict(),
        },
        outcome,
    )


def _h_mouse_drag(p: dict) -> dict:
    target, mapped, outcome, error = _preflight_mouse(
        p, [(p["from_x"], p["from_y"]), (p["to_x"], p["to_y"])]
    )
    if error is not None:
        return error
    from_screen, to_screen = mapped
    ok = win32.send_mouse_drag(
        from_screen,
        to_screen,
        button=p.get("button", "left"),
        steps=p.get("steps", 10),
    )
    return guard.annotate(
        {"success": ok, "from_screen": list(from_screen), "to_screen": list(to_screen)},
        outcome,
    )


def _h_scroll(p: dict) -> dict:
    target, mapped, outcome, error = _preflight_mouse(p, [(p["x"], p["y"])])
    if error is not None:
        return error
    sx, sy = mapped[0]
    delta = p.get("delta", 120)
    ok = win32.send_scroll(sx, sy, delta)
    return guard.annotate({"success": ok, "screen_coords": [sx, sy], "delta": delta}, outcome)


def _h_send_keys(p: dict) -> dict:
    focused, outcome, error = _preflight_keyboard(p, settle_s=0.1)
    if error is not None:
        return error
    ok = win32.send_keys(p["keys"])
    return guard.annotate({"success": ok, "keys": p["keys"]}, outcome)


def _h_key_down(p: dict) -> dict:
    focused, outcome, error = _preflight_keyboard(p)
    if error is not None:
        return error
    sent = win32.send_key_down(p["key"], p.get("mode", "scancode"))
    return guard.annotate(
        ok_response(
            success=sent == 1,
            sent=sent,
            key=p["key"],
            mode=p.get("mode", "scancode"),
            focused=focused,
        ),
        outcome,
    )


def _h_key_up(p: dict) -> dict:
    # Releases are never gated: a stray key-up is harmless, a latched key-down
    # is not. activate=True still focuses first (v1 behaviour).
    if p.get("activate", True):
        target, error = _resolve_target(p)
        if error is not None:
            return error
        error = _activate_target(target, True, 0.0)
        if error is not None:
            return error
    sent = win32.send_key_up(p["key"], p.get("mode", "scancode"))
    return ok_response(
        success=sent == 1,
        sent=sent,
        key=p["key"],
        mode=p.get("mode", "scancode"),
        focused=True,
    )


def _h_tap_key(p: dict) -> dict:
    focused, outcome, error = _preflight_keyboard(p)
    if error is not None:
        return error
    sent = win32.tap_key(p["key"], p.get("mode", "scancode"), p.get("hold_ms", 30))
    return guard.annotate(
        ok_response(
            success=sent == 2,
            sent=sent,
            key=p["key"],
            mode=p.get("mode", "scancode"),
            focused=focused,
        ),
        outcome,
    )


def _h_hotkey(p: dict) -> dict:
    focused, outcome, error = _preflight_keyboard(p)
    if error is not None:
        return error
    names = p["keys"]
    mode = p.get("mode", "vk")
    strokes = [keys.resolve_key(key, mode) for key in names]
    downs = [Edge("key", key, True, stroke=stroke) for key, stroke in zip(names, strokes)]
    ups = [
        Edge("key", key, False, stroke=stroke)
        for key, stroke in reversed(list(zip(names, strokes)))
    ]
    # Two batches: the chord is pressed together and released together.
    sent = win32.send_edges(downs)
    sent += win32.send_edges(ups)
    expected = len(names) * 2
    return guard.annotate(
        ok_response(
            success=sent == expected,
            sent=sent,
            keys=names,
            mode=mode,
            focused=focused,
        ),
        outcome,
    )


def _h_type_text(p: dict) -> dict:
    focused, outcome, error = _preflight_keyboard(p)
    if error is not None:
        return error
    sent = bool(win32.send_keys(p["text"]))
    return guard.annotate(
        ok_response(success=sent, sent=int(sent), text=p["text"], focused=focused),
        outcome,
    )


@dataclass
class _TimelineRun:
    abort: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    released: list[str] = field(default_factory=list)


_TIMELINES: dict[str, "_TimelineRun"] = {}
_TIMELINES_LOCK = threading.Lock()


# === Input sessions ===========================================================
# Daemon-owned held state + lease watchdog. See
# docs/superpowers/specs/2026-09-09-continuous-control-design.md sections 1-2.

def _session_view(record: SessionRecord) -> dict:
    now = SESSIONS.now()
    return {
        "session_id": record.session_id,
        "hwnd": record.hwnd,
        "pid": record.pid,
        "status": record.status,
        "reason": record.reason,
        "focus_policy": record.focus_policy,
        "lease_ms": record.lease_ms,
        "max_hold_ms": record.max_hold_ms,
        "held_keys": sorted(record.held_keys),
        "held_buttons": sorted(record.held_buttons),
        "age_ms": int(round((now - record.opened_at) * 1000.0)),
        "since_heartbeat_ms": int(round((now - record.last_heartbeat) * 1000.0)),
        "foreground": win32.get_foreground_hwnd() == record.hwnd,
        "presence": _presence_reading(),
    }


def _abort_running_timeline(session_id: str, wait_s: float = 2.0) -> bool:
    """Stop a daemon-executed timeline on this session (if any) and wait for
    its handler to release and return. Used by close/takeover/expiry so a
    scheduler thread never keeps injecting on a dead session."""
    with _TIMELINES_LOCK:
        run = _TIMELINES.get(session_id)
    if run is None:
        return False
    run.abort.set()
    return run.done.wait(wait_s)


def _release_session(record: SessionRecord, reason: str) -> list[str]:
    """Inject up edges for everything the session holds and clear its state.
    Ups go to whatever window is foreground: a stray key-up is harmless, a
    latched key-down is not."""
    if reason not in ("aborted", "focus_lost", "user_input"):
        _abort_running_timeline(record.session_id)
    edges = plan_release(record)
    released = [edge.name for edge in edges]
    if edges:
        try:
            sent = win32.send_edges(edges)
            if sent != len(edges):
                log.warning(
                    "session %s release(%s): sent %d/%d",
                    record.session_id, reason, sent, len(edges),
                )
        finally:
            record.held_keys.clear()
            record.held_buttons.clear()
    if released:
        log.info("session %s released %s (%s)", record.session_id, released, reason)
    return released


def _release_all_sessions(reason: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for record in SESSIONS.expire_all(reason):
        try:
            out[record.session_id] = _release_session(record, reason)
        except Exception:  # noqa: BLE001
            log.exception("release failed for session %s", record.session_id)
    return out


def _watchdog_sweep() -> int:
    return Watchdog(SESSIONS, on_expire=_release_session).run_once()


def _resolve_session(p: dict) -> tuple[SessionRecord | None, dict | None]:
    session_id = p.get("session_id")
    try:
        record = SESSIONS.get(str(session_id))
    except SessionNotFound:
        return None, error_response(
            "SESSION_NOT_FOUND",
            "Input session was not found",
            session_id=session_id,
        )
    if not record.live:
        return None, error_response(
            "SESSION_EXPIRED",
            f"Input session is {record.status}",
            session_id=record.session_id,
            reason=record.reason,
            status=record.status,
        )
    return record, None


def _ensure_foreground(record: SessionRecord) -> dict | None:
    """Apply the session focus policy before an injection. acquire_each
    re-focuses (v1 behaviour); the other policies only verify and fail closed,
    releasing everything held when the target lost the foreground. Under the
    strict presence policy a user who is active also pauses the session."""
    if record.focus_policy == "acquire_each":
        if win32.get_foreground_hwnd() != record.hwnd:
            gate = guard.gate_presence(_presence_reading(), "focus")
            if not gate.ok:
                return gate.error
        if not win32.focus_window_detailed(record.pid).get("success"):
            return error_response(
                "FOCUS_FAILED",
                "Target window could not be focused",
                retryable=True,
                session_id=record.session_id,
            )
        return None
    foreground = win32.get_foreground_hwnd()
    if foreground != record.hwnd:
        released = _release_session(record, "focus_lost")
        record.status = "paused"
        record.reason = "focus_lost"
        return error_response(
            "FOCUS_LOST",
            "Target window is not foreground; held input was released",
            retryable=True,
            session_id=record.session_id,
            foreground_hwnd=foreground,
            released=released,
        )
    taken_over = _user_took_over(record)
    if taken_over is not None:
        return taken_over
    if record.status == "paused":
        record.status = "active"
        record.reason = None
    return None


def _user_took_over(record: SessionRecord) -> dict | None:
    """Strict policy: real user input is the emergency brake. Release what the
    session holds and pause it; it resumes by itself once the user has been
    idle for the threshold (the same way FOCUS_LOST pauses resume)."""
    reading = _presence_reading()
    if reading["policy"] != "strict" or reading["state"] != "present":
        return None
    released = _release_session(record, "user_input")
    record.status = "paused"
    record.reason = "user_input"
    return error_response(
        "USER_TOOK_OVER",
        "A user is active at this machine; held input was released and the session paused",
        retryable=True,
        session_id=record.session_id,
        released=released,
        user_idle_ms=reading["user_idle_ms"],
        threshold_ms=reading["threshold_ms"],
        retry_after_ms=max(0, reading["threshold_ms"] - int(reading["user_idle_ms"] or 0)),
    )


def _h_session_open(p: dict) -> dict:
    target, error = _resolve_target(p)
    if error is not None:
        return error
    focus_policy = str(p.get("focus", "acquire_once"))
    if focus_policy in ("acquire_once", "acquire_each"):
        gate = _gate_focus_change(target.hwnd)
    else:
        gate = guard.gate_presence(_presence_reading(), "inject")
    if not gate.ok:
        return gate.error
    try:
        record, replaced = SESSIONS.open(
            hwnd=target.hwnd,
            pid=target.pid,
            focus_policy=focus_policy,
            lease_ms=int(p.get("lease_ms", 2000)),
            max_hold_ms=int(p.get("max_hold_ms", 30000)),
            takeover=bool(p.get("takeover", False)),
        )
    except SessionExists as exc:
        return error_response(
            "SESSION_EXISTS",
            "Another live session already owns this window; pass takeover=true",
            session_id=exc.existing.session_id,
            hwnd=exc.existing.hwnd,
        )
    except (TypeError, ValueError) as exc:
        return error_response("INVALID_PARAMS", str(exc))
    replaced_released: list[str] = []
    if replaced is not None:
        replaced_released = _release_session(replaced, "takeover")
    if focus_policy in ("acquire_once", "acquire_each"):
        focus = win32.focus_window_detailed(record.pid)
        if not focus.get("success"):
            SESSIONS.close(record.session_id)
            return error_response(
                "FOCUS_FAILED",
                "Target window could not be focused",
                retryable=True,
                target=target.to_dict(),
                focus=focus,
            )
    view = _session_view(record)
    view["target"] = target.to_dict()
    view["replaced_session_id"] = replaced.session_id if replaced is not None else None
    view["replaced_released"] = replaced_released
    return guard.annotate(ok_response(**view), gate)


def _h_session_close(p: dict) -> dict:
    try:
        record = SESSIONS.close(str(p.get("session_id")))
    except SessionNotFound:
        return error_response(
            "SESSION_NOT_FOUND", "Input session was not found", session_id=p.get("session_id")
        )
    released = _release_session(record, "closed")
    return ok_response(session_id=record.session_id, released=released)


def _h_session_heartbeat(p: dict) -> dict:
    record, error = _resolve_session(p)
    if error is not None:
        return error
    try:
        SESSIONS.heartbeat(record.session_id, p.get("lease_ms"))
    except (TypeError, ValueError) as exc:
        return error_response("INVALID_PARAMS", str(exc))
    return ok_response(session_id=record.session_id, lease_ms=record.lease_ms)


def _h_session_state(p: dict) -> dict:
    session_id = p.get("session_id")
    try:
        record = SESSIONS.get(str(session_id))
    except SessionNotFound:
        return error_response("SESSION_NOT_FOUND", "Input session was not found", session_id=session_id)
    return ok_response(**_session_view(record))


def _h_set_keys(p: dict) -> dict:
    record, error = _resolve_session(p)
    if error is not None:
        return error
    try:
        edges, skipped = plan_state_change(
            record,
            down=p.get("down"),
            up=p.get("up"),
            buttons_down=p.get("buttons_down"),
            buttons_up=p.get("buttons_up"),
            mode=str(p.get("mode", "scancode")),
        )
    except ValueError as exc:
        return error_response("INVALID_KEY", str(exc), session_id=record.session_id)
    error = _ensure_foreground(record)
    if error is not None:
        return error
    sent = 0
    qpc = win32.qpc_ns()
    if edges:
        sent = win32.send_edges(edges)
        qpc = win32.qpc_ns()
        # Track even a partial send: the release path must cover anything the
        # OS may have accepted.
        apply_edges(record, edges, now=SESSIONS.now())
    return guard.annotate(
        ok_response(
            success=sent == len(edges),
            session_id=record.session_id,
            sent=sent,
            expected=len(edges),
            skipped=skipped,
            qpc_ns=qpc,
            held_keys=sorted(record.held_keys),
            held_buttons=sorted(record.held_buttons),
        ),
        guard.gate_presence(_presence_reading(), "inject"),
    )


# === Timelines ================================================================
# Daemon-executed scheduled edges; see the design spec section 3. The wait and
# clock are module attributes so tests can substitute a fake clock.

_timeline_clock = time.perf_counter
_timeline_spin_margin_s = 0.002


def _timeline_wait(abort: threading.Event, seconds: float) -> bool:
    return abort.wait(seconds)


def _h_run_timeline(p: dict) -> dict:
    record, error = _resolve_session(p)
    if error is not None:
        return error
    try:
        total_ms = float(p.get("total_ms", 0))
        batches = compile_timeline(
            record,
            list(p.get("events") or []),
            total_ms=total_ms,
            allow_dangling=bool(p.get("allow_dangling", False)),
        )
    except (TypeError, ValueError, KeyError) as exc:
        return error_response("INVALID_TIMELINE", str(exc), session_id=record.session_id)

    run = _TimelineRun()
    with _TIMELINES_LOCK:
        if record.session_id in _TIMELINES:
            return error_response(
                "TIMELINE_RUNNING",
                "A timeline is already running on this session",
                retryable=True,
                session_id=record.session_id,
            )
        _TIMELINES[record.session_id] = run
    try:
        error = _ensure_foreground(record)
        if error is not None:
            return error
        record.busy_until = SESSIONS.now() + total_ms / 1000.0 + 1.0

        def on_batch(batch, edges) -> None:
            now = SESSIONS.now()
            apply_edges(record, edges, now=now)
            record.last_heartbeat = now

        foreground_ok = None
        if record.focus_policy != "acquire_each":
            foreground_ok = lambda: win32.get_foreground_hwnd() == record.hwnd  # noqa: E731

        def presence_gate() -> str | None:
            reading = _presence_reading()
            if reading["policy"] == "strict" and reading["state"] == "present":
                return "user_input"
            return None

        runner = TimelineRunner(
            send=win32.send_edges,
            clock=_timeline_clock,
            wait=_timeline_wait,
            qpc_ns=win32.qpc_ns,
            foreground_ok=foreground_ok,
            gate=presence_gate,
            spin_margin_s=_timeline_spin_margin_s,
        )
        with runner.precise_timing():
            result = runner.run(batches, total_ms, run.abort, on_batch=on_batch)
        record.busy_until = None
        record.last_heartbeat = SESSIONS.now()

        released: list[str] = []
        if result.stopped_reason != "completed":
            released = _release_session(record, result.stopped_reason)
            run.released = released
            if result.stopped_reason in ("focus_lost", "user_input"):
                record.status = "paused"
                record.reason = result.stopped_reason

        payload = {
            "session_id": record.session_id,
            "stopped_reason": result.stopped_reason,
            "started_qpc_ns": result.started_qpc_ns,
            "ended_ms": round(result.ended_ms, 3),
            "total_ms": total_ms,
            "batches": [b.to_dict() for b in result.batches],
            "pending_indices": result.pending_indices,
            "released_on_exit": released,
            "held_keys": sorted(record.held_keys),
            "held_buttons": sorted(record.held_buttons),
        }
        if result.stopped_reason == "completed":
            return ok_response(
                success=all(b.sent == b.expected for b in result.batches),
                **payload,
            )
        if result.stopped_reason == "aborted":
            return error_response("ABORTED", "Timeline aborted; held input was released", **payload)
        if result.stopped_reason == "user_input":
            reading = _presence_reading()
            return error_response(
                "USER_TOOK_OVER",
                "A user became active during the timeline; held input was released",
                retryable=True,
                user_idle_ms=reading["user_idle_ms"],
                threshold_ms=reading["threshold_ms"],
                **payload,
            )
        return error_response(
            "FOCUS_LOST",
            "Target window lost foreground during the timeline; held input was released",
            retryable=True,
            **payload,
        )
    finally:
        record.busy_until = None
        with _TIMELINES_LOCK:
            _TIMELINES.pop(record.session_id, None)
        run.done.set()


def _h_mouse_move_relative(p: dict) -> dict:
    """Relative mouse motion as a one-op timeline: constant counts/s profile at
    rate_hz (duration_ms=0 -> a single MOUSEEVENTF_MOVE)."""
    try:
        duration_ms = float(p.get("duration_ms", 0) or 0)
        event = {
            "t_ms": 0,
            "op": "look",
            "dx": int(p.get("dx", 0)),
            "dy": int(p.get("dy", 0)),
            "duration_ms": duration_ms,
            "rate_hz": float(p.get("rate_hz", 250)),
        }
    except (TypeError, ValueError) as exc:
        return error_response("INVALID_PARAMS", str(exc), session_id=p.get("session_id"))
    result = _h_run_timeline(
        {"session_id": p.get("session_id"), "events": [event], "total_ms": max(duration_ms, 1.0)}
    )
    if result.get("error_code") == "INVALID_TIMELINE":
        result["error_code"] = "INVALID_PARAMS"
    batches = result.get("batches") if result.get("success") else None
    if batches:
        result["dx"], result["dy"] = event["dx"], event["dy"]
        result["steps"] = len(batches)
        result["first_qpc_ns"] = batches[0]["qpc_ns"]
        result["last_qpc_ns"] = batches[-1]["qpc_ns"]
    return result


def _h_abort_timeline(p: dict) -> dict:
    session_id = p.get("session_id")
    try:
        record = SESSIONS.get(str(session_id))
    except SessionNotFound:
        return error_response("SESSION_NOT_FOUND", "Input session was not found", session_id=session_id)
    with _TIMELINES_LOCK:
        run = _TIMELINES.get(record.session_id)
    if run is None:
        return error_response("NO_TIMELINE", "No timeline is running on this session", session_id=record.session_id)
    run.abort.set()
    finished = run.done.wait(2.0)
    return ok_response(session_id=record.session_id, aborted=finished, released=list(run.released))


HANDLERS = {
    "list_targets": _h_list_targets,
    "get_target_info": _h_get_target_info,
    "focus_target": _h_focus_target,
    "capture": _h_capture,
    "set_window_geometry": _h_set_window_geometry,
    "get_window_info": _h_get_window_info,
    "focus_window": _h_focus_window,
    "mouse_click": _h_mouse_click,
    "mouse_drag": _h_mouse_drag,
    "scroll": _h_scroll,
    "send_keys": _h_send_keys,
    "key_down": _h_key_down,
    "key_up": _h_key_up,
    "tap_key": _h_tap_key,
    "hotkey": _h_hotkey,
    "type_text": _h_type_text,
    "session_open": _h_session_open,
    "session_close": _h_session_close,
    "session_heartbeat": _h_session_heartbeat,
    "session_state": _h_session_state,
    "set_keys": _h_set_keys,
    "run_timeline": _h_run_timeline,
    "abort_timeline": _h_abort_timeline,
    "mouse_move_relative": _h_mouse_move_relative,
}


# === Security =================================================================

def build_user_scoped_security_attributes():
    """SECURITY_ATTRIBUTES with DACL granting the daemon's owning user FULL
    access, and nobody else. The daemon runs as the user (elevated IL but same
    user SID), so the client (medium IL, same user) matches and gets in;
    other users on the machine — and even Administrators of a *different*
    user — cannot open the pipe.

    We deliberately do NOT add the Administrators group: the threat we care
    about is a malicious medium-IL process under a *different* user account
    sneaking inputs into our admin-elevated game. Local Administrators can
    already do anything via UAC anyway.
    """
    user_sid_str = ipc.current_user_sid_str()
    user_sid = win32security.ConvertStringSidToSid(user_sid_str)

    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        # FILE_ALL_ACCESS for pipes — read/write/connect/etc.
        0x001F01FF,
        user_sid,
    )

    sd = win32security.SECURITY_DESCRIPTOR()
    sd.SetSecurityDescriptorDacl(1, dacl, 0)

    sa = win32security.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = 0
    return sa


# === Entry point ============================================================

def _log_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "game-input-mcp"
    base.mkdir(parents=True, exist_ok=True)
    return base / "daemon.log"


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(_log_path(), encoding="utf-8"),
            logging.StreamHandler(sys.stderr),
        ],
    )


def main() -> int:
    global CONFIG
    _setup_logging()
    CONFIG = config_module.load()
    log.info("starting; sid=%s pipe=%s", ipc.current_user_sid_str(), ipc.pipe_name())
    log.info("config: %s", CONFIG)
    # Every SendInput/keybd_event from here on is attributed to us, not the user.
    win32.set_injection_hook(PRESENCE.injecting)
    wgc_backend.configure(idle_ttl_s=CONFIG.wgc_idle_ttl_s)

    server = ipc.Server()
    for name, handler in HANDLERS.items():
        server.register(name, handler)

    watchdog = Watchdog(SESSIONS, on_expire=_release_session)
    watchdog.start()

    sa = build_user_scoped_security_attributes()
    try:
        server.serve_forever(sa)
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception:
        log.exception("fatal")
        return 1
    finally:
        watchdog.stop()
        try:
            capture_base._get_backend("wgc").close_all()
        except Exception:  # noqa: BLE001
            log.exception("closing WGC sessions failed")
        released = _release_all_sessions("daemon_shutdown")
        if released:
            log.info("shutdown released %s", released)
    return 0


if __name__ == "__main__":
    sys.exit(main())
