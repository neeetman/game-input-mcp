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
import time
from pathlib import Path

import win32security

from . import ipc, targets, win32
from .capture import service as capture_service
from .frames import FrameCache
from .geometry import FrameGeometry, point_to_screen
from .models import Rect, TargetInfo, error_response, ok_response

log = logging.getLogger("game-input-daemon")

FRAME_CACHE = FrameCache()


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
    out = win32.focus_window_detailed(p["pid"])
    out["pid"] = p["pid"]
    return out


def _h_list_targets(p: dict) -> dict:
    del p
    return ok_response(targets=[target.to_dict() for target in targets.list_targets()])


def _h_get_target_info(p: dict) -> dict:
    target, error = _resolve_target(p)
    if error is not None:
        return error
    return ok_response(target=target.to_dict())


def _h_focus_target(p: dict) -> dict:
    target, error = _resolve_target(p)
    if error is not None:
        return error
    out = win32.focus_window_detailed(target.pid)
    out["pid"] = target.pid
    out["target"] = target.to_dict()
    return out


def _h_capture(p: dict) -> dict:
    _, error = _resolve_target(p)
    if error is not None:
        return error
    return capture_service.capture_target(
        target=p.get("target", p.get("pid")),
        region=p.get("region"),
        scope=p.get("scope", "client"),
        backend=p.get("backend", "auto"),
        max_width=p.get("max_width", 1920),
        cache=FRAME_CACHE,
    )


def _fb_size(p: dict):
    fw, fh = p.get("framebuffer_width"), p.get("framebuffer_height")
    return (fw, fh) if fw is not None and fh is not None else None


def _target_param(p: dict) -> dict | int:
    return p.get("target", p.get("pid"))


def _frame_geometry(frame_id: str | None) -> FrameGeometry | None:
    if not frame_id:
        return None
    record = FRAME_CACHE.get(frame_id)
    if record is None:
        raise KeyError(frame_id)
    geometry = record.metadata["geometry"]
    image = record.metadata["image"]
    return FrameGeometry(
        image_size=(int(image["width"]), int(image["height"])),
        capture_rect_screen=Rect.from_list(geometry["capture_rect_screen"]),
        client_rect_screen=Rect.from_list(geometry["client_rect_screen"]),
        scale=float(image.get("scale", 1.0)),
    )


def _resolve_target(p: dict) -> tuple[TargetInfo | None, dict | None]:
    try:
        target = targets.resolve_target(_target_param(p))
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


def _resolve_frame(scope: str, frame_id: str | None) -> tuple[FrameGeometry | None, dict | None]:
    try:
        frame = _frame_geometry(frame_id)
    except (KeyError, TypeError, ValueError):
        frame = None
    if scope in {"capture", "normalized"} and frame is None:
        return None, error_response(
            "FRAME_NOT_FOUND",
            "Frame metadata was not found",
            retryable=True,
            frame_id=frame_id,
        )
    return frame, None


def _focus_if_requested(target_param, activate: bool) -> tuple[bool, dict | None]:
    if not activate:
        return True, None
    target, error = _resolve_target({"target": target_param})
    if error is not None:
        return False, error
    if not win32.focus_window(target.pid):
        return False, error_response(
            "FOCUS_FAILED",
            "Target window could not be focused",
            retryable=True,
            target=target.to_dict(),
        )
    return True, None


def _h_mouse_click(p: dict) -> dict:
    target, error = _resolve_target(p)
    if error is not None:
        return error

    if p.get("activate", True):
        win32.focus_window(target.pid)
        time.sleep(0.05)

    scope = p.get("scope", "capture" if p.get("frame_id") else "framebuffer").lower().strip()
    frame, error = _resolve_frame(scope, p.get("frame_id"))
    if error is not None:
        return error

    sx, sy = point_to_screen(
        p["x"],
        p["y"],
        scope,
        target,
        frame=frame,
        framebuffer_size=_fb_size(p),
    )
    ok = win32.send_mouse_click(
        sx, sy, button=p.get("button", "left"), clicks=p.get("clicks", 1)
    )
    return {
        "success": ok,
        "screen_coords": [sx, sy],
        "scope": scope,
        "translated_from": [p["x"], p["y"]],
        "client_size": list(target.client_size),
        "client_origin": list(target.client_screen_origin),
        "target": target.to_dict(),
    }


def _h_mouse_drag(p: dict) -> dict:
    target, error = _resolve_target(p)
    if error is not None:
        return error

    if p.get("activate", True):
        win32.focus_window(target.pid)
        time.sleep(0.05)

    scope = p.get("scope", "capture" if p.get("frame_id") else "framebuffer").lower().strip()
    frame, error = _resolve_frame(scope, p.get("frame_id"))
    if error is not None:
        return error

    from_screen = point_to_screen(
        p["from_x"],
        p["from_y"],
        scope,
        target,
        frame=frame,
        framebuffer_size=_fb_size(p),
    )
    to_screen = point_to_screen(
        p["to_x"],
        p["to_y"],
        scope,
        target,
        frame=frame,
        framebuffer_size=_fb_size(p),
    )
    ok = win32.send_mouse_drag(
        from_screen,
        to_screen,
        button=p.get("button", "left"),
        steps=p.get("steps", 10),
    )
    return {
        "success": ok,
        "from_screen": list(from_screen),
        "to_screen": list(to_screen),
    }


def _h_scroll(p: dict) -> dict:
    target, error = _resolve_target(p)
    if error is not None:
        return error

    if p.get("activate", True):
        win32.focus_window(target.pid)
        time.sleep(0.05)

    scope = p.get("scope", "capture" if p.get("frame_id") else "framebuffer").lower().strip()
    frame, error = _resolve_frame(scope, p.get("frame_id"))
    if error is not None:
        return error

    sx, sy = point_to_screen(
        p["x"],
        p["y"],
        scope,
        target,
        frame=frame,
        framebuffer_size=_fb_size(p),
    )
    delta = p.get("delta", 120)
    ok = win32.send_scroll(sx, sy, delta)
    return {"success": ok, "screen_coords": [sx, sy], "delta": delta}


def _h_send_keys(p: dict) -> dict:
    focused, error = _focus_if_requested(_target_param(p), p.get("activate", True))
    if error is not None:
        return error
    if focused and p.get("activate", True):
        time.sleep(0.1)
    ok = win32.send_keys(p["keys"])
    return {"success": ok, "keys": p["keys"]}


def _h_key_down(p: dict) -> dict:
    focused, error = _focus_if_requested(_target_param(p), p.get("activate", True))
    if error is not None:
        return error
    sent = win32.send_key_down(p["key"], p.get("mode", "scancode"))
    return ok_response(
        success=sent == 1,
        sent=sent,
        key=p["key"],
        mode=p.get("mode", "scancode"),
        focused=focused,
    )


def _h_key_up(p: dict) -> dict:
    focused, error = _focus_if_requested(_target_param(p), p.get("activate", True))
    if error is not None:
        return error
    sent = win32.send_key_up(p["key"], p.get("mode", "scancode"))
    return ok_response(
        success=sent == 1,
        sent=sent,
        key=p["key"],
        mode=p.get("mode", "scancode"),
        focused=focused,
    )


def _h_tap_key(p: dict) -> dict:
    focused, error = _focus_if_requested(_target_param(p), p.get("activate", True))
    if error is not None:
        return error
    sent = win32.tap_key(p["key"], p.get("mode", "scancode"), p.get("hold_ms", 30))
    return ok_response(
        success=sent == 2,
        sent=sent,
        key=p["key"],
        mode=p.get("mode", "scancode"),
        focused=focused,
    )


def _h_hotkey(p: dict) -> dict:
    focused, error = _focus_if_requested(_target_param(p), p.get("activate", True))
    if error is not None:
        return error
    keys = p["keys"]
    mode = p.get("mode", "vk")
    sent = 0
    for key in keys:
        sent += win32.send_key_down(key, mode)
    for key in reversed(keys):
        sent += win32.send_key_up(key, mode)
    expected = len(keys) * 2
    return ok_response(
        success=sent == expected,
        sent=sent,
        keys=keys,
        mode=mode,
        focused=focused,
    )


def _h_type_text(p: dict) -> dict:
    focused, error = _focus_if_requested(_target_param(p), p.get("activate", True))
    if error is not None:
        return error
    sent = bool(win32.send_keys(p["text"]))
    return ok_response(success=sent, sent=int(sent), text=p["text"], focused=focused)


HANDLERS = {
    "list_targets": _h_list_targets,
    "get_target_info": _h_get_target_info,
    "focus_target": _h_focus_target,
    "capture": _h_capture,
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
    _setup_logging()
    log.info("starting; sid=%s pipe=%s", ipc.current_user_sid_str(), ipc.pipe_name())

    server = ipc.Server()
    for name, handler in HANDLERS.items():
        server.register(name, handler)

    sa = build_user_scoped_security_attributes()
    try:
        server.serve_forever(sa)
    except KeyboardInterrupt:
        log.info("interrupted")
    except Exception:
        log.exception("fatal")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
