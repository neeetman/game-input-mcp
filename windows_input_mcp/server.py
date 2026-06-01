"""MCP server exposing Windows OS-level input primitives with framebuffer↔client↔screen
scope auto-translation.

This process runs at the same integrity level as Claude Code (medium). All
real work — SendInput, focus_window, etc. — is delegated over a per-user
named pipe to the windows-input-daemon scheduled task (high IL, see
daemon.py / install.py). Doing input from a high-IL helper is what allows
the agent to drive games that are themselves elevated (UIPI blocks medium→
high SendInput).

If the daemon is not installed, every tool returns a structured error with
the install hint instead of crashing the MCP server.
"""
from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP

from .ipc import Client, DaemonUnavailable

mcp = FastMCP("windows-input")
_client = Client()


def _call(method: str, **params) -> dict:
    try:
        return _client.call(method, **params)
    except DaemonUnavailable as e:
        return {"success": False, "found": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": f"ipc error: {type(e).__name__}: {e}"}


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
    scope: Literal["framebuffer", "client", "screen"] = "framebuffer",
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
        pid=pid, x=x, y=y,
        button=button, scope=scope, clicks=clicks,
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
    scope: Literal["framebuffer", "client", "screen"] = "framebuffer",
    framebuffer_width: int | None = None,
    framebuffer_height: int | None = None,
    steps: int = 10,
    activate: bool = True,
) -> dict:
    """Drag from (from_x, from_y) to (to_x, to_y). `steps` controls smoothness."""
    return _call(
        "mouse_drag",
        pid=pid, from_x=from_x, from_y=from_y, to_x=to_x, to_y=to_y,
        button=button, scope=scope,
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
    scope: Literal["framebuffer", "client", "screen"] = "framebuffer",
    framebuffer_width: int | None = None,
    framebuffer_height: int | None = None,
    activate: bool = True,
) -> dict:
    """Mouse wheel scroll at (x, y). delta > 0 = up, < 0 = down. 120 = one notch."""
    return _call(
        "scroll",
        pid=pid, x=x, y=y, delta=delta, scope=scope,
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


def main() -> None:
    """Entry point for `windows-input-mcp` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
