# windows-input-mcp

OS-level Windows input MCP server for driving Unity games during
`navigate-to-gameplay` workflow. Wraps Win32 SendInput + window
manipulation with **framebuffer↔client↔screen scope auto-translation**
so agents don't manually compute DPI / window offset / multi-monitor
negative coordinates.

## Why this exists

Unity Explorer MCP's `simulate_key inject` fails for some games,
`simulate_key os` requires foreground + sometimes hits Win32 error 87,
`click_widget` doesn't work for self-custom Selectable subclasses or
full-screen InputCatcher. Agent has to fall back to manual
`Add-Type [DllImport("user32.dll")]` PowerShell on every call, which
wastes turns and silently breaks when window position changes.

This MCP gives agent stable input primitives with **scope abstraction**:

| scope | (x, y) meaning |
|---|---|
| `framebuffer` | Unity render framebuffer pixel (matches UE screenshot dimensions) — auto-scaled to client area |
| `client` | window client area pixel (relative to client top-left) |
| `screen` | absolute desktop pixel (multi-monitor virtual screen) |

DPI awareness is set to PER_MONITOR_AWARE_V2 at process start so all
internal coords are physical pixels — no virtual-pixel surprises.

## Tools

- `get_window_info(pid)` — window rect / client size / screen origin / DPI / foreground status
- `focus_window(pid)` — bring window to foreground (restore from minimized)
- `mouse_click(pid, x, y, button?, scope?, clicks?, framebuffer_width?, framebuffer_height?, activate?)`
- `mouse_drag(pid, from_x, from_y, to_x, to_y, button?, scope?, ..., steps?, activate?)`
- `scroll(pid, x, y, delta?, scope?, ..., activate?)`
- `send_keys(pid, keys, activate?)` — supports `{enter}` / `{esc}` / `{space}` / `{tab}` / arrows / `{f1}`–`{f12}` + literal Unicode chars

## Architecture: split MCP server + elevated daemon

```
Claude Code (medium IL)
   └── MCP server (medium IL)  ── named pipe ──▶  daemon (high IL)
       windows_input_mcp.server                   windows_input_mcp.daemon
       (thin client — 1 IPC call per tool)        (does the real SendInput)
```

UIPI (User Interface Privilege Isolation) blocks medium→high SendInput.
Games launched with admin rights (anti-cheat, some launchers) therefore
ignore input from a non-elevated MCP server. The daemon runs at high IL
via a per-user `ONLOGON` scheduled task with `/RL HIGHEST`, registered
once; from then on every login starts it automatically with no UAC prompt.

The named pipe is per-user: `\\.\pipe\windows-input-mcp.<SID>`, with a
DACL granting only the owning user's SID — other users on the same
machine cannot inject inputs through it.

## Install

```powershell
git clone https://github.com/neeetman/windows-input-mcp.git
cd windows-input-mcp
python -m pip install -e .
```

> Requires Windows + Python ≥ 3.10. `pywin32` is pulled in automatically.

### One-time daemon install (requires elevated shell)

```powershell
# Open PowerShell as Administrator
python -m windows_input_mcp.install
```

This registers the `WindowsInputDaemon` scheduled task and starts it
immediately. The task auto-starts on every subsequent login — no UAC
prompt at runtime.

Lifecycle commands:
```powershell
python -m windows_input_mcp.install --status      # query schtasks state
python -m windows_input_mcp.install --restart     # stop + start now
python -m windows_input_mcp.install --uninstall   # remove the task
```

You can also run the daemon manually (non-elevated) for development —
it will work for non-admin targets:
```powershell
python -m windows_input_mcp.daemon
```

## Register the MCP server in project .mcp.json

```json
{
  "mcpServers": {
    "windows-input": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "windows_input_mcp.server"]
    }
  }
}
```

After editing `.mcp.json`, run `/mcp` to reconnect.

If the daemon is not running, every tool returns
`{"success": false, "error": "daemon pipe ... not found. Run: python -m windows_input_mcp.install"}`
— the MCP server itself stays up regardless.

## Framebuffer scope walkthrough

Agent takes UE screenshot of a 1280×819 framebuffer and sees a button at
pixel (640, 410). The game window is 1600×1024 client size at screen
origin (−1760, 39) on a secondary monitor.

```
mouse_click(
  pid=<game pid>,
  x=640, y=410,
  scope="framebuffer",
  framebuffer_width=1280,
  framebuffer_height=819,
)
```

Internally:
1. `get_window_info` → client_size=(1600, 1024), client_screen_origin=(−1760, 39)
2. Scale framebuffer (640, 410) to client (640 * 1600/1280, 410 * 1024/819) = (800, 512)
3. Translate to screen (−1760 + 800, 39 + 512) = (−960, 551)
4. `SendInput MOUSEEVENTF_ABSOLUTE` normalized against virtual screen
   (including negative origin) — click lands correctly on the secondary
   monitor.

Without this MCP the agent has to hand-write the same logic with
PowerShell `Add-Type` + `[DllImport("user32.dll")]` every navigate
session.

## Limitations

- Windows only (uses Win32 API).
- DPI awareness: if your terminal / Python invocation runs as
  DPI-unaware, GetWindowRect may return scaled coords. The server sets
  PER_MONITOR_AWARE_V2 at import; if you embed it differently, ensure
  the host process is DPI-aware before any window query.
- `send_keys` Unicode mode (`KEYEVENTF_UNICODE`) bypasses keyboard
  layout — useful for typing CJK characters into chat boxes but may not
  match games that read raw scan codes for movement keys. For game
  movement use VK names like `{w}` is **not** supported; instead use
  `{up}` / `{down}` / `{left}` / `{right}` for arrows, or extend
  `KEY_NAME_TO_VK` in `win32.py` with the VK codes you need.
