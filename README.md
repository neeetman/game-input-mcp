# game-input-mcp

general Windows game screenshot and input MCP. It captures target game windows
through OS/display capture backends and drives foreground games through an
elevated Win32 `SendInput` daemon.

## Tools

- `list_targets()` - list visible candidate game windows.
- `get_target_info(target)` - resolve pid/hwnd and return window, client, DPI,
  monitor, and foreground metadata.
- `capture(target, region?, scope?, backend?, max_width?)` - capture target
  pixels and return `frame_id`, `image_path`, and geometry metadata.
- `focus_target(target)` / `focus_window(pid)` - bring the target foreground.
- `mouse_click(..., scope="capture", frame_id=...)`
- `mouse_drag(..., scope="capture", frame_id=...)`
- `scroll(..., scope="capture", frame_id=...)`
- `key_down(target, key, mode="scancode")`
- `key_up(target, key, mode="scancode")`
- `tap_key(target, key, mode="scancode")`
- `hotkey(target, keys, mode="vk")`
- `type_text(target, text)` - foreground text entry helper.
- `send_keys(pid, keys)` - compatibility text/key sequence helper.
- `get_window_info(pid)` - compatibility window metadata helper.
- `input_session_open(target, lease_ms?, focus?, max_hold_ms?, takeover?)` -
  open a daemon-owned input session for continuous control.
- `set_keys(session_id, down?, up?, buttons_down?, buttons_up?)` - atomic
  multi-key state change in one `SendInput` batch.
- `input_session_heartbeat(session_id)` / `input_session_state(session_id)` /
  `input_session_close(session_id)`.

## Capture-To-Click Flow

1. Call `capture({"pid": <game-pid>})`.
2. Inspect the returned PNG at `image_path`.
3. Click an image pixel using the returned frame:

```python
mouse_click(
    pid=<game-pid>,
    x=640,
    y=360,
    scope="capture",
    frame_id="<frame_id>",
)
```

## Architecture

The MCP server is a medium-integrity client. Real input and capture work is
delegated over a per-user named pipe to an elevated daemon:

```text
MCP client -> game_input_mcp.server -> named pipe -> game_input_mcp.daemon
```

The pipe name is scoped to the current user SID and the daemon keeps a
file-backed frame cache under the user's local app data directory. Capture
responses return paths and metadata instead of embedding image bytes in the
MCP response.

## Capture Backends

`backend="auto"` tries registered backends in priority order:

- `dxcam` for fast same-monitor region capture when available.
- `mss` as the general Windows screen capture fallback.
- `pillow` via `ImageGrab` as the final built-in fallback.

`windows_graphics_capture` is not included in this v1.

## Coordinate Scopes

- `screen` - absolute desktop pixel coordinates.
- `client` - target window client-area coordinates.
- `capture` - pixel coordinates in the image returned by `capture(...)`.
- `normalized` - 0.0-1.0 coordinates against the captured frame.
- `framebuffer` - deprecated alias kept for older pid-based callers.

When a mouse call includes `frame_id`, the daemon loads frame metadata from the
cache and maps `capture` coordinates back to screen coordinates before calling
`SendInput`.

## Keyboard Input

Use scan-code controls for game actions:

```python
key_down({"pid": <game-pid>}, "w", mode="scancode")
key_up({"pid": <game-pid>}, "w", mode="scancode")
tap_key({"pid": <game-pid>}, "space", mode="scancode")
hotkey({"pid": <game-pid>}, ["ctrl", "s"], mode="vk")
```

Supported scan-code names include `w`, `a`, `s`, `d`, `space`, `shift`,
`ctrl`, `alt`, `enter`, `esc`, `tab`, arrow keys, and `f1` through `f12`.

## Input Sessions (continuous control)

One-shot tools re-focus the window on every call, which is wrong for holding
W for seconds while pulsing other keys. Sessions move the held state into the
elevated daemon:

```python
s = input_session_open({"pid": <game-pid>}, lease_ms=2000)
set_keys(s["session_id"], down=["w"])                      # cruise
set_keys(s["session_id"], down=["a", "space"], up=["w"])   # one SendInput batch, ups first
input_session_heartbeat(s["session_id"])                   # keep the lease alive
input_session_close(s["session_id"])                       # releases everything
```

Safety contract:

- The daemon knows exactly what each session holds and releases all of it when
  the **lease** expires without a heartbeat, when any key is held longer than
  `max_hold_ms`, when the session is closed, when the daemon shuts down, or
  when the target window loses foreground (`FOCUS_LOST`, session `paused`).
- `focus="acquire_once"` (default) focuses once at open and never re-focuses,
  so no Alt self-press leaks into the game mid-hold. `acquire_each` keeps the
  one-shot behaviour; `none` never focuses and only verifies.
- A second session on the same window is refused (`SESSION_EXISTS`) unless
  `takeover=true`, which releases the previous session first.
- Every response carries `qpc_ns` (injection timestamp) and the resulting
  `held_keys` / `held_buttons`.

Scan-code names cover the whole US layout (`e`, `1`, `-`, `numpad5`,
`lshift`, `rctrl`, `home`, ...); unknown names fall back to
`MapVirtualKey`. Mouse buttons `left/right/middle/x1/x2` are holdable state.
Button edges are injected at the current cursor position (no implicit move),
and injected key-downs do not auto-repeat the way a physical keyboard does.

## Install

```powershell
python -m pip install -e .[dev]
```

Requires Windows and Python 3.10 or newer.

### One-Time Daemon Install

Run from an elevated shell:

```powershell
python -m game_input_mcp.install
```

Lifecycle commands:

```powershell
python -m game_input_mcp.install --status
python -m game_input_mcp.install --restart
python -m game_input_mcp.install --uninstall
```

Development daemon:

```powershell
python -m game_input_mcp.daemon
```

## MCP Registration

```json
{
  "mcpServers": {
    "game-input": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "game_input_mcp.server"]
    }
  }
}
```

If the daemon is not running, tools return a structured unavailable-daemon
error and the MCP server stays alive.

## Smoke Test

```powershell
python -m game_input_mcp.install --restart
notepad
```

Then use an MCP client:

1. `list_targets()`
2. `capture({"pid": <notepad-pid>})`
3. `mouse_click(pid=<notepad-pid>, x=20, y=20, scope="capture", frame_id="<frame_id>")`
4. `tap_key({"pid": <notepad-pid>}, "space")`

The click should land in the captured client area and `tap_key` should send one
key-down/key-up pair.
