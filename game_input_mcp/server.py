"""MCP server exposing Windows OS-level input primitives with framebuffer↔client↔screen
scope auto-translation.

This process runs at the same integrity level as Claude Code (medium). All
real work — SendInput, focus_window, etc. — is delegated over a per-user
named pipe to the game-input-daemon scheduled task (high IL, see
daemon.py / install.py). Doing input from a high-IL helper is what allows
the agent to drive games that are themselves elevated (UIPI blocks medium→
high SendInput).

If the daemon is not installed, every tool returns a structured error with
the install hint instead of crashing the MCP server.
"""
from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .ipc import Client, DaemonUnavailable

mcp = FastMCP("game-input")
_client = Client()


def _call(method: str, **params) -> dict:
    try:
        return _client.call(method, **params)
    except DaemonUnavailable as e:
        return {"success": False, "found": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"ipc error: {type(e).__name__}: {e}"}


@mcp.tool()
def list_targets() -> dict:
    """List visible target windows that can be captured or driven."""
    return _call("list_targets")


@mcp.tool()
def get_target_info(target: dict[str, Any]) -> dict:
    """Return resolved target metadata for a pid/hwnd target object."""
    return _call("get_target_info", target=target)


@mcp.tool()
def focus_target(target: dict[str, Any]) -> dict:
    """Bring a pid/hwnd target object to the foreground."""
    return _call("focus_target", target=target)


@mcp.tool()
def capture(
    target: dict[str, Any],
    region: list[int] | None = None,
    scope: Literal["client", "screen"] = "client",
    backend: Literal["auto", "dxcam", "mss", "pillow"] = "auto",
    max_width: int = 1920,
) -> dict:
    """Capture target pixels and return frame metadata plus image_path."""
    return _call(
        "capture",
        target=target,
        region=region,
        scope=scope,
        backend=backend,
        max_width=max_width,
    )


@mcp.tool()
def get_window_info(pid: int) -> dict:
    """Return window metadata for a given process pid.

    Useful for an agent to learn:
      - whether the target window exists and is foreground
      - client size vs framebuffer ratio (need framebuffer_size from screenshot)
      - screen origin (negative on multi-monitor secondary displays)
      - DPI for further scaling decisions

    Returns:
      {
        "found": bool,
        "hwnd": int,
        "title": str,
        "window_rect": [l, t, r, b],          # screen coords
        "client_size": [w, h],                # client area pixels
        "client_screen_origin": [x, y],       # ClientToScreen((0,0))
        "dpi": int,
        "is_foreground": bool,
      }
    """
    return _call("get_window_info", pid=pid)


@mcp.tool()
def focus_window(pid: int) -> dict:
    """Bring the target window to foreground. Restores from minimized.

    SendInput won't deliver to a background window for many Unity games — call
    this first if keystrokes are getting lost.
    """
    return _call("focus_window", pid=pid)


@mcp.tool()
def mouse_click(
    pid: int,
    x: int,
    y: int,
    button: Literal["left", "right", "middle"] = "left",
    scope: Literal["framebuffer", "capture", "client", "screen"] = "framebuffer",
    frame_id: str | None = None,
    clicks: int = 1,
    framebuffer_width: int | None = None,
    framebuffer_height: int | None = None,
    activate: bool = True,
) -> dict:
    """Click at (x, y) in the given coordinate scope.

    scope:
      - "screen"     : (x, y) is absolute screen coords (passes through unchanged)
      - "client"     : (x, y) is relative to window client area top-left
      - "framebuffer": (x, y) is in Unity render framebuffer coords. Pass
                        framebuffer_width / framebuffer_height (from UE
                        screenshot dimensions) to auto-scale to client.
                        If omitted, assumes framebuffer == client (no scale).

    activate=True (default) brings the window to foreground first. Set to False
    if you want to send input to a background window (rarely works for Unity).
    """
    return _call(
        "mouse_click",
        pid=pid, target={"pid": pid},
        x=x, y=y,
        button=button, scope=scope, clicks=clicks,
        frame_id=frame_id,
        framebuffer_width=framebuffer_width,
        framebuffer_height=framebuffer_height,
        activate=activate,
    )


@mcp.tool()
def mouse_drag(
    pid: int,
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    button: Literal["left", "right"] = "left",
    scope: Literal["framebuffer", "capture", "client", "screen"] = "framebuffer",
    frame_id: str | None = None,
    framebuffer_width: int | None = None,
    framebuffer_height: int | None = None,
    steps: int = 10,
    activate: bool = True,
) -> dict:
    """Drag from (from_x, from_y) to (to_x, to_y). `steps` controls smoothness."""
    return _call(
        "mouse_drag",
        pid=pid, target={"pid": pid},
        from_x=from_x, from_y=from_y, to_x=to_x, to_y=to_y,
        button=button, scope=scope, frame_id=frame_id,
        framebuffer_width=framebuffer_width,
        framebuffer_height=framebuffer_height,
        steps=steps, activate=activate,
    )


@mcp.tool()
def scroll(
    pid: int,
    x: int,
    y: int,
    delta: int = 120,
    scope: Literal["framebuffer", "capture", "client", "screen"] = "framebuffer",
    frame_id: str | None = None,
    framebuffer_width: int | None = None,
    framebuffer_height: int | None = None,
    activate: bool = True,
) -> dict:
    """Mouse wheel scroll at (x, y). delta > 0 = up, < 0 = down. 120 = one notch."""
    return _call(
        "scroll",
        pid=pid, target={"pid": pid}, x=x, y=y, delta=delta, scope=scope,
        frame_id=frame_id,
        framebuffer_width=framebuffer_width,
        framebuffer_height=framebuffer_height,
        activate=activate,
    )


@mcp.tool()
def send_keys(pid: int, keys: str, activate: bool = True) -> dict:
    """Send keyboard input to the target window.

    `keys` string supports:
      - Literal characters: "Hello"
      - Named keys in braces: "{enter}", "{esc}", "{space}", "{tab}",
        "{left}", "{up}", "{right}", "{down}", "{f1}"–"{f12}"
      - Mixed: "Hello{enter}"

    Common navigate-to-gameplay patterns:
      - Dismiss "press any key" splash: send_keys(pid, "{space}")
      - Confirm dialog: send_keys(pid, "{enter}")
      - Skip cutscene: send_keys(pid, "{esc}")
    """
    return _call("send_keys", pid=pid, keys=keys, activate=activate)


@mcp.tool()
def key_down(
    target: dict[str, Any],
    key: str,
    mode: Literal["scancode", "vk"] = "scancode",
    activate: bool = True,
) -> dict:
    return _call("key_down", target=target, key=key, mode=mode, activate=activate)


@mcp.tool()
def key_up(
    target: dict[str, Any],
    key: str,
    mode: Literal["scancode", "vk"] = "scancode",
    activate: bool = True,
) -> dict:
    return _call("key_up", target=target, key=key, mode=mode, activate=activate)


@mcp.tool()
def tap_key(
    target: dict[str, Any],
    key: str,
    mode: Literal["scancode", "vk"] = "scancode",
    hold_ms: int = 30,
    activate: bool = True,
) -> dict:
    return _call(
        "tap_key",
        target=target,
        key=key,
        mode=mode,
        hold_ms=hold_ms,
        activate=activate,
    )


@mcp.tool()
def hotkey(
    target: dict[str, Any],
    keys: list[str],
    mode: Literal["scancode", "vk"] = "vk",
    activate: bool = True,
) -> dict:
    return _call("hotkey", target=target, keys=keys, mode=mode, activate=activate)


@mcp.tool()
def type_text(target: dict[str, Any], text: str, activate: bool = True) -> dict:
    return _call("type_text", target=target, text=text, activate=activate)


# --- Input sessions (daemon-owned held state + lease watchdog) ---------------

@mcp.tool()
def input_session_open(
    target: dict[str, Any],
    lease_ms: int = 2000,
    focus: Literal["acquire_once", "acquire_each", "none"] = "acquire_once",
    max_hold_ms: int = 30000,
    takeover: bool = False,
) -> dict:
    """Open an input session on a game window for continuous control.

    The daemon tracks every key/button the session holds and releases all of
    them when the lease expires without a heartbeat, when a key is held longer
    than max_hold_ms, when the target loses foreground (acquire_once/none), or
    when the session is closed. focus="acquire_once" focuses the window now
    and never re-focuses (no Alt self-press leaking mid-hold); later sends fail
    closed with FOCUS_LOST if the window is not foreground.
    """
    return _call(
        "session_open",
        target=target,
        lease_ms=lease_ms,
        focus=focus,
        max_hold_ms=max_hold_ms,
        takeover=takeover,
    )


@mcp.tool()
def input_session_close(session_id: str) -> dict:
    """Close a session and release everything it still holds."""
    return _call("session_close", session_id=session_id)


@mcp.tool()
def input_session_heartbeat(session_id: str, lease_ms: int | None = None) -> dict:
    """Extend a session lease (optionally changing lease_ms)."""
    return _call("session_heartbeat", session_id=session_id, lease_ms=lease_ms)


@mcp.tool()
def input_session_state(session_id: str) -> dict:
    """Return held keys/buttons, status, age and foreground state of a session."""
    return _call("session_state", session_id=session_id)


@mcp.tool()
def set_keys(
    session_id: str,
    down: list[str] | None = None,
    up: list[str] | None = None,
    buttons_down: list[str] | None = None,
    buttons_up: list[str] | None = None,
    mode: Literal["scancode", "vk"] = "scancode",
) -> dict:
    """Atomically change held key/mouse-button state in one SendInput batch.

    Ups are injected before downs. Keys already in the requested state are
    skipped. Any scan-code name (letters, digits, punctuation, numpad, lshift/
    rctrl/..., arrows, f1-f12) and buttons left/right/middle/x1/x2 are valid.
    Returns qpc_ns of the injection for alignment with recorded input.
    """
    return _call(
        "set_keys",
        session_id=session_id,
        down=down,
        up=up,
        buttons_down=buttons_down,
        buttons_up=buttons_up,
        mode=mode,
    )


@mcp.tool()
def run_timeline(
    session_id: str,
    events: list[dict[str, Any]],
    total_ms: int,
    allow_dangling: bool = False,
) -> dict:
    """Execute a scheduled multi-key timeline inside the daemon and block until it ends.

    events: [{"t_ms": 0, "op": "down", "key": "w"}, {"t_ms": 800, "op": "up", "key": "w"},
             {"t_ms": 100, "op": "button_down", "button": "left"},
             {"t_ms": 200, "op": "look", "dx": 600, "dy": 0, "duration_ms": 500, "rate_hz": 250},
             {"t_ms": 300, "op": "wheel", "delta": -120}, ...]
    Edges at the same t_ms are injected as one SendInput batch (ups first); each
    batch returns actual_ms and qpc_ns. Validation fails closed: every down needs a
    matching up inside total_ms unless allow_dangling=true, total_ms <= max_hold_ms.
    Abort or focus loss stops the run and releases everything the session holds.
    """
    return _call(
        "run_timeline",
        session_id=session_id,
        events=events,
        total_ms=total_ms,
        allow_dangling=allow_dangling,
    )


@mcp.tool()
def mouse_move_relative(
    session_id: str,
    dx: int,
    dy: int,
    duration_ms: int = 0,
    rate_hz: int = 250,
) -> dict:
    """Relative mouse motion (camera look) on an input session.

    dx/dy are raw mouse counts (positive dx = right, positive dy = down), spread
    evenly over duration_ms at rate_hz as integer sub-moves whose sum is exact;
    duration_ms=0 sends one move. Injected like a physical mouse
    (MOUSEEVENTF_MOVE, hDevice==NULL), so UECapture records it as action data.
    Returns steps, first_qpc_ns/last_qpc_ns and the per-batch log.
    """
    return _call(
        "mouse_move_relative",
        session_id=session_id,
        dx=dx,
        dy=dy,
        duration_ms=duration_ms,
        rate_hz=rate_hz,
    )


@mcp.tool()
def abort_timeline(session_id: str) -> dict:
    """Stop the running timeline on a session and release all held input."""
    return _call("abort_timeline", session_id=session_id)


def main() -> None:
    """Entry point for `game-input-mcp` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
