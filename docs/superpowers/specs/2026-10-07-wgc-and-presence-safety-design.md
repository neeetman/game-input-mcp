# WGC Capture and Presence/Foreground Safety Design

Date: 2026-10-07
Status: Phases 0-5 implemented 2026-10-07 (see "Implementation notes" at the end
for what was measured and where the code deviates from the design below).

## Summary

`D:\GitProjects\universal-modder` (`um win`) is the other Windows game
automation stack this project is compared against. Reading it end to end
(`um/win.py`, `um/ps1/*`, the `game-automation` skill, two knowledge notes and
the Terraria `AgentBridge.cs`) shows that most of its input and window code is
a weaker version of what `game_input_mcp` already has (sessions, atomic
batches, QPC timelines, per-monitor DPI, hwnd-exact foreground checks). It
does have three things this project lacks, and two of them are about
**safety**, not features:

1. **Window-targeted capture** through Windows.Graphics.Capture (WGC). Every
   `game_input_mcp` capture backend grabs a *screen rectangle*, so a window
   that is covered, half off-screen or across monitors silently returns other
   pixels as "the game".
2. **User-presence awareness.** `um` documents a real incident (an agent
   focused GTA V while the human was typing; the keystrokes hit GTA's landing
   page and GTA flagged the session). `game_input_mcp` has no equivalent
   check, and its `focus_window_detailed` steals foreground by design.
3. **A foreground guard on every injection.** `um`'s `Send()` refuses unless
   the game is foreground. Here only *sessions* have that guard; the one-shot
   tools do not, and `activate=False` sends keys to whatever window is in front.

This spec adds those three, plus a handful of cheap capture diagnostics `um`
learned the hard way (Auto HDR colour shift, frozen-WGC detection). It keeps
the MCP surface unchanged: **no new tools in Phases 1-4**, only optional
parameters, extra response fields and new error codes. Everything `um` does
that needs a shell, the registry, process control or engine injection stays
out of the elevated daemon; section "Separate components" says where each
piece should live instead.

## Context

### What was read

| Source | Used for |
| --- | --- |
| `README.md`, `2026-07-02-game-io-generalization-design.md`, `2026-09-09-continuous-control-design.md` | architecture, boundaries, doc format |
| `game_input_mcp/{win32,daemon,targets,models,frames,geometry,server,install,ipc,client}.py`, `capture/*`, `input/{state,timeline}.py` | "game-input today" column |
| `um/win.py`, `um/ps1/WinDrive.ps1`, `um/ps1/ProcLoopback.ps1` | "um win" column |
| `skills/game-automation/SKILL.md`, `knowledge/techniques/{driving-real-games-safely,oracles-how-agents-know-a-mod-works}.md`, `examples/terraria-tmodloader/reference/AgentBridge.cs` | intent, incidents, gotchas |

### Verified by reading code or running a command

- `um` captures with `ffmpeg ... gfxcapture` (`win.py:205-218`, `274-291`). The
  local ffmpeg (8.1.1, gyan full build) lists the filter with `hwnd`,
  `window_exe`, `capture_border`, `display_border`, `crop_*`, `output_fmt`
  (`8bit`, `10bit`, `rgbaf16`).
- `um`'s `idle` is `GetLastInputInfo` (`WinDrive.ps1:227-232`) and is
  **advisory only**: nothing in `Drive` consults it (`win.py:376-429`), and
  `Drive.cmd` re-focuses the game and retries on a foreground error
  (`win.py:389-392`) with no idle check.
- `um`'s crash-reporter cleanup is documentation only: no code under `um/`
  mentions `BsSndRpt`, `CrashReportClient` or `UnityCrashHandler`.
- `game_input_mcp` has no code for: user idle, HDR, process name/exe, monitor
  index, `CAPTURE_BLACK_FRAME`, `TARGET_NOT_FOREGROUND`, `TARGET_AMBIGUOUS`,
  or the `GAME_IO_*` environment variables (all listed in the 2026-07-02 spec
  but never implemented; confirmed by grep).
- `ipc.Client.call` and `client.Client.call` block on `ReadFile` with no read
  timeout (`ipc.py:121-141`; no timeout handling in `client.py`), so a hung
  daemon handler hangs the caller forever.
- The daemon runs `python -m game_input_mcp.daemon` as a `/RL HIGHEST` task
  from the installing user's interpreter (`install.py:74-83`).

### Not verified (hypotheses, resolved by Phase 0 spikes)

| ID | Hypothesis | Why it matters |
| --- | --- | --- |
| H1 | `SendInput` updates `GetLastInputInfo.dwTime` | decides whether naive idle is usable; the design below is correct either way |
| H2 | WGC delivers an initial frame for a static window (`um` says gfxcapture only emits on redraw, `win.py:369`) | cold-capture latency and timeout design |
| H3 | `IsBorderRequired=false` works for an unpackaged elevated Python process on Win11 | visible yellow border during capture |
| H4 | WGC frame size equals DWM extended frame bounds, so client crop = client rect minus that origin | coordinate mapping |
| H5 | `Direct3D11CaptureFrame.SystemRelativeTime` is QPC-derived | joining frames to `qpc_ns` of edges |
| H6 | A maintained WGC wrapper has a wheel for CPython 3.14 (local interpreter is 3.14.3) | dependency choice |
| H7 | `AutoHDREnable` odd = on (this is `um`'s own heuristic, `win.py:254`), and `DisplayConfigGetDeviceInfo(GET_ADVANCED_COLOR_INFO)` reports display HDR | HDR warning precision |
| H8 | HDR colour shift affects dxcam/mss/pillow too, not only WGC | warn on all backends |
| H9 | WGC behaviour on exclusive fullscreen | documented limitation vs fallback |

## Gap Analysis

Verdicts: **adopt** = take the behaviour into game-input; **adapt** = take the
idea with a different mechanism or scope; **separate** = build, if at all, as
an optional component outside the elevated daemon; **reject**; **have** =
game-input already does this as well or better (no action).

| # | Capability | game-input today | um win | Verdict | Reason |
| --- | --- | --- | --- | --- | --- |
| 1 | Window-targeted capture (WGC) | none. All backends take a screen rect: `capture/base.py:64-79`, `dxcam_backend.py:49-59`, `mss_backend.py:22-33`, `pillow_backend.py:14-18`; the target hwnd never reaches a backend (`service.py:74`). WGC deferred: `README.md:74`, 07-02 spec `:214-216` | `ffmpeg gfxcapture` per call, `win.py:205-218,274-291` | adopt idea, adapt mechanism | A covered or off-screen window returns the occluder's pixels as the game. But ffmpeg-per-call is wrong for an elevated daemon (it would exec a user-writable binary, `install.py:74`) and cold-starts a process per frame. Use an in-process WGC library as an optional extra. |
| 2 | Frozen-WGC detection | none | doc only, `oracles...md:55-68` (WGC replays its last frame under ReShade/independent flip); `shot` has no check | adopt | Cheap (hash + cross-check against a screen backend) and prevents a confident answer about a stale frame. |
| 3 | Occlusion / black-frame detection | `CAPTURE_BLACK_FRAME`, `TARGET_NOT_FOREGROUND` listed (07-02 `:233,321,324`) but emitted nowhere | n/a (WGC sidesteps it) | adopt | Needed for the non-WGC backends and as the trigger for `auto` to escalate to WGC. |
| 4 | Auto HDR warning | none | `win.py:221-271`: stderr only (invisible to an MCP agent), global flag only when no `--exe`, never checks that the display is HDR | adapt | Return it in `warnings`, add the display-HDR API check to cut false positives, apply to every backend (H8). |
| 5 | Cheap scaled copy | `max_width` resize, one image (`service.py:33-38`, `server.py:59`) | full + `_small.png`, `win.py:285-290`; clicks need x3 mental math, `SKILL.md:46-47` | adapt (optional) | Add `thumb_width`: one capture, two files, same frame. Clicks go through `frame_id` / `normalized`, so no manual scaling. |
| 6 | Frame timestamp joinable to input edges | `created_at = time.time()` (`frames.py:46`) while edges use QPC (`win32.py:365-368`) | recorder uses QPC hns (`ProcLoopback.ps1:135`) | adapt | Add `capture_qpc_ns` so frames and `qpc_ns` of edges share a clock (09-09 spec P6 motivation). |
| 7 | User presence | none | `idle`, `WinDrive.ps1:227-232`, advisory only | adopt idea, adapt mechanism | Must live in the daemon, be enforced, and be **injection-aware**: our own `SendInput` may reset `GetLastInputInfo` (H1), so a naive check would refuse the agent's second call. |
| 8 | Foreground guard, sessions | present: `daemon.py:479-508`; timeline `daemon.py:673-674`, `timeline.py:253-255` | `Safe()` `WinDrive.ps1:133-141` | have | hwnd-exact, stricter than `um`. Keep. |
| 9 | Foreground guard, one-shot tools | none. `mouse_click/drag/scroll` ignore the focus result (`daemon.py:188-190,224-226,267-269`); `activate=False` skips every check (`daemon.py:167-168`); key tools inject into whatever is foreground | refuses unless `Safe()` (`WinDrive.ps1:143-147`) | adopt | Bug-class gap: SendInput never reaches a background window, so `activate=False` can only leak. |
| 10 | "Nothing foreground and cursor over game" exception | `FOCUS_LOST` whenever foreground != hwnd, including 0 (`daemon.py:492-504`) | `WinDrive.ps1:133-141` | adapt | Only for absolute-mouse one-shots, judged on the destination point, reported as `guard.exception`. Never for keyboard, relative mouse or sessions. |
| 11 | Foreground match granularity | exact hwnd (`daemon.py:493`) | process name (`WinDrive.ps1:82-86`), first `MainWindowHandle` (`:68-73`) | have | `um` mis-handles two same-named processes and owned windows. |
| 12 | Focus acquisition | Alt self-press, zero lock timeout, AttachThreadInput (`win32.py:289-329`) | Alt tap, TOPMOST, then a real click at client y=4 (`WinDrive.ps1:108-126`) | reject um's fallback; gate ours | `um`'s fallback click lands inside the game's client area and can trigger in-game UI. Ours stays but is gated by presence (Design 2). |
| 13 | Auto re-focus on guard failure | n/a | `win.py:389-392` | reject | Stealing focus without a presence check is the incident path. |
| 14 | Window rect / title / client | `TargetInfo` (`models.py:66-90`, `targets.py:9-25`) | `WinDrive.ps1:167-176` | have | |
| 15 | Set client size / untop | none (read-only) | `size` `WinDrive.ps1:177-184`, `untop` `:168` | adapt (deferred, opt-in) / reject untop | Resolution drift is mostly solved by `frame_id`/`normalized`; resizing mutates the target, so gate it by config. We never set TOPMOST, so `untop` is moot. |
| 16 | Resolve by exe/title | pid/hwnd only (`models.py:33-54`); no `exe` in `TargetInfo` | exe regex (`win.py:218`) | adapt (small) | Add `exe` and `monitor` to target metadata first (also needed for HDR); resolver by exe later, returning the spec'd `TARGET_AMBIGUOUS`. |
| 17 | DPI correctness | per-monitor v2 at import (`win32.py:20-29`) | DPI-unaware: clicks logical, screenshots physical, ~44 px drift (`driving-real-games-safely.md:53-62`) | have | We are ahead; add a regression test only. |
| 18 | Relative mouse, wheel, scan codes | full, atomic, QPC-stamped (`win32.py:625-657`, `input/keys.py`) | `rel`, `wheel`, `scanmode` (`WinDrive.ps1:196,226,216`) | have | |
| 19 | Non-ASCII titles / locale | UTF-8 framing (`ipc.py:94-98`, `client.py:196-199`) | needs `PYTHONUTF8=1` workaround (`driving-real-games-safely.md:63-69`) | have | Add a CJK-title round-trip test. |
| 20 | Recording: window video + game-only audio | none | `Recorder` `win.py:294-373`, `ProcLoopback.ps1` | separate | Non-goal (07-02 `:65-66`); downloads and execs ffmpeg (`win.py:95-125`). Join to game-input via the QPC clock (row 6). |
| 21 | Encoder auto-pick | n/a | `win.py:151-158` | separate | Belongs with 20. |
| 22 | Launch (Steam / exe) | none | `win.py:193-199`: `cmd /c start`, args concatenated into a `steam://` URL unescaped | separate | Shell is a non-goal; a `steam-mcp` already exists in this environment. |
| 23 | List processes / kill by exact PID | `list_targets` only | `win.py:171-190` | separate | An elevated daemon killing processes is a large authority jump. Keep "exact PID only" as documented guidance. |
| 24 | Crash-reporter cleanup | none | docs only (`SKILL.md:85-91`) | separate (doc) | No code exists to adopt. |
| 25 | Registry get/set | none | `win.py:435-447` (backs up before set) | reject | 07-02 non-goal. Sole carve-out: a fixed, read-only HDR probe (Design 4), never exposed as a tool. |
| 26 | In-game JSON bridge | none | `AgentBridge.cs` | separate (convention) | Engine injection is a non-goal (07-02 `:63`). It is complementary: the bridge sets controls directly and works unfocused (`AgentBridge.cs:23-24,328-329`), so it avoids both SendInput and the human conflict. |
| 27 | Log oracles | none | `oracles...md` table | separate (doc) | Reading arbitrary log files is a filesystem capability. |

## Problems

P1. **Capture returns the wrong pixels without saying so.** A covered,
    partially off-screen or cross-monitor window yields whatever is on screen
    in that rectangle. `capture` neither checks occlusion nor reports it, and
    dxcam silently declines cross-monitor rects (`dxcam_backend.py:23-24`) so
    the fallback chain hides which backend answered.

P2. **No black-frame or stale-frame detection**, although the 07-02 spec
    promised black-frame handling (`:233`). A frozen capture path (WGC under
    ReShade) would answer confidently about an old frame.

P3. **Focus is stolen from a human who is typing.** `focus_window_detailed`
    presses Alt and zeroes the lock timeout (`win32.py:289-304`), and the
    one-shot tools call it by default. Nothing asks whether a human is active.

P4. **One-shot injection has no foreground guard.** `mouse_click`, `mouse_drag`
    and `scroll` ignore a failed focus (`daemon.py:188-190`, `224-226`,
    `267-269`); `activate=False` skips every check; key tools send to whatever
    is in front.

P5. **A frame's geometry is trusted forever.** `_frame_geometry`
    (`daemon.py:121-134`) maps clicks with the rect cached at capture time and
    never compares it with the window's current rect, so a moved window means
    a mis-click. WGC makes this likelier (the image no longer proves where the
    window is on screen).

P6. **HDR colour shift is invisible to agents** (and `um`'s warning, which only
    reaches stderr, would not reach an MCP client either).

P7. **Frames and edges are on different clocks** (`time.time()` vs QPC), so
    "which frame followed this key edge" is guesswork.

P8. **Target metadata is thin.** No `exe`, no monitor index (both promised by
    the 07-02 spec's `list_targets`), which blocks per-exe HDR lookup and makes
    multi-instance disambiguation depend on titles.

P9. **No daemon-side deadlines for capture.** The client has no read timeout
    (`ipc.py:121-141`), and WGC can legitimately stall on a frozen window, so
    every capture path needs its own bounded wait.

## Goals

- Return pixels of the target window even when it is covered, and say so
  whenever the pixels may not be the target's.
- Never inject into a window the agent was not told to drive; never take
  foreground from a human who is active (configurable, default per Decision D1).
- Treat real user input during an agent session as an emergency brake when the
  strict policy is on.
- Give agents the information to avoid trouble *before* acting: presence,
  HDR, occlusion, exe, monitor in the existing pre-flight calls.
- Keep the MCP tool list unchanged for Phases 1-4; keep every change additive
  or fail-closed with a documented escape hatch.
- Keep the elevated daemon's authority at capture and input.

## Non-goals

- Recording, audio capture, encoders (separate component).
- Launching, killing or listing processes, crash-reporter cleanup (separate).
- Registry or filesystem tools, log reading.
- Engine bridges or injection; anti-cheat evasion (unchanged from 07-02).
- Detecting gamepad activity by the human (neither `GetLastInputInfo` nor
  Raw Input mouse/keyboard sees XInput).
- Guaranteed capture of exclusive-fullscreen titles (H9).
- Defending against a *malicious* agent. The guards stop mistakes and
  accidental collisions with the human; an agent with filesystem access can
  edit the daemon config (see Security).
- Executing ffmpeg, or any user-writable binary, from the elevated daemon.

## Design

### 1. Injection chokepoint and foreground guard

All injection in the daemon goes through one `Injector.send(edges, kind, ...)`
in a new `game_input_mcp/guard.py`. Today `win32.send_edges` /
`send_mouse_click` / `send_keys` are called from at least ten places
(`daemon.py:205,249,285,295,303,317,331,354-355,430,607,677`). A single
chokepoint is also where presence (Design 2) and its lock live.

```text
guard.check(target_hwnd, kind, point=None) -> GuardResult
  kind: "keyboard" | "mouse_rel" | "mouse_abs"
  GuardResult: {ok, exception, foreground: {hwnd, pid, exe, title}}
```

| Foreground window | `keyboard`, `mouse_rel` | `mouse_abs` |
| --- | --- | --- |
| == target hwnd | ok | ok |
| none (0) | refuse | ok iff the root window at the destination point is the target's root; reported as `exception: "no_foreground_cursor_over_target"` |
| anything else | refuse | refuse |

The exception is `um`'s `Safe()` (`WinDrive.ps1:133-141`) restricted to the one
case where it is meaningful: with no foreground window, keystrokes go nowhere,
but a click lands on whatever is under the pointer, so "pointer over the
target" is the right test. We judge the *destination* point rather than the
current cursor because we know where the click will go.

One-shot handler order becomes:

```text
resolve target
-> presence gate (only if the call would change the foreground)   [Design 2]
-> focus if activate=True; a failed focus returns FOCUS_FAILED     [fixes daemon.py:188-190 etc.]
-> guard.check (always, including activate=False)                  [new]
-> frame geometry check (frame-based scopes only)                  [new]
-> map coordinates -> Injector.send
```

Errors:

| Code | When | Retryable | Details |
| --- | --- | --- | --- |
| `TARGET_NOT_FOREGROUND` | one-shot guard refused | yes | `foreground{hwnd,pid,exe,title}`, `target_hwnd`, `hint` |
| `FOCUS_FAILED` | `activate=True` and focus did not succeed (now also for mouse tools) | yes | existing |
| `FRAME_GEOMETRY_CHANGED` | `capture`/`normalized` scope and the target's current client rect or DPI differs from the frame's | yes (recapture) | `frame_client_rect`, `current_client_rect`, `frame_dpi`, `current_dpi` |
| `FOCUS_LOST` | sessions and timelines, unchanged | yes | existing |

`activate=False` keeps its meaning ("do not try to focus") but no longer means
"send anyway". Sessions keep `FOCUS_LOST` semantics, including auto-resume
when the target regains foreground (`daemon.py:505-507`).

Watchdog and release paths (`_release_session`, `daemon.py:420-441`) call the
injector with `bypass_gate=True`: a stray key-up is harmless, a latched
key-down is not.

### 2. User presence

#### Signal

`GetLastInputInfo` (session-wide, so it works from the elevated daemon on the
user's desktop) plus `GetTickCount`, as `um` does, with the 32-bit wrap handled
by masked subtraction. The problem `um` never had to solve is that the daemon
itself injects input. `PresenceMonitor` therefore attributes each change of
`dwTime` to "us" or "someone else":

```text
class PresenceMonitor:
    own_tick:   int | None   # dwTime read immediately after our last injection
    human_tick: int | None   # newest dwTime that was not ours

    probe():                          # before every injection and every gate check
        cur = last_input_tick()
        if cur != own_tick and cur != human_tick:
            human_tick = cur
        return None if human_tick is None else (now_tick() - human_tick) & 0xFFFFFFFF

    note_injection():                 # right after SendInput returns
        own_tick = last_input_tick()
```

`probe -> SendInput -> note_injection` runs under one lock (the `Injector`'s),
otherwise thread B's injection could be misread as human input by thread A. The
critical section is a few microseconds, so the 09-09 timing budget (error
<= 1 ms) is unaffected; Phase 2 re-runs the real-clock timing tests to prove it.

Why this works whether or not H1 holds: if `SendInput` bumps `dwTime`, our
post-injection read records it as ours and any later difference is human
input seen at the next probe; if it does not, `own_tick` simply keeps the old
value and any change is still human. Human input that our own injection would
have overwritten is caught because every injection is preceded by a probe,
including each batch of a running timeline (the runner already evaluates a
per-batch predicate, `timeline.py:253-255`). Blind spots, documented:
two events inside one tick (<= ~16 ms), gamepad input, and any *other*
injector (macro tools, remote desktop) which counts as "not us".

A Raw Input listener (`WM_INPUT`, `RAWINPUTHEADER.hDevice != NULL` for
hardware, `NULL` for injected) is the more precise upgrade if S1 shows
`GetLastInputInfo` is too noisy (risk R11). A low-level hook is rejected: it
sits in every application's input path, and a Python callback stalled by the
GIL (the timeline spin loop holds it) would add system-wide input latency.

#### Reading

Returned inside `get_target_info`, session views and, when relevant, as a
warning on input results:

```json
"presence": {
  "state": "present",
  "user_idle_ms": 4200,
  "threshold_ms": 30000,
  "policy": "focus",
  "source": "GetLastInputInfo"
}
```

`state` is `present` (idle < threshold), `away`, or `unknown` (signal
unavailable, treated as `away` for gating but flagged). `um` suggests 60 s as an
"ask first" rule of thumb; as a hard gate that is sticky, so the default
threshold is 30 s (Decision D1).

#### Policy

`presence` is a daemon-level setting (config file or environment, see
Configuration). **There is deliberately no per-call parameter to relax it**: an
agent can always read `presence` and choose to wait, but only the human who
owns the daemon config can grant "act while I am here".

| Policy | A: changes foreground (`focus_target`, `focus_window`, `activate=True` that needs a steal, `session_open` with `acquire_*`, `acquire_each` re-focus) | B: injection while already foreground | C: running session / timeline |
| --- | --- | --- | --- |
| `off` | allow | allow | allow |
| `warn` | allow + warning | allow + warning | allow + warning |
| `focus` | **refuse `USER_PRESENT`** | allow + warning | allow + warning |
| `strict` | refuse `USER_PRESENT` | refuse `USER_PRESENT` | **pause session `USER_TOOK_OVER`** |

`focus` blocks exactly the documented incident (stealing the foreground from a
typing human). `strict` makes the agent act only when the human is away and
turns any real input during a session into a brake: held keys are released, the
timeline stops with the partial batch log and `pending_indices` (same shape as
`FOCUS_LOST`), the session becomes `paused` with `reason: "user_input"`, and it
auto-resumes once `user_idle_ms >= threshold_ms`, the way `FOCUS_LOST` pauses
already auto-resume (`daemon.py:505-507`). Residual risk, unavoidable: human
input between two batches reaches the game, because the agent holds the
foreground.

New errors: `USER_PRESENT` (retryable, `details{user_idle_ms, threshold_ms,
policy, retry_after_ms}`), `USER_TOOK_OVER` (retryable, same plus the partial
timeline log when applicable). `TimelineRunner.run` gains a `gate` callable
returning a stop reason or `None`; `foreground_ok` stays for compatibility and
is wrapped into it. `RunResult.stopped_reason` gains `"user_input"`.

### 3. WGC capture backend

#### Shape

`CaptureBackend` is extended additively so existing backends and tests are
untouched:

```python
class CaptureBackend:
    needs_window = False                                       # new, default False
    def is_available_for(self, target, rect): return self.is_available(rect)   # new hook
    def capture_for(self, target, rect):      return self.capture(rect)        # new hook

def capture_region(rect, *, backend="auto", target=None, timeout_ms=None)       # new kwargs
```

`capture_target` passes the resolved `TargetInfo` (`service.py:50`) through.
Registry name `wgc` (the 07-02 environment variable named it
`windows_graphics_capture`; both spellings are accepted).

#### Backend behaviour

- One `WgcSession` per hwnd: capture item from the hwnd, free-threaded frame
  pool, latest-frame slot guarded by a condition variable. Sessions are warm
  for `wgc_idle_ttl_s` (default 5) after the last request and then torn down,
  so repeated `capture` calls are cheap and the capture indicator/border
  appears only during activity rather than flashing per call. At most 4 warm
  sessions.
- Frame size follows the window's visual bounds. The crop to the client area
  is computed from `DWMWA_EXTENDED_FRAME_BOUNDS` and `client_rect_screen`
  (H4), so `frame.geometry.capture_rect_screen` stays the client rect in
  screen coordinates and the existing `FrameGeometry` mapping is unchanged.
  The window's screen rect is re-read right after the frame is dequeued and
  stored with the frame; a window that moved *during* capture is reported
  (`warnings: WINDOW_MOVED_DURING_CAPTURE`).
- Cursor capture off. Border off when the OS allows (H3); otherwise
  documented as an OS-drawn indicator, not part of the pixels.
- `is_available_for` is false when: the library import fails, the target is
  minimized (already `TARGET_MINIMIZED`, `service.py:53-59`), or a `region`
  falls outside the client rect (WGC is window-only).
- Waiting: `capture_for` waits for a frame newer than the request time up to
  `capture_timeout_ms` (default 1500). If none arrives it returns the latest
  frame it has with `warnings: WGC_NO_NEW_FRAME` and `frame_age_ms` (a static
  window legitimately produces no new frames); if it has no frame at all it
  raises `CAPTURE_TIMEOUT`, and `auto` falls through to the screen backends.
  Stopping a session never blocks on the game: teardown is off the request
  thread (`um` learned that a frozen window stalls gfxcapture, `win.py:342-343`).
- Elevation: the daemon is high-integrity, so capturing an elevated game is not
  blocked by integrity levels. This is a reason to keep WGC in the daemon.

#### Library choice (Decision D3, settled by spike S2)

| Option | For | Against |
| --- | --- | --- |
| `winrt-*` (pywinrt) projections + D3D11 interop | in-process, full WGC API (`SystemRelativeTime`, `IsBorderRequired`) | needs D3D11 device and staging-texture readback glue; wheel availability for 3.14 (H6) |
| `windows-capture` (Rust, PyPI) | prebuilt, small API | less control over crop/border/timestamps; maintainer dependency |
| Hand-rolled ctypes/COM (`comtypes` is already a dependency of dxcam) | no new wheel | ~300 lines of fragile COM |
| `ffmpeg gfxcapture` subprocess (`um`) | proven, trivial | **rejected for the daemon** (execs a user-writable binary at high integrity, per-call process start, no frame timestamps); acceptable only as a test oracle in the spike |

Whatever is chosen ships as an optional extra
(`pip install game-input-mcp[wgc]`) pinned in `pyproject.toml`; core installs
and the existing three backends are unaffected. `capture` with
`backend="wgc"` and no library returns `CAPTURE_BACKEND_UNAVAILABLE` with
`details.reason`. No runtime downloads, ever (contrast `win.py:95-125`).

#### Backend selection in `auto`

Default order stays dxcam, mss, pillow (priorities 10/20/100,
`dxcam_backend.py:18`, `mss_backend.py:17`, `pillow_backend.py:12`) so existing
behaviour does not change. `auto` **escalates to WGC first** when any of these
hold, and only when WGC is available:

1. the occlusion probe found another window over the client rect (Design 4);
2. the client rect is not inside one monitor (dxcam would decline anyway);
3. the first screen backend returned a black frame.

Explicit `backend="wgc"` always tries WGC only. Flipping the default order to
WGC-first is Decision D4 in Phase 5, after real-world soak. Result metadata:
`backend: {name: "wgc", mode: "window", session: "warm"|"cold", frame_age_ms}`.

### 4. Capture diagnostics (all backends)

New optional `warnings: [{code, message, details}]` on the `capture` result.

| Code | Trigger | Notes |
| --- | --- | --- |
| `TARGET_OCCLUDED` | any of 9 sample points in the client rect hits a foreign root window (`WindowFromPoint` + `GetAncestor(GA_ROOT)`, same-pid owned windows ignored) | `details.by{hwnd,pid,exe,title}`; click-through layered overlays are skipped by `WindowFromPoint`, so HUD overlays do not trigger it |
| `BLACK_FRAME` | max channel < 8 on a 16x9 downscale | implements the 07-02 `CAPTURE_BLACK_FRAME` intent as a warning; triggers `auto` escalation |
| `FRAME_IDENTICAL_TO_PREVIOUS` | hash equals the previous capture of this hwnd within 60 s | see below |
| `WGC_NO_NEW_FRAME` | Design 3 | |
| `HDR_COLOR_SHIFT_POSSIBLE` | display HDR on (and Auto HDR on for this exe or globally) | see below |

**Frozen-oracle cross-check** (`oracles...md:55-68`). Identical frames are
ambiguous: frozen capture, or a static scene that still burns CPU rendering
identical frames. Heuristics (`um` suggests comparing process CPU time) cannot
tell them apart, so when the backend was WGC, the target is not occluded and a
screen backend is available, an identical hash triggers a one-off capture with
that screen backend. If it differs, the WGC session is discarded and rebuilt,
the fresh frame is returned and the warning carries `details.cross_check:
"differs"`. If it matches, the warning is dropped (genuinely static).
`GetProcessTimes` CPU delta is attached as `details.cpu_ms_since_previous` for
agents that still want it.

**HDR.** Implemented as a read-only probe module with two inputs: the display
advanced-colour state of the target's monitor via `DisplayConfigGetDeviceInfo`
(no registry), and the per-exe / global Auto HDR flag from
`HKCU\Software\Microsoft\DirectX\UserGpuPreferences`, parsed with `um`'s rule
(`win.py:253-264`, fixture format `AppStatus=1;AutoHDREnable=2097;`, odd =
on). The registry access is `KEY_READ` on that one fixed key; it is not exposed
as a tool and is the only registry touch in the project. Warn only when the
display is HDR; `um` warns on the flag alone (`win.py:267-271`) and so
false-positives on SDR displays. No tone-mapping, no fixing, only a warning
with `details{display_hdr, auto_hdr, exe}`.

**Other capture metadata** (additive): `capture_qpc_ns` (QPC at frame handoff;
for WGC also `frame_qpc_ns` from `SystemRelativeTime` if H5 holds), `frame_hash`
(blake2b-8 of the pixels), `hdr`.

**Thumbnail.** `capture(..., thumb_width=None)`: when set, also writes
`thumb_<frame_id>.png` and returns `thumb_path`, `thumb{width,height,scale}`.
The thumbnail shares the frame's `frame_id`; the agent clicks with
`scope="normalized"` (already supported, `geometry.py:44-47`) or
`scope="capture"` against the full image. The prefix is `thumb_`, not
`frame_<id>.thumb.png`, on purpose: `FrameCache.cleanup`
(`frames.py:88-93`) treats any `frame_*.png` without a matching `.json` as an
orphan and deletes it, so a `frame_<id>.thumb.png` would be removed on the
next sweep. `cleanup` is extended to expire `thumb_*` with its frame.

### 5. Target metadata

`TargetInfo` (`models.py:66-90`) gains `exe: str | None` (basename via
`QueryFullProcessImageNameW`; `None` when the process cannot be opened) and
`monitor: int | None`. `to_dict` adds both, so `list_targets`,
`get_target_info` and every `target` block carry them. Both fields default to
`None`, so existing positional constructions in tests keep working. Resolver
by exe/title (`target={"exe": "Game.exe"}`) and the `TARGET_AMBIGUOUS` error
are Phase 5.

### 6. Window geometry (deferred, opt-in)

Only if Decision D5 says yes. `set_window_geometry(target, client_size=None,
position=None)` using `SetWindowPos` with the border delta computed from
`GetWindowRect`/`GetClientRect` (the arithmetic of `WinDrive.ps1:177-184`).
Refused unless `allow_window_mutation` is true (`WINDOW_MUTATION_DISABLED`),
and refused for windows with no non-client frame where it would be a no-op or
a mode change. It mutates the target, which is why it is config-gated and why
it is last. There is no `untop`: game-input never sets TOPMOST.

### 7. MCP surface

| Tool | Change | Backward compatible |
| --- | --- | --- |
| `capture` | `backend` Literal gains `"wgc"`; new `thumb_width`; result gains `warnings`, `capture_qpc_ns`, `frame_hash`, `hdr`, `thumb*` | yes, all optional/additive |
| `get_target_info`, `list_targets` | target dict gains `exe`, `monitor`; `get_target_info` also returns `presence` | yes |
| `input_session_open`, `input_session_state` | view gains `presence`, `guard` | yes |
| all input tools | may return `warnings`; new error codes below | additive |
| one-shot input tools | now fail closed (`TARGET_NOT_FOREGROUND`, `FOCUS_FAILED`, `FRAME_GEOMETRY_CHANGED`) | **behaviour change**, listed in Risks |
| `focus_target`, `focus_window` | may return `USER_PRESENT` under `focus`/`strict` | behaviour change when the policy is on |
| new tools | none in Phases 1-4 (Phase 5 optionally `set_window_geometry`) | n/a |

### 8. Configuration

Read once at daemon start from `%LOCALAPPDATA%\game-input-mcp\config.json`;
environment variables with the `GAME_INPUT_` prefix override (the unimplemented
`GAME_IO_*` names in the 07-02 spec are superseded). `install.py` gains
`--set key=value` for convenience; changing a value needs `--restart`.

| Key | Values | Default |
| --- | --- | --- |
| `foreground_guard` | `strict`, `warn`, `off` | `strict` |
| `frame_geometry_check` | `strict`, `warn`, `off` | `strict` |
| `presence` | `off`, `warn`, `focus`, `strict` | `focus` (D1; `warn` is the conservative alternative) |
| `presence_idle_s` | seconds | `30` |
| `capture_backend` | `auto`, `dxcam`, `mss`, `pillow`, `wgc` | `auto` |
| `capture_timeout_ms` | ms | `1500` |
| `wgc_idle_ttl_s` | seconds | `5` |
| `allow_window_mutation` | bool | `false` |

Relaxing a guard is only possible here, never per call.

### 9. Error codes added

`USER_PRESENT`, `USER_TOOK_OVER`, `TARGET_NOT_FOREGROUND`,
`FRAME_GEOMETRY_CHANGED`, `CAPTURE_TIMEOUT`, `CAPTURE_BACKEND_UNAVAILABLE`
(already in the 07-02 list, now emitted), `WINDOW_MUTATION_DISABLED` and
`TARGET_AMBIGUOUS` (Phase 5). `CAPTURE_BLACK_FRAME` is realised as the
`BLACK_FRAME` warning rather than an error, because a legitimately black scene
(a loading screen) should still return a frame.

## Wire protocol changes

None to framing (4-byte length prefix + UTF-8 JSON). New request fields
(`thumb_width`, `backend="wgc"`) and response fields are additive; unknown
fields are ignored by existing clients. No new wire methods in Phases 1-4:
presence rides on `get_target_info`.

The one operational gap: neither client has a read timeout
(`ipc.py:121-141`), so a capture that hangs would hang the caller. This spec
puts the deadline in the daemon (`capture_timeout_ms`, bounded WGC waits)
instead of relying on the client. A client-side read timeout
(`total_ms + 2000` for timelines, as the 09-09 text intended) is a separate
small fix worth doing alongside Phase 3.

## Separate components (not built here)

Each is a decision about *where* the idea lives, so nobody re-litigates it.

| Idea | Home | Constraints |
| --- | --- | --- |
| Window recording with only the game's audio, encoder auto-pick | optional `game-record` tool, **medium integrity**, never in the daemon | pins or verifies its own ffmpeg; joins to game-input through the QPC clock (`capture_qpc_ns`, edge `qpc_ns`, `ProcLoopback` `start_hns`); WGC library may be shared |
| Launch / list / kill by exact PID, crash-reporter cleanup | existing `steam-mcp` for Steam; otherwise a small process tool, outside the daemon | exact-PID kill only; no shell string building (`win.py:195,199` shows the hazard) |
| In-game JSON bridge | per-game mod, plus a short convention doc (`observe` / `step` / `click` as in `AgentBridge.cs:1-24`) | a bridge replaces pixels and SendInput for that game; game-input stays unaware and handles menus and anything unbridged |
| Log oracles | agent's own file tools | not a game-input capability |

## Phases

Phases are independently shippable; the dependency graph is `0 -> {1 -> 2, 3
-> 4} -> 5`, where 3 only needs the `exe` metadata from 1 for HDR (Phase 4).
Each phase is TDD against the existing pytest suite, updates `README.md`, and
ends with a Notepad smoke run plus the manual items listed.

### Phase 0: Spikes (no product code)

- **S1 presence signal.** Script that (a) injects a key and checks whether
  `GetLastInputInfo.dwTime` moves (H1), (b) records tick resolution with and
  without `timeBeginPeriod(1)`, (c) runs the `PresenceMonitor` attribution
  logic live while a person types between injections.
- **S2 WGC.** Prototype each candidate library (Design 3 table) against
  Notepad, a DirectX sample, a covered window, a minimized window and an
  elevated target. Measure cold-start latency, first frame on a static window
  (H2), border behaviour (H3), extended-frame-bounds crop (H4),
  `SystemRelativeTime` vs `perf_counter_ns` offset (H5), wheel availability
  (H6). Use `ffmpeg gfxcapture` output as the pixel oracle.
- **S3 HDR.** Query advanced-colour state and the `UserGpuPreferences` values
  on this machine; capture one frame with every backend under HDR on and off
  (H7, H8).
- Exit: hypotheses table filled in as an appendix here, D3 chosen, D1
  threshold confirmed against S1 noise.

### Phase 1: Injection chokepoint, foreground guard, metadata

Files: new `guard.py`; `daemon.py` handlers; `win32.py` (`root_window_at`,
`exe_of_pid`, `monitor_of_hwnd`); `models.py`; `targets.py`; `README.md`.

Tests (`tests/test_guard.py`, additions to `test_daemon_handlers.py`):
- foreground table: target / none / other x keyboard / relative / absolute,
  including the destination-point exception and its `exception` marker;
- `mouse_click`, `mouse_drag`, `scroll` return `FOCUS_FAILED` and send nothing
  when focus fails (today they send, `daemon.py:188-190`);
- `activate=False` with another window foreground returns
  `TARGET_NOT_FOREGROUND` for every one-shot tool and sends nothing;
- `FRAME_GEOMETRY_CHANGED` when the client rect or DPI differs; identical rect
  passes; `warn` mode adds a warning instead;
- watchdog release still works with the gate failing (`bypass_gate`);
- every previous session/timeline test passes unchanged;
- `TargetInfo.to_dict` carries `exe`/`monitor`, constructors without them
  still work; CJK window title round-trips through `ipc` (row 19).

Manual: cover Notepad with another window and click with `activate=False`.

### Phase 2: User presence

Files: new `presence.py`; `guard.py` integration; `daemon.py`
(`get_target_info`, session views, gate calls); `input/timeline.py` (`gate`
param, `user_input` stop reason); `install.py --set`; config loader.

Tests:
- attribution: human then own, own then human, wrap-around, `own_tick` unset,
  and the H1-false variant (injection does not move `dwTime`);
- two-thread test: A probes while B is between `SendInput` and
  `note_injection` (the lock must prevent misattribution);
- policy matrix (4 policies x classes A/B/C) incl. `USER_PRESENT` details and
  `retry_after_ms`;
- `strict`: session paused with `reason: "user_input"`, held keys released,
  timeline returns the partial log and `pending_indices`, auto-resume after
  `presence_idle_s`;
- `TimelineRunner` accepts both `foreground_ok` and `gate`;
- `get_target_info` returns `presence`; unavailable signal -> `unknown`;
- `slow` real-clock timeline tests still show max error <= 1 ms with the gate
  in the loop.

Manual: type during a `strict` session (brake), hands off (resume),
`focus_target` while typing (`USER_PRESENT`).

### Phase 3: WGC backend

Files: `capture/base.py` (additive hooks), new `capture/wgc.py` (+ glue chosen
by S2), `capture/service.py` (pass `target`, escalation, occlusion probe, black
check), `server.py` (`Literal`), `pyproject.toml` (`[wgc]` extra), `README.md`
(replace "`windows_graphics_capture` is not included in this v1").

Tests (fake WGC source, no GPU):
- existing `test_capture_backends.py` / `test_capture_service.py` unchanged
  and green; `capture_region(rect, backend="auto")` without `target` behaves
  as today;
- crop math from extended-frame-bounds incl. a negative-origin monitor and
  mixed DPI;
- session lifecycle: warm reuse, TTL teardown, resize recreate, max 4;
- waiting: new frame, `WGC_NO_NEW_FRAME` with `frame_age_ms`,
  `CAPTURE_TIMEOUT`, teardown never blocks the request thread;
- `auto` escalation matrix (occluded / cross-monitor / black -> WGC; WGC
  absent -> chain continues, warning added); explicit `wgc` without the
  library -> `CAPTURE_BACKEND_UNAVAILABLE`;
- occlusion probe: foreign window, same-pid owned window ignored,
  click-through overlay ignored; black detector on synthetic images.

Manual: covered Notepad, window straddling two monitors, borderless DX
sample, minimized target (`TARGET_MINIMIZED`), a ReShade title (Phase 4 uses it).

### Phase 4: Capture diagnostics

Files: new `capture/diagnostics.py` (hash, cross-check, HDR probe), `frames.py`
(`thumb_` files, cleanup), `capture/service.py`.

Tests:
- identical-hash path: WGC + screen backend differs -> session rebuilt, warning
  with `cross_check: "differs"`; matches -> no warning; non-WGC -> warning only;
- HDR: parser fixtures (`AppStatus=1;AutoHDREnable=2097;`, global
  `DirectXUserGlobalSettings`, per-exe precedence, even = off), fake display
  state (HDR display + flag -> warning; SDR display + flag -> none); the probe
  opens the key read-only;
- thumbnail: file written, `scale` right, `normalized` click maps to the same
  screen point as the full-size `capture` click, `cleanup` removes
  `thumb_*` with its frame and no longer deletes live ones;
- `capture_qpc_ns` is monotonic and comparable with timeline `qpc_ns`.

### Phase 5: Optional follow-ups

- Resolver by exe/title with `TARGET_AMBIGUOUS`.
- `set_window_geometry`, gated (Decision D5).
- Flip `auto` to WGC-first (Decision D4) after soak; keep `capture_backend`
  config as the rollback.
- Client-side read timeout in `ipc.Client` / `client.Client`.
- Convention doc for the bridge pattern.

## Risks

- **R1. `GetLastInputInfo` limits.** Gamepad activity is invisible; other
  injectors and remote-desktop input count as "not us"; two events in one tick
  are indistinguishable. Documented; the Raw Input route is the upgrade path.
- **R2. Guards block legitimate unattended runs.** Mitigated by daemon-level
  `off`/`warn`, never per call. The defaults in Configuration are the decision
  to review (D1, D2).
- **R3. Behaviour changes for existing callers.** One-shot tools that relied on
  `activate=False` background sends, or on a mouse click proceeding after a
  failed focus, now get structured errors. Known consumers (09-09 spec) use
  sessions or `activate=True`; `foreground_guard=warn` is the escape hatch.
- **R4. WGC freezes under ReShade or independent flip** (`oracles...md:55-68`).
  Mitigated by hash plus cross-check; not mitigated if no screen backend can
  see the window either.
- **R5. Capture indicator / border.** Win10 always draws a yellow border; Win11
  may allow hiding it (H3). Warm sessions avoid flashing per call; the border
  is visible to the human either way.
- **R6. Native dependency on Python 3.14.** H6 may fail; fall back to
  hand-rolled COM or ship WGC only on interpreters with a wheel. It is an
  optional extra, so nothing regresses.
- **R7. Elevated code from user-writable places.** The daemon already executes
  the installing user's Python at `/RL HIGHEST` (`install.py:74-83`), so any
  user-writable code is a privilege boundary. This spec does not widen it: no
  ffmpeg, no runtime downloads, WGC library pinned. Hardening the install
  location is out of scope but worth its own spec.
- **R8. Chokepoint contention** could eat into the <= 1 ms timeline error
  budget; mitigated by a microsecond critical section and the Phase 2 real-clock
  assertion.
- **R9. Occlusion probe false results.** Nine sample points can miss a small
  overlay, and `WindowFromPoint` skips click-through windows by design; it is a
  warning, not a proof. WGC exists precisely so correctness does not depend on
  it.
- **R10. Human-takeover sensitivity** under `strict`: a bumped mouse pauses the
  session. Acceptable by design (the brake is the point); tune via threshold or
  move to Raw Input if S1 shows it is too twitchy.
- **R11. Security scope.** Guards stop mistakes, not a hostile agent: the
  config file is user-writable, as is the pipe's client side.

## Decisions for the owner

- **D1.** Default `presence` policy: `focus` (recommended: refuses only the
  focus-steal that caused the documented incident) or `warn` (no behaviour
  change); threshold 30 s vs `um`'s 60 s.
- **D2.** `foreground_guard` and `frame_geometry_check` default `strict`
  (fail closed, may break callers that relied on `activate=False` background
  sends) or `warn` for a release first.
- **D3.** WGC library, after S2. **Decided: `windows-capture`** (see below).
- **D4.** Make `auto` WGC-first after soak, or keep the escalate-only rule
  permanently.
- **D5.** Whether `set_window_geometry` is built at all.
- D1 and D2 were implemented with the recommended defaults (`presence=focus`,
  threshold 30 s, guards `strict`); both are one `install --set` away from `warn`.

## Implementation notes (2026-10-07)

### Phase 0 findings

| ID | Result | Evidence |
| --- | --- | --- |
| H1 | **True.** `SendInput` moves `GetLastInputInfo.dwTime` (F24 key and a zero mouse move both did), so the injection-aware attribution is required, not optional | spike S1 |
| tick resolution | `GetTickCount` steps of 15/16 ms without `timeBeginPeriod` | spike S1 |
| H2 | **True for the first frames.** A static tk window delivered a first frame ~270 ms after start and a second at ~670 ms | spike S2 |
| H4 | **True.** Frame = DWM extended frame bounds (402x332 for a 400x300 client); client origin inside the frame = client origin minus extended-bounds origin ((1, 31) here); the cropped pixels matched the known red/blue content | spike S2 |
| H5 | **True.** `Frame.timespan` x 100 is on the `perf_counter_ns` clock (~5 ms behind at callback time), so `frame_qpc_ns` is comparable with the `qpc_ns` of input edges | spike S2 |
| H6 | **True.** `windows-capture` 2.0.1 ships a `cp39-abi3` wheel; pywinrt projections ship `cp314` wheels | `pip download` |
| H3, H7, H8, H9 | not measured (border visibility needs a human eye; HDR is Phase 4; exclusive fullscreen needs a game) | |

D3: `windows-capture` takes `window_hwnd`, `draw_border`, `cursor_capture`,
`minimum_update_interval` and runs free-threaded. Cost: it pulls `numpy` and
`opencv-python`. The library is confined to `capture/_wgc_native.py`, so moving
to pywinrt later touches one module.

Live end-to-end check (a tk window fully covered by a green topmost window):
`backend="pillow"` returned the cover's green for every pixel; `backend="auto"`
escalated to `wgc` and returned the real red/blue content (cold 1.26 s including
the first import, warm ~0.1 s). One-shot tools refused `activate=False` with
`TARGET_NOT_FOREGROUND` while another window was in front, and `focus_target`
returned `USER_PRESENT` while the machine's user was typing.

### Deviations from the design text

- **No single `Injector.send` chokepoint.** Gate decisions (foreground, presence)
  run in the handlers (`guard.py`, `daemon._preflight_*`); presence *attribution*
  is hooked at the lowest level instead: `win32._send_inputs` and the Alt press in
  `focus_window_detailed` call `win32._injecting()`, which the daemon points at
  `PresenceMonitor.injecting` (probe, send, note under one lock). Same guarantee
  (every injection is attributed, atomically), far less handler churn, and tests
  that patch `win32.send_*` keep working. Measured overhead of the bracket: ~2 us.
- **`WGC_NO_NEW_FRAME` and the wait.** A warm session waits `FRESH_GRACE_S`
  (250 ms) for a frame newer than the request, not the full `capture_timeout_ms`;
  only a cold session waits the full timeout. Without this every capture of a
  static window would cost 1.5 s.
- **`TARGET_OFFSCREEN` warning added** next to `TARGET_OCCLUDED` (a sample point
  with no window at all), and it also triggers `auto` escalation.
- **`activate=False` key tools need a resolvable target** now (the guard needs
  the hwnd). `key_up` is exempt from every gate and still works without one.
- **Existing tests changed** (all consequences of the intended behaviour):
  `test_models` (two new dict keys), `test_daemon_handlers` (autouse foreground
  fixture; `type_text` with `activate=False` now patches `resolve_target`),
  `test_capture_service` (fake `capture_region` accepts the new keyword
  arguments). `tests/conftest.py` makes presence hermetic (user away by default).
- **`key_up` for unknown names** still raises `ValueError` out of the handler
  (pre-existing, found while testing; not changed here).

### Phase 4

Implemented as designed (`capture/diagnostics.py`, `capture/hdr.py`,
`frames.py`, `capture/service.py`, `win32.process_cpu_ms`), with these findings and
deviations:

- **The cross-check needs a tolerance.** Live, with a static test window, WGC and
  a screen grab of it differed in ~0.01 % of the pixels (a handful, the kind of
  noise a cursor, caret or capture-indicator flash causes). Treating any
  difference as "frozen" would discard healthy sessions, so the screen frame
  replaces the WGC one only when more than `CROSS_CHECK_DIFF_FRACTION` (0.5 %) of
  the pixels differ by more than 8 per channel; the fraction is reported as
  `details.diff_fraction`. A stalled capture differs across most of the frame, so
  the trade-off only misses staleness in an almost-static scene, where it hardly
  matters.
- **A static window emits very few WGC frames.** In a 4 s recording it delivered
  one (the earlier spike saw two), so "no new frame" is the normal state of a
  static window, which is why `WGC_NO_NEW_FRAME` is a warning and not an error,
  and why a warm static capture costs the full 250 ms grace. The first frame of a
  fresh session can differ from the next one (seen on a tk window; not
  diagnosed), so a single early pair is weak evidence either way.
- **Asymmetry kept from the design:** a WGC frame that the screen backend agrees
  with raises no warning; a screen-backend frame identical to the previous one
  always raises `FRAME_IDENTICAL_TO_PREVIOUS` (`cross_check: "not_applicable"`),
  which also tells an agent that its last action changed nothing visible.
- **HDR on this machine:** one display, HDR off (`DisplayConfig` reports the
  path as supported but not enabled), no `AutoHDREnable` value in the registry.
  The probe therefore stays silent here; the HDR-on branch is covered by tests
  only. H7 (the odd-means-on parity rule) is still unmeasured. H8 (HDR also
  affects dxcam/mss/pillow) is unmeasured, but the warning is raised for every
  backend.
- **`thumb_width`** is forwarded to the daemon only when set, so a new MCP server
  still works against an older daemon.

### Phase 5

- **Client read timeout.** Neither client could time out: both use synchronous
  pipe handles, and `ReadFile` on one cannot be bounded. Both now poll
  `PeekNamedPipe` (0.2 ms backing off to 5 ms) before each read, against one
  deadline per call. Default 30 s; `run_timeline` / `mouse_move_relative` pass
  their duration + 5 s (`LONG_CALL_SLACK_S`); `None` restores the old unbounded
  wait. `read_timeout_s` is a reserved keyword of `Client.call`/`invoke`, never
  forwarded. Errors: `DaemonTimeout` (pywin32 client, mapped by the MCP server to
  `DAEMON_TIMEOUT`) and `InputError` with `DAEMON_TIMEOUT` (stdlib client).
  Tested on real named pipes. The 09-09 spec's `total_ms + 2000` is now `+ 5 s`.
- **Targets by exe / title.** `TargetSpec` gains `exe` (basename, case-insensitive,
  `.exe` optional, whole-name match) and `title` (case-insensitive substring);
  `hwnd` and `pid` still win. Several windows of one process are not ambiguous
  (largest client area, as for a pid); windows of several processes raise
  `TargetAmbiguous` -> `TARGET_AMBIGUOUS` with up to 10 `{pid, hwnd, exe, title}`
  candidates, from the daemon handlers and from `capture`.
- **`set_window_geometry`** (D5: built). Gated by `allow_window_mutation`
  (`WINDOW_MUTATION_DISABLED` with the `install --set` hint). Refuses minimized,
  maximized and whole-monitor windows, and a client resize of a window without a
  caption frame (`WINDOW_RESIZE_FAILED`); moving a frameless window is allowed.
  Sizes 64..16384 per side. Never activates or reorders the window. A window that
  clamps the size gets `WINDOW_SIZE_ADJUSTED`. Live: a tk window went 400x300 ->
  640x480 and moved as asked; with the default config it was refused.
- **`auto` WGC-first** (D4): implemented as `auto_wgc_first` (default **false**),
  not as a changed default. H3 now holds on this machine (a WGC session ran with
  no visible border: 4 stray yellow-ish pixels around the window, measured from a
  screen grab, non-elevated Python, Windows 11; the elevated, deployed daemon's own
  WGC session showed 0), which removes the main objection,
  but the design said "after soak" and there has been none, WGC costs 0.4-1.3 s
  cold and 250 ms on a static window against ~0.2 s for a screen grab, and the
  escalation rules already send the cases where a screen grab is wrong to WGC.
  Turn it on with `install --set auto_wgc_first=true`; it still falls through to
  the screen backends when `wgc` is missing or fails. Whether to make it the
  default is left to whoever has run it for a while.
- **Bridge convention** written down in `docs/bridge-contract.md`; no code.

### Not done

Nothing from the design remains open except flipping the `auto_wgc_first` default.
H7 (the odd-means-on Auto HDR parity rule), H8 (HDR also shifts the screen
backends) and H9 (exclusive fullscreen) are unmeasured. The running daemon is
installed from the main checkout, so changes are live in it only after that
checkout is updated and the task restarted.
