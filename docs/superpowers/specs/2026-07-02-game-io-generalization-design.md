# Game I/O Generalization Design

Date: 2026-07-02

## Summary

Evolve this project from a Unity-oriented Windows input MCP into a general
Windows game I/O MCP. Version 1 stays Windows-only and non-injected. It should
capture game pixels through OS/display capture APIs, drive games through
elevated OS-level input, and keep a reliable coordinate link between each
captured frame and later mouse actions.

The current elevated daemon and named-pipe split remain the foundation. The
main change is to add target-aware screenshot capture and replace the
Unity-specific `framebuffer` concept with a general `capture` coordinate
space.

## Context

The current project exposes these capabilities:

- Find a target window by process id.
- Focus the target window.
- Send mouse click, drag, scroll, and simple keyboard input.
- Translate `framebuffer`, `client`, and `screen` coordinates.
- Delegate real Win32 work to an elevated per-user daemon so input can reach
  elevated games.

The current project does not capture screenshots. It also treats
`framebuffer` as a Unity render buffer concept, requiring callers to pass
`framebuffer_width` and `framebuffer_height` manually.

`D:\MCP\Windows-MCP` provides useful engineering patterns:

- Screenshot backend registry with automatic fallback.
- A fast screenshot tool separate from a richer desktop snapshot tool.
- Screenshot metadata that reports scale, region, and backend.
- Multi-display and non-zero-origin screenshot tests.
- Config-driven backend selection and profiling.

It should not be copied wholesale. Its UI Automation tree and high-level
desktop input paths are built for normal applications, and its README states
that it is not intended for playing video games. This project should keep the
elevated `SendInput` path.

## Goals

- Provide a generic game screenshot tool that works without Unity, Unreal, or
  engine injection.
- Provide a generic game input toolset with stable mouse and keyboard control.
- Make every screenshot self-describing enough that later clicks can use
  image coordinates safely.
- Support windowed, borderless-windowed, and visible full-screen game cases as
  well as practical Windows APIs allow.
- Preserve the elevated daemon model for admin-launched games.
- Keep the MCP surface small and focused on game capture and input.
- Maintain backward compatibility for the existing input tools during a
  migration period.

## Non-Goals

- Cross-platform support in v1.
- Engine-specific capture or injection.
- UI Automation element trees, DOM extraction, or label-based desktop clicks.
- Filesystem, registry, shell, browser scraping, or other broad Windows
  automation tools.
- Anti-cheat bypass. The tool should not attempt to evade game protection.
- Guaranteed background input. Many games require foreground focus and OS
  policy cannot be bypassed generally.

## Recommended Approach

Use a target-aware capture and input architecture:

1. Target selection resolves a game target to pid, hwnd, window geometry, and
   display geometry.
2. Capture backends return an image plus metadata that maps image pixels back
   to client and screen coordinates.
3. Input tools accept coordinates in `capture`, `client`, `screen`, or
   `normalized` space.
4. The daemon performs both capture and input when elevation is needed. The
   MCP server remains a thin schema and IPC layer.

Alternative approaches were considered:

- Reuse Windows-MCP directly. This has mature desktop automation features, but
  it is UIA-centric and not game-oriented.
- Build an engine adapter first. This may produce high-quality captures for
  one engine but does not solve the generic game requirement.
- Build only a screenshot tool and keep current input unchanged. This is
  smaller, but it leaves the important screenshot-to-click coordinate loop
  fragile.

The recommended approach is the third way: keep the existing elevated input
architecture, add game-aware capture, and define the coordinate contract
between them.

## Architecture

Proposed package layout:

```text
windows_input_mcp/
  server.py
  daemon.py
  ipc.py
  targets.py
  geometry.py
  frames.py
  input/
    __init__.py
    sendinput.py
    keys.py
  capture/
    __init__.py
    base.py
    gdi.py
    dxcam_backend.py
    mss_backend.py
    wgc.py
```

Responsibilities:

- `server.py`: MCP tool definitions, argument validation, thin daemon calls.
- `daemon.py`: handler registration and privileged execution.
- `ipc.py`: named-pipe framing and daemon availability errors.
- `targets.py`: window enumeration, target resolution, foreground state,
  monitor geometry, process metadata.
- `geometry.py`: coordinate transforms among capture, client, screen, and
  normalized spaces.
- `frames.py`: short-lived frame metadata cache keyed by `frame_id`.
- `input/sendinput.py`: mouse and keyboard primitives using Win32 `SendInput`.
- `input/keys.py`: key name, virtual-key, and scan-code mapping.
- `capture/base.py`: backend protocol and result types.
- `capture/*`: capture backend implementations.

The package name can remain `windows_input_mcp` for compatibility. A new
console script such as `game-io-mcp` can be added later, while the existing
`windows-input-mcp` entry point remains available.

## Target Model

Most tools should accept a target object rather than only `pid`:

```json
{
  "pid": 1234,
  "hwnd": 5678
}
```

Rules:

- `hwnd` is preferred when supplied.
- `pid` resolves to the best visible top-level render window when `hwnd` is
  omitted.
- The resolver should return structured diagnostics when multiple windows
  match, when the target is minimized, or when the window disappears.
- The old `pid` argument remains supported by compatibility wrappers.

## Coordinate Model

Use these coordinate spaces:

- `screen`: Windows virtual desktop physical pixels.
- `client`: target window client-area pixels, relative to client top-left.
- `capture`: pixels in the returned screenshot image.
- `normalized`: floating point coordinates in the range `[0.0, 1.0]` relative
  to the returned screenshot image.

`framebuffer` becomes a deprecated alias for `capture` during migration.

Each capture returns enough metadata to convert later coordinates:

```json
{
  "frame_id": "frame_000001",
  "target": {
    "pid": 1234,
    "hwnd": 5678,
    "title": "Game"
  },
  "image": {
    "width": 1280,
    "height": 720,
    "format": "png",
    "scale": 1.0
  },
  "geometry": {
    "window_rect_screen": [100, 100, 1700, 1000],
    "client_rect_screen": [100, 139, 1700, 1000],
    "capture_rect_screen": [100, 139, 1700, 1000],
    "client_size": [1600, 861],
    "dpi": 144
  },
  "backend": {
    "name": "dxcam",
    "mode": "screen_region"
  }
}
```

Mouse tools can accept either a `frame_id` or explicit geometry metadata. When
`frame_id` is present, the daemon resolves `capture` coordinates through the
cached metadata from that frame.

## Capture Backends

Use a backend registry with explicit priorities and automatic fallback.

Initial v1 backends:

1. `dxcam`: DXGI desktop duplication through the existing Python package. Good
   first backend because Windows-MCP already demonstrates the pattern and it
   has practical multi-display handling.
2. `mss`: secondary display-region fallback.
3. `pillow`: last-resort fallback for ordinary windows and development.

Planned backend:

- `windows_graphics_capture`: target-window capture for better behavior on
  borderless games and occlusion-sensitive cases. This should become the
  preferred backend once implemented and proven stable.

Backend rules:

- `auto` tries backends by priority.
- Explicit backend selection is available for diagnosis.
- Backend result includes backend name and mode.
- A backend may decline a request before capture if the region crosses
  monitors or the target state is unsupported.
- Capture code should detect obviously empty black frames and return a warning
  or fallback when possible.

Images should not be pushed through the daemon pipe as large JSON blobs. The
daemon should either return MCP image bytes through the server after a bounded
IPC transfer, or write the PNG to a per-user cache and return `path`,
`frame_id`, and metadata. The initial implementation should cap encoded image
size and downscale when requested.

## MCP Tools

Version 1 tool surface:

- `list_targets()`: list visible candidate game windows with pid, hwnd, title,
  process name, status, client size, and monitor index.
- `get_target_info(target)`: return resolved window, client, DPI, monitor, and
  foreground data.
- `capture(target, region=None, scope="client", backend="auto",
  max_width=1920, format="png")`: capture pixels and return image plus
  metadata.
- `focus_target(target)`: bring the target to foreground and return detailed
  focus diagnostics.
- `mouse_click(target, x, y, scope="capture", frame_id=None, button="left",
  clicks=1, activate=True)`.
- `mouse_drag(target, from_x, from_y, to_x, to_y, scope="capture",
  frame_id=None, button="left", steps=10, activate=True)`.
- `scroll(target, x, y, delta=120, scope="capture", frame_id=None,
  activate=True)`.
- `key_down(target, key, mode="scancode", activate=True)`.
- `key_up(target, key, mode="scancode", activate=True)`.
- `tap_key(target, key, mode="scancode", hold_ms=30, activate=True)`.
- `hotkey(target, keys, mode="vk", activate=True)`.
- `type_text(target, text, activate=True)`.

Compatibility tools:

- Keep `get_window_info(pid)`, `focus_window(pid)`, `mouse_click(pid, ...)`,
  `mouse_drag(pid, ...)`, `scroll(pid, ...)`, and `send_keys(pid, ...)`.
- Treat `scope="framebuffer"` as `scope="capture"` when a `frame_id` or
  explicit framebuffer dimensions are supplied.
- Emit a deprecation warning in results, not in stderr.

## Input Design

The existing `SendInput` implementation remains the primary input path.

Enhancements:

- Add scan-code key support for game controls such as WASD, Shift, Ctrl,
  Space, Escape, function keys, and arrow keys.
- Add `key_down` and `key_up` so agents can hold movement keys.
- Add `tap_key` for short deterministic presses.
- Add `hotkey` for system-style combinations.
- Keep Unicode text typing as a separate `type_text` path.
- Optionally paste long plain text through the clipboard with prior clipboard
  restoration, but only for text entry, never for game movement controls.

Every input result should report:

- Target hwnd and pid.
- Whether focus was attempted.
- Whether the target was foreground after focus.
- Translated screen coordinates when applicable.
- Number of Win32 input events requested and sent.

## Error Handling

Use structured error responses instead of free-form strings:

```json
{
  "success": false,
  "error_code": "TARGET_MINIMIZED",
  "message": "Target window is minimized and cannot be captured.",
  "retryable": true,
  "details": {
    "pid": 1234,
    "hwnd": 5678
  }
}
```

Important error codes:

- `DAEMON_UNAVAILABLE`
- `TARGET_NOT_FOUND`
- `TARGET_AMBIGUOUS`
- `TARGET_MINIMIZED`
- `TARGET_NOT_FOREGROUND`
- `CAPTURE_BACKEND_UNAVAILABLE`
- `CAPTURE_BACKEND_FAILED`
- `CAPTURE_BLACK_FRAME`
- `CAPTURE_REGION_UNSUPPORTED`
- `FRAME_NOT_FOUND`
- `COORDINATE_OUT_OF_BOUNDS`
- `INPUT_SEND_FAILED`
- `UNSUPPORTED_KEY`

## Configuration

Environment variables:

- `GAME_IO_CAPTURE_BACKEND`: `auto`, `dxcam`, `mss`, `pillow`,
  `windows_graphics_capture`.
- `GAME_IO_CAPTURE_SCALE`: float from `0.1` to `1.0`.
- `GAME_IO_MAX_IMAGE_WIDTH`: default `1920`.
- `GAME_IO_MAX_IMAGE_HEIGHT`: default `1080`.
- `GAME_IO_PROFILE_CAPTURE`: enable timing logs.
- `GAME_IO_FRAME_CACHE_TTL_SEC`: default `30`.
- `GAME_IO_CAPTURE_FLASH`: default `false`; set to `true` only for manual
  debugging because overlays can interfere with game capture.

Names can also be offered with the old `WINDOWS_INPUT_MCP_` prefix if keeping
project branding is preferred. The design should avoid the broad
`WINDOWS_MCP_` prefix to reduce confusion with the separate Windows-MCP
project.

## Security

Keep the current user-scoped named pipe DACL. The daemon should continue to
run as the same user at elevated integrity, not as a machine-wide service.

The tool should not expose shell, filesystem, registry, browser, or network
tools. Its authority should remain limited to capture and input.

If HTTP transport is ever added, require authentication for non-loopback binds
and provide explicit tool filtering. This is not required for v1.

## Testing

Unit tests:

- Backend registry and priority order.
- Explicit and automatic backend selection.
- Backend fallback on unavailable dependency and raised exception.
- Single-monitor capture rect resolution.
- Cross-monitor region fallback behavior.
- Negative virtual-screen origin coordinate math.
- `capture`, `client`, `screen`, and `normalized` transforms.
- Frame cache hit, miss, and expiry.
- Key parser for scan-code and virtual-key modes.
- Structured error response creation.
- Compatibility handling for `framebuffer` alias.

Integration or smoke tests:

- Capture Notepad or another normal window as a baseline.
- Capture a borderless DirectX sample or simple game window.
- Click a known point using `scope="capture"` after a capture.
- Hold and release a key with `key_down` and `key_up`.
- Run daemon unavailable path and verify MCP server remains alive.

Manual validation:

- Target on secondary monitor with negative coordinates.
- Target on mixed-DPI monitors.
- Minimized target.
- Admin-launched target.
- Borderless full-screen target.
- Exclusive full-screen target, expecting fallback limitations.

## Rollout

1. Refactor existing window and coordinate helpers into `targets.py` and
   `geometry.py` without changing tool behavior.
2. Add `capture` package with `dxcam`, `mss`, and `pillow` backends plus tests.
3. Add `capture` MCP tool and frame metadata cache.
4. Update mouse tools to support `scope="capture"` with `frame_id`.
5. Add `key_down`, `key_up`, `tap_key`, and scan-code mappings.
6. Add target-based tool signatures while preserving pid-based compatibility.
7. Update README from Unity-specific language to general game I/O language.
8. Add manual smoke-test instructions for windowed and borderless games.

## Decisions For Implementation

- Keep the Python package name and current console scripts for v1. Add a
  `game-io-mcp` console-script alias only after the capture and input API is
  stable.
- Use a file-backed per-user frame cache for daemon-to-server image transfer.
  The daemon writes PNG files and frame metadata, and the MCP server reads the
  file to return image content to clients when appropriate.
- Defer `windows_graphics_capture` to a follow-up phase. Start v1 with
  `dxcam`, `mss`, and `pillow`; add a separate WGC spike once the coordinate
  and frame-cache contracts are tested.
