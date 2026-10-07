# In-game bridge: a convention, not a feature

game-input drives a game from the outside: pixels in, `SendInput` out. For games
where you control the code (a mod, a plugin, a debug build) there is a better
loop that needs neither: a small server inside the game that reports state as
text and accepts controls directly. universal-modder's Terraria `AgentBridge.cs`
is the reference. This page records the shape so bridges for different games look
alike. **game-input does not implement, load or talk to a bridge**: engine
injection is a non-goal (see `specs/2026-07-02-game-io-generalization-design.md`),
and a bridge belongs with the game's mod, not in an elevated input daemon.

## Why bother

- No focus dependency. A bridge sets controls inside the game, so the game need
  not be foreground and there is no keystroke that can leak into another window
  or collide with a human typing (the failure `presence` and the foreground guard
  exist for).
- Text beats pixels. `observe` returns menu options, health, positions and so on,
  at a fraction of the cost of a screenshot, and exactly what the game believes.
- It can be a frame-accurate oracle: the reply describes one consistent frame.

## Shape (from `AgentBridge.cs`)

- **Transport:** JSON lines over a TCP socket on `127.0.0.1` only, one request
  per line, one reply per line. Enabled by an environment variable (for example
  `AGENT_BRIDGE_PORT`); off by default.
- **Threading:** requests are queued and answered on the game's main thread right
  after an update, so every reply is consistent with one frame. Never touch game
  state from the socket thread.
- **Commands** (names are conventional, extend as needed):

  | Command | Meaning |
  | --- | --- |
  | `observe` | the screen as text: menu options as `{id, label}`, or world state |
  | `click` `{id}` | activate option `id` from the last `observe` |
  | `type` `{text}` | fill the on-screen text box |
  | `controls` `{...}` | set held controls for the coming frames (move, jump, fire, aim) |
  | `step` `{...controls}` | `controls` plus `observe` in one round trip: the control loop |

  Replies are one JSON object; failures are `{"error": "..."}`, never an
  exception that kills the socket.
- **Read menus generically** from the game's UI tree (anything with a click
  handler) instead of hard-coding positions, so the agent sees what a player sees.
- **Leave destructive actions out:** delete, cloud moves, purchases. They should
  not be reachable through `observe`/`click` at all.
- **Aim the way the game does** (the reference writes the cursor position the game
  reads each frame), and remove any "wait for the button to be released" guards
  that only make sense with a physical, focused window.

## How it pairs with game-input

| Need | Use |
| --- | --- |
| Launch, intro screens, anything before the mod loads | game-input (`set_keys`, `mouse_click`) from a captured frame |
| Menus and play once the bridge answers | the bridge |
| Check that it *looks* right (scale, facing, layering) | game-input `capture`, and read the result |
| A game the mod cannot reach, or an online game | game-input only, with `presence` left on `focus` or `strict` |

Treat the bridge as a second oracle next to screenshots: when they disagree,
believe neither until you know why (the screenshot oracle can be stale, see the
frozen-capture notes in the 2026-10-07 spec).
