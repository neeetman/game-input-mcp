# Continuous / Multi-Input Control Design

Date: 2026-09-09
Status: Phases 1-2 implemented 2026-09-09 (sessions, set_keys, watchdog, key table, daemon timelines with QPC stamps and abort); Phases 3-5 pending

## Summary

GameInputMCP v1 is a "one tool call = one SendInput" facade. That is fine for
clicking menus, but UECapture's automation (L1 autoroute, L2 waypoint follower,
action-window sampler, per-game L0 tools) needs **continuous, overlapping,
time-accurate, fail-safe** control: hold W for seconds while pulsing yaw and a
jump, release everything on any error, and know exactly when each edge was
injected so it can be aligned with the recorded input stream.

This proposal moves input *state* and input *timing* into the elevated daemon:

- an **InputSession** per target that owns held keys/buttons, a focus policy
  and a watchdog lease;
- **atomic multi-key state changes** (one `SendInput` array per change);
- a **daemon-executed timeline** with a high-resolution scheduler, per-event
  QPC timestamps and an abort primitive;
- **relative mouse motion** (camera look) as a first-class primitive;
- a **complete scan-code key table** with a `MapVirtualKey` fallback;
- a **pure-ctypes client library** so UECapture stops carrying `sys.path`
  hacks and a pywin32 requirement.

Everything stays Windows-only, non-injected, `SendInput`-based and behind the
existing per-user named pipe. Old tools keep working during migration.

## Measured baseline (2026-09-09, this machine)

| Path | Median | p90 |
| --- | --- | --- |
| `list_targets` round trip | 1.9 ms | 5.0 ms |
| `get_target_info` round trip | 1.1 ms | 2.1 ms |
| `get_window_info` round trip | 1.0 ms | 2.6 ms |

Test suite: 72 passed. So per-call IPC cost is not the bottleneck; the
problems below are structural.

## How UECapture uses it today

All runtime consumers bypass the MCP tool facade and talk to `ipc.Client`
directly through `tools.l1_autoroute.GameInputDaemonInput`:

| Consumer | Calls | Pattern |
| --- | --- | --- |
| `l1_autoroute.py` / `l2_waypoint_route.py` | `key_down("w")`, `key_up`, `tap_key(left/right, hold_ms)`, `release_all` | hold W across probe cycles; turn = blocking tap whose `hold_ms` is derived from a per-title deg/s |
| `action_window_runner.py` | `key_down`/`key_up` per compiled `InputEvent` | Python `time.sleep` between events, one IPC call per edge, `release_all` in `finally` |
| `*_l0.py` (cloudheim, stellarblade, spirit_realm, bloodlines2) | same as above via a `Gate` wrapper | 5-second windows, body + camera overlap |
| `agent_game_nav.py` | `tap_key`, `capture`, `mouse_click(scope=capture)` | menu navigation, verified by UE reflection |

Camera yaw/pitch goes through arrow keys that UECapture's `CameraDrive` owns
(capture-armed only). There is no camera path when capture is not armed, so
`agent_game_nav.py` refuses any route that turns.

## Problems

P1. **Held state lives only in the client.** The daemon does not know what is
    held. If the Python runner is killed (`taskkill`, OOM, terminal closed)
    between `key_down("w")` and `finally: release_all()`, W stays down until
    the game loses focus. `release_all` is best-effort and client-side.

P2. **Timelines are executed client-side.** `action_window_runner` sleeps in
    Python and issues one IPC call per edge. Simultaneous edges (W+A at the
    same `start_s`) become two `SendInput` calls a few ms apart; every edge
    pays pipe connect + thread spawn + two `EnumWindows` + `SendInput`.
    Recorded `actual_s` is measured on the client, not where injection
    happened.

P3. **Focus is re-acquired on every press.** `activate=True` on `key_down`
    runs `resolve_target` + `focus_window` each call. When the game is *not*
    foreground, `focus_window_detailed` self-presses **Alt** via
    `keybd_event` and zeroes the foreground-lock timeout. Mid-window this
    leaks an Alt edge into the game and can toggle menus. Worse, `key_up`
    is sent with `activate=False`: if focus moved, the release lands in
    another window and the game keeps the key latched.

P4. **Key table is tiny.** Scan-code mode knows ~28 names (WASD, space,
    modifiers, enter/esc/tab, arrows, F1-F12). No digits, no other letters,
    no numpad, no mouse buttons as holdable state. Profiles already had to
    fall back to `mode="vk"` for `"1"`, and vk-only input (`wScan=0`) is
    ignored by titles that read raw scan codes.

P5. **No relative mouse motion.** Only absolute click/drag/scroll exist.
    Camera control therefore depends on `CameraDrive`, which is capture-owned
    and requires an armed capture. Menu-free navigation, aim, and
    "look around" for perception all need `MOUSEEVENTF_MOVE` deltas.

P6. **No injection timestamps.** Responses carry `sent` counts only. Nothing
    reports *when* an edge was injected, so alignment with the capture input
    stream and with frames is inferred from client-side clocks.

P7. **No abort.** A `tap_key` with a multi-second `hold_ms` blocks a worker
    thread and cannot be cancelled; a stuck route has no way to force-release
    from another process.

P8. **`hotkey` is not a chord.** It issues sequential `SendInput` calls; there
    is no "set this exact key state" operation.

P9. **Client packaging.** Every consumer inserts `--game-input-root` into
    `sys.path` and needs pywin32. There is no installable, dependency-free
    client.

## Goals

- Hold any set of keys/buttons for seconds with the daemon as the single
  source of truth for "what is currently pressed".
- Guarantee release on client death, lease expiry, focus loss, daemon
  shutdown and explicit abort.
- Execute an overlapping multi-key timeline with <=1 ms scheduling error,
  simultaneous edges in one `SendInput` array, and QPC timestamps per edge.
- Provide relative mouse motion with a constant counts/s profile.
- Support every physical key on a US layout by scan code, plus mouse buttons.
- Keep the MCP surface small; keep old tools working unchanged.
- Remove the pywin32 requirement from the client side.

## Non-goals

- Background (non-foreground) input, driver-level injection (Interception),
  anti-cheat evasion.
- Virtual gamepad (ViGEm). Worth a later phase for analog training data, not
  part of this design.
- Replacing `CameraDrive`. Its closed-loop constant deg/s is the right tool for
  calibrated capture passes; daemon-side mouse look is for navigation, aim and
  perception when capture is not armed.

## Design

### 1. InputSession (daemon-owned state + watchdog)

```text
session_open(target, lease_ms=2000, focus="acquire_once", max_hold_ms=30000)
  -> {session_id, hwnd, foreground: true}
session_heartbeat(session_id, lease_ms?)      # extends lease
session_state(session_id)                     # {held_keys, held_buttons, foreground, age_ms}
session_close(session_id)                     # releases everything
```

- One session per target hwnd. Opening a second session on the same hwnd
  fails with `SESSION_EXISTS` unless `takeover=true`, which releases the old
  session's held state first.
- Daemon keeps `held: dict[key -> (KeyStroke, qpc_down)]` and
  `buttons: set`.
- **Watchdog thread** (one per daemon): every 50 ms checks all sessions;
  if `now - last_heartbeat > lease_ms` or any key held longer than
  `max_hold_ms`, it injects the corresponding up edges and marks the session
  `expired` with a reason. The client sees `SESSION_EXPIRED` on its next call.
- **Focus policy**
  - `acquire_once` (default): focus at `session_open` using the existing
    `focus_window_detailed`; afterwards **never re-focus**. Every send first
    checks `GetForegroundWindow() == hwnd`. On mismatch: release all held
    state (up edges still go to the foreground window, which is harmless),
    return `FOCUS_LOST`, and mark the session paused.
  - `acquire_each`: v1 behaviour, kept for menu-style one-shot use.
  - `none`: never focus, only check.
- Daemon shutdown (`SIGTERM`, exception in `serve_forever`) runs
  `release_all_sessions()`.

### 2. Atomic state changes

```text
set_keys(session_id, down=["w","a"], up=["s"], buttons_down=["right"], buttons_up=[])
  -> {sent, qpc_ns, held_keys, held_buttons}
```

- Builds **one** `INPUT[]`: ups first, then downs, keyboard and mouse
  interleaved as given. One `SendInput` call, so the game sees the edges in
  the same message-queue burst.
- Idempotent: keys already in the requested state are skipped (reported in
  `skipped`).
- Replaces the client-side `_held` bookkeeping in `GameInputDaemonInput`.
- `hotkey` is re-implemented as `set_keys(down=keys)` + `set_keys(up=reversed)`.

### 3. Daemon-executed timeline

```text
run_timeline(session_id, events, total_ms, on_focus_lost="abort")
  events: [
    {"t_ms": 500,  "op": "down", "key": "w"},
    {"t_ms": 800,  "op": "down", "key": "left"},
    {"t_ms": 1000, "op": "down", "key": "space"},
    {"t_ms": 1120, "op": "up",   "key": "space"},
    {"t_ms": 1800, "op": "up",   "key": "left"},
    {"t_ms": 2000, "op": "up",   "key": "w"},
    {"t_ms": 2500, "op": "look", "dx": 800, "dy": 0, "duration_ms": 1000, "rate_hz": 250},
    {"t_ms": 4000, "op": "button_down", "button": "left"},
    {"t_ms": 4100, "op": "button_up",   "button": "left"},
    {"t_ms": 4500, "op": "wheel", "delta": -120}
  ]
  -> {timeline_id, started_qpc_ns, events: [{index, t_ms, actual_ms, qpc_ns, sent}], completed: true, released_on_exit: [...]}
abort_timeline(session_id) -> {aborted: true, released: [...]}
```

- Runs on a dedicated **scheduler thread** with `timeBeginPeriod(1)`,
  `SetThreadPriority(TIME_CRITICAL)`, sleep until 2 ms before the deadline
  then spin on `QueryPerformanceCounter`. Target: |actual - scheduled| <= 1 ms.
- Events with equal `t_ms` are coalesced into one `SendInput` array.
- `look` expands into `duration_ms * rate_hz / 1000` relative-move
  sub-events with integer accumulation so the sum equals `dx, dy` exactly.
- Validation before the first edge (fail closed): every key resolvable,
  every `down` has a matching `up` inside `total_ms` or the caller passes
  `allow_dangling=true`, `total_ms <= max_hold_ms`, no opposed keys from the
  same opposition group overlapping if the caller supplies `oppositions`.
  (The semantic conflict checks in `action_window_runner.compile_input_events`
  stay in UECapture; the daemon only enforces physical safety.)
- On completion: any key still down is released and listed in
  `released_on_exit`. On focus loss: release, stop, return `FOCUS_LOST` with
  the partial event log. On `abort_timeline`: same, with `ABORTED`.
- The request blocks until completion (bounded by `total_ms`); a session can
  have only one running timeline. Blocking keeps the wire protocol as-is
  (one request, one response).

### 4. Relative mouse motion

```text
mouse_move_relative(session_id, dx, dy, duration_ms=0, rate_hz=250)
```

- `duration_ms=0`: one `MOUSEEVENTF_MOVE` event.
- Otherwise a constant counts/s profile at `rate_hz`, injected from the
  scheduler thread. Same expansion as the `look` timeline op.
- Injected events arrive at the game with `RAWINPUT.header.hDevice == NULL`,
  exactly like `CameraDrive`, so UECapture's `InputCapture` records them as
  part of the action stream and `camera_drive_block_mouse` lets them pass.
- Reports `qpc_ns` of first and last sub-event so the caller can compute the
  observed deg/count gain from UE `ControlRotation` feedback.

### 5. Key table

- Replace `input/keys.py` tables with the full US-layout scan-code set 1
  table: letters, digits, punctuation, numpad (extended where required),
  `lshift/rshift`, `lctrl/rctrl`, `lalt/ralt`, `capslock`, `backspace`,
  `insert/delete/home/end/pageup/pagedown`, `printscreen`, `pause`.
- Fallback: for any name that resolves to a VK (including single characters
  via `VkKeyScanW`), derive the scan code with
  `MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX)`; mark extended when the high byte
  is `0xE0`.
- Mouse buttons `left/right/middle/x1/x2` become holdable state alongside keys.
- `mode="vk"` remains available but is no longer needed for digits/letters.

### 6. Timestamps and audit log

- Every input response carries `qpc_ns` (per edge for timelines).
- Optional per-session JSONL log at
  `%LOCALAPPDATA%\game-input-mcp\sessions\<session_id>.jsonl` with one row per
  injected `SendInput` batch: `qpc_ns`, ops, `sent`, `foreground_ok`.
  Enabled by `session_open(log=true)`. This is the join key against UECapture's
  recorded input stream when validating that "the recorded action stream
  equals what was injected".

### 7. Client library

- New module `game_input_mcp/client.py`: pure `ctypes` named-pipe client
  (CreateFileW / ReadFile / WriteFile), same framing as `ipc.py`, no pywin32.
- `InputSession` context manager:

```python
from game_input_mcp.client import Client

with Client().session({"pid": pid}, lease_ms=2000) as s:   # heartbeat thread inside
    s.set_keys(down=["w"])
    s.run_timeline(events, total_ms=5000)
    s.look(dx=600, dy=0, duration_ms=800)
# __exit__ -> session_close -> everything released even on exception
```

- Ship as a wheel (`pip install game-input-mcp[client]` has zero deps) so
  UECapture drops `--game-input-root` / `GAME_INPUT_MCP_ROOT`.
- `tools.l1_autoroute.GameInputDaemonInput` becomes a thin adapter over
  `InputSession` keeping its `key_down/key_up/tap/release_all` protocol.

### 8. MCP tool surface (additions)

`input_session_open`, `input_session_close`, `input_session_state`,
`set_keys`, `run_timeline`, `abort_timeline`, `mouse_move_relative`.
Existing tools are unchanged and internally create a one-shot session with
`focus="acquire_each"`.

## Wire protocol changes

None to framing. Long-running requests (`run_timeline`, `mouse_move_relative`
with duration) hold their pipe connection open until done; `Server` already
spawns one thread per connection, so heartbeats and `abort_timeline` from a
second connection proceed concurrently. Client read timeout for these calls is
`total_ms + 2000`.

## UECapture integration

| Consumer | Change |
| --- | --- |
| `action_window_runner.py` | `compile_input_events` output maps 1:1 onto timeline events; `execute_event_timeline` becomes one `run_timeline` call; `actual_s` comes from daemon `qpc_ns`. Semantic conflict checks stay. |
| `l1_autoroute.py` / `l2_waypoint_route.py` | open one session per run; cruise = `set_keys(down=["w"])`; turn = `run_timeline([down, up])` or `mouse_move_relative` when capture is not armed; `finally` -> `session_close`. Focus loss now surfaces as `FOCUS_LOST` instead of a silently latched key. |
| `*_l0.py` | `Gate` wraps `InputSession`; unchanged semantics. |
| `agent_game_nav.py` | unchanged (one-shot tools). Turn routes can use `mouse_move_relative` when `turn_requires_capture` is false for a title. |

## Phases

1. **Session + state + watchdog + key table** (compat-preserving). **Done 2026-09-09**: `input/state.py`, daemon `session_*`/`set_keys` handlers, `win32.send_edges`, MCP tools, 143 tests; live-verified on a Tk harness (atomic batch, lease expiry release, FOCUS_LOST release) with the daemon task restarted on the new code.
   `models.py` gains `SessionRecord`; `daemon.py` gains `SessionRegistry` and
   the watchdog; `set_keys`; full key table with `MapVirtualKey` fallback.
   Tests: registry lease expiry, release-on-expiry, focus-lost release,
   atomic batch ordering (ups before downs), key table coverage.
2. **Timeline + timestamps + abort.** Scheduler thread, coalescing, `look`
   expansion, validation, partial logs. **Done 2026-09-09**: `input/timeline.py`,
   daemon `run_timeline`/`abort_timeline`, `win32.build_inputs` (move/wheel),
   session `busy_until` lease exemption; 171 tests. Live on Daemon X Machina
   (third person, in-game): 202 batches over 2 s, timing error max 0.74 ms /
   mean 0.26 ms, yaw +92 deg from a 600-count look, W+jump moved the pawn,
   abort of a 5 s hold landed in 1.4 ms and released W. The standalone
   `mouse_move_relative` tool (Phase 3) is now a thin wrapper over the `look` op. Tests: pure-logic expansion and
   coalescing; a fake `SendInput` records QPC and asserts <=1 ms error on the
   real scheduler.
3. **Relative mouse.** `mouse_move_relative`, profile expansion, `hDevice`
   note verified against UECapture `InputCapture` on one title.
4. **Client library + UECapture adapters.** ctypes client, `InputSession`,
   wheel packaging, migrate `GameInputDaemonInput`, `action_window_runner`,
   L0 tools. Remove `--game-input-root`.
5. (Optional) **Virtual gamepad** via ViGEmBus for analog sticks.

Each phase is TDD against the existing pytest suite (72 tests today) and ends
with a smoke run against Notepad plus one UE title with capture attached.

## Risks

- Titles that poll `GetAsyncKeyState` see `SendInput` state fine; titles on
  pure DirectInput may not. Unchanged from v1; documented, not solved here.
- `timeBeginPeriod(1)` is process-global for the daemon; acceptable for a
  dedicated input daemon.
- A blocked `run_timeline` request holds a worker thread for up to
  `max_hold_ms`; the server is multi-instance so this does not starve other
  calls.
- Watchdog release goes to whatever window is foreground. This is by design
  (a stray key-up is harmless; a latched key-down is not).
