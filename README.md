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
- `run_timeline(session_id, events, total_ms, allow_dangling?)` - daemon-executed
  scheduled edges with per-batch `qpc_ns`; `abort_timeline(session_id)`.
- `mouse_move_relative(session_id, dx, dy, duration_ms?, rate_hz?)` - relative
  mouse motion (camera look) with a constant counts/s profile.

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

`wgc` (Windows.Graphics.Capture) is opt-in: `pip install game-input-mcp[wgc]`,
then `capture(..., backend="wgc")`. It captures the window itself, so a covered
or half off-screen window still returns its own pixels. `backend="auto"` keeps
the order above and escalates to `wgc` only when a screen grab cannot be
trusted: another window covers the target, part of it is off screen or spans
monitors, or the first grab came back black. Sessions stay warm for 5 s, so the
first capture costs ~0.3-1 s and later ones ~0.1 s.

`capture` results may carry `warnings`; read them before trusting the pixels:

| Code | Meaning |
| --- | --- |
| `TARGET_OCCLUDED` | another window covers part of the target (`details.by` names it); the pixels are not the game's |
| `TARGET_OFFSCREEN` | part of the target is off screen |
| `BLACK_FRAME` | the frame is (almost) black |
| `WGC_NO_NEW_FRAME` | the window has not redrawn recently; this is its latest frame |
| `WINDOW_MOVED_DURING_CAPTURE` | capture again before clicking |
| `FRAME_IDENTICAL_TO_PREVIOUS` | pixel-identical to this window's previous capture (`details.cpu_ms_since_previous`, `cross_check`): nothing changed, or the capture is stalled. When `wgc` produced it, a screen backend is asked as a tie-breaker; if it shows a different picture the stale `wgc` session is discarded and the screen frame is returned (`cross_check: "differs"`); if they agree it is a static scene and no warning is raised |
| `HDR_COLOR_SHIFT_POSSIBLE` | the display is in HDR mode (or Auto HDR is on and the display state is unknown): captured colours may be brighter and shifted; turn HDR / Auto HDR off for the game while capturing. Never raised on an SDR display |

Errors: `CAPTURE_BACKEND_UNAVAILABLE` (e.g. `wgc` without the package),
`CAPTURE_TIMEOUT`, `CAPTURE_REGION_UNSUPPORTED`.

Every capture also returns `frame_hash`, `hdr` (`display_hdr`, `auto_hdr`) and
`timing`: `capture_qpc_ns` (when the frame was handed over) and, for `wgc`,
`frame_qpc_ns` (when the OS composed it). Both are on the same QPC clock as the
`qpc_ns` of input edges, so a frame can be placed between two key events.

`capture(..., thumb_width=480)` also writes a small copy of the same frame
(`thumb_path`, `thumb{width,height,scale}`) that is cheap to look at. It shares the
`frame_id`; click on it with `scope="normalized"` (fractions of the image) or keep
using the full image with `scope="capture"`. No thumbnail is written unless it
would be smaller than the returned image.

## Input Safety

SendInput never reaches a background window: keys go to the foreground window
and a click lands on whatever is under the pointer. So one-shot tools now fail
closed, including `activate=False`:

- `TARGET_NOT_FOREGROUND` - the target is not the foreground window (`details`
  names the window that is). Absolute mouse tools have one exception: with *no*
  foreground window, a click is allowed if the target is under the destination
  point (windowed games that drop the foreground on a click); the response then
  carries `guard.exception`.
- `FOCUS_FAILED` - `mouse_click`/`mouse_drag`/`scroll` used to send anyway when
  focusing failed.
- `FRAME_GEOMETRY_CHANGED` - a `capture`/`normalized` click whose frame was
  taken before the window moved, resized or changed DPI; capture again.
- Key releases (`key_up`) are never blocked.

## User Presence

`get_target_info` returns `presence` (`state`, `user_idle_ms`, `threshold_ms`,
`policy`). The daemon tells the user's input from its own (its `SendInput` also
moves `GetLastInputInfo`), and applies a daemon-level policy:

| Policy | Changing the foreground | Injecting into the foreground window | Running session / timeline |
| --- | --- | --- | --- |
| `off` | allow | allow | allow |
| `warn` | warning | warning | warning |
| `focus` (default) | `USER_PRESENT` | warning | warning |
| `strict` | `USER_PRESENT` | `USER_PRESENT` | pause: `USER_TOOK_OVER` |

Warnings appear as `warnings: [{"code": "USER_PRESENT", ...}]`. Under `strict`
any real user input releases everything the session holds, stops a running
timeline (partial log and `pending_indices` returned) and pauses the session; it
resumes by itself after the user has been idle for `presence_idle_s`. Errors
carry `retry_after_ms`. Gamepad input is not visible to this signal, and input
from other injectors counts as the user's.

## Configuration

Settings are read when the daemon starts from
`%LOCALAPPDATA%\game-input-mcp\config.json`; `GAME_INPUT_<KEY>` environment
variables override the file. There is deliberately no per-call way to relax a
guard. Write and apply them with:

```powershell
python -m game_input_mcp.install --set presence=strict --set presence_idle_s=45
python -m game_input_mcp.install --restart
```

| Key | Values | Default |
| --- | --- | --- |
| `foreground_guard` | `strict`, `warn`, `off` | `strict` |
| `frame_geometry_check` | `strict`, `warn`, `off` | `strict` |
| `presence` | `off`, `warn`, `focus`, `strict` | `focus` |
| `presence_idle_s` | seconds | `30` |
| `capture_backend` | `auto`, `dxcam`, `mss`, `pillow`, `wgc` | `auto` |
| `capture_timeout_ms` | ms | `1500` |
| `wgc_idle_ttl_s` | seconds | `5` |

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

## Timelines

`run_timeline` moves timing into the daemon: the events below hold W for 2 s,
overlap a 1 s yaw look and a 120 ms jump, and return one record per injected
batch with `actual_ms` and `qpc_ns`.

```python
run_timeline(session_id, total_ms=2500, events=[
    {"t_ms": 0,    "op": "down", "key": "w"},
    {"t_ms": 300,  "op": "look", "dx": 800, "dy": 0, "duration_ms": 1000, "rate_hz": 250},
    {"t_ms": 500,  "op": "down", "key": "space"},
    {"t_ms": 620,  "op": "up",   "key": "space"},
    {"t_ms": 2000, "op": "up",   "key": "w"},
])
```

- Ops: `down`/`up` (`key`, optional `mode`), `button_down`/`button_up`
  (`button`), `look` (`dx`, `dy`, `duration_ms`, `rate_hz`; relative mouse
  counts spread evenly, integer sum exact), `wheel` (`delta`).
- Equal `t_ms` values become one `SendInput` batch, ups before downs. The
  scheduler runs at 1 ms timer resolution with a spin-wait for the last 2 ms.
- Validation fails closed before any edge is sent: unknown keys, `t_ms`
  outside `[0, total_ms]`, `total_ms` above the session `max_hold_ms`, an
  `up` for something not down, a `down` for something already down, and any
  key still down at the end unless `allow_dangling=true`.
- The request blocks until `total_ms` elapses. `abort_timeline` from another
  connection stops it (`ABORTED`); losing foreground stops it (`FOCUS_LOST`).
  Both release everything the session holds and report the partial batch log
  plus `pending_indices`.

## Relative Mouse (camera look)

`mouse_move_relative(session_id, dx=600, dy=0, duration_ms=800, rate_hz=250)`
spreads 600 counts to the right over 0.8 s as 200 integer sub-moves whose sum is
exactly 600. The events are `MOUSEEVENTF_MOVE` with `hDevice == NULL`, the same
path a physical mouse and UECapture's `CameraDrive` use, so the game's
sensitivity, smoothing and pitch clamps apply and the injected motion is
recorded as part of the action stream. `duration_ms=0` sends one move.

## Client Library (stdlib-only)

`game_input_mcp.client` talks to the daemon over the same named pipe using only
`ctypes`, so scripts need neither pywin32 nor the MCP server:

```python
from game_input_mcp.client import Client, InputError

with Client().session({"pid": pid}, lease_ms=2000) as s:   # heartbeat thread inside
    s.hold("w")
    s.run_timeline([...], total_ms=2000)
    s.look(dx=600, dy=0, duration_ms=800)
    s.tap("space", hold_ms=120)
# leaving the block closes the session -> everything released, even on exceptions
```

Structured daemon errors raise `InputError` with `.code` (`FOCUS_LOST`,
`SESSION_EXPIRED`, `INVALID_TIMELINE`, ...) and `.details`. `Client.call` is
the raw pass-through used by older tools; `Client.invoke` raises on
`success: false`.

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
