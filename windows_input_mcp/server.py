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

from typing import Any, Literal

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


def main() -> None:
    """Entry point for `windows-input-mcp` console script."""
    mcp.run()


if __name__ == "__main__":
    main()
