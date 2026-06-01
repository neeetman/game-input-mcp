"""Elevated background daemon. Runs as a per-user scheduled task at high IL
(see install.py), listens on the user-scoped named pipe defined in ipc.py,
and executes the win32.py primitives on behalf of the (medium-IL) MCP server.

Run manually for development:
    python -m windows_input_mcp.daemon

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

from . import ipc, win32

log = logging.getLogger("windows-input-daemon")


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


def _fb_size(p: dict):
    fw, fh = p.get("framebuffer_width"), p.get("framebuffer_height")
    return (fw, fh) if fw is not None and fh is not None else None


def _h_mouse_click(p: dict) -> dict:
    info = win32.get_window_info(p["pid"])
    if info is None:
        return {"success": False, "error": f"window not found for pid {p['pid']}"}
    if p.get("activate", True):
        win32.focus_window(p["pid"])
        time.sleep(0.05)
    sx, sy = win32.translate_to_screen(
        info, p["x"], p["y"], p.get("scope", "framebuffer"), _fb_size(p)
    )
    ok = win32.send_mouse_click(
        sx, sy, button=p.get("button", "left"), clicks=p.get("clicks", 1)
    )
    return {
        "success": ok,
        "screen_coords": [sx, sy],
        "scope": p.get("scope", "framebuffer"),
        "translated_from": [p["x"], p["y"]],
        "client_size": list(info.client_size),
        "client_origin": list(info.client_screen_origin),
    }


def _h_mouse_drag(p: dict) -> dict:
    info = win32.get_window_info(p["pid"])
    if info is None:
        return {"success": False, "error": f"window not found for pid {p['pid']}"}
    if p.get("activate", True):
        win32.focus_window(p["pid"])
        time.sleep(0.05)
    scope = p.get("scope", "framebuffer")
    fb = _fb_size(p)
    f_screen = win32.translate_to_screen(info, p["from_x"], p["from_y"], scope, fb)
    t_screen = win32.translate_to_screen(info, p["to_x"], p["to_y"], scope, fb)
    ok = win32.send_mouse_drag(
        f_screen, t_screen,
        button=p.get("button", "left"),
        steps=p.get("steps", 10),
    )
    return {"success": ok, "from_screen": list(f_screen), "to_screen": list(t_screen)}


def _h_scroll(p: dict) -> dict:
    info = win32.get_window_info(p["pid"])
    if info is None:
        return {"success": False, "error": f"window not found for pid {p['pid']}"}
    if p.get("activate", True):
        win32.focus_window(p["pid"])
        time.sleep(0.05)
    sx, sy = win32.translate_to_screen(
        info, p["x"], p["y"], p.get("scope", "framebuffer"), _fb_size(p)
    )
    delta = p.get("delta", 120)
    ok = win32.send_scroll(sx, sy, delta)
    return {"success": ok, "screen_coords": [sx, sy], "delta": delta}


def _h_send_keys(p: dict) -> dict:
    if p.get("activate", True):
        if not win32.focus_window(p["pid"]):
            return {"success": False, "error": f"failed to focus window for pid {p['pid']}"}
        time.sleep(0.1)
    ok = win32.send_keys(p["keys"])
    return {"success": ok, "keys": p["keys"]}


HANDLERS = {
    "get_window_info": _h_get_window_info,
    "focus_window": _h_focus_window,
    "mouse_click": _h_mouse_click,
    "mouse_drag": _h_mouse_drag,
    "scroll": _h_scroll,
    "send_keys": _h_send_keys,
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
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "windows-input-mcp"
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
