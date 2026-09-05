# shipboard

Voice input daemon for agent TUIs: hold-to-talk transcription with a
paste-and-send mode, wired straight into your keyboard via evdev.

Part of [shipboard](../README.md). Requires the local whisper.cpp server
(see the parent compose file and wake proxy).

## How it works

The daemon reads evdev keys directly (defaults: `KEY_PAUSE` for record,
`KEY_SCROLLLOCK` for send), so it works identically on any Wayland/X11
compositor (niri, Hyprland, sway, …) with no compositor-specific code. The
triggers are configurable via `key_record` / `key_send` / `key_record_send`
(evdev key names, e.g. `insert`, `home`, `f13`; empty string disables a
trigger). Key injection for the send modes uses a uinput virtual keyboard —
modifier combos are layout-independent, so non-Latin text pastes correctly
regardless of the active layout.

## Modes

- **Record key held** (default: **Pause**) — record → whisper → `wl-copy`
  (clipboard only). With `key_record_mode = "toggle"`, press to start
  recording and press again to stop and process.
- **Send key tap** (default: **Scroll Lock**) — paste current clipboard +
  Enter (send only).
- **Record + send keys** (default: **Pause + Scroll Lock**) — record →
  whisper → clipboard → paste → Enter (auto-send after recognition). The keys
  can be pressed in either order; a 150 ms grace window catches
  near-simultaneous presses.
- **Optional third key** (`key_record_send`) — record → clipboard → paste +
  Enter in one go, while held.

Notifications: Recording… / Processing speech… / Copied… / Sent…

## Dictation symbols

After transcription the text is normalized: spoken punctuation names become
symbols and get glued to neighbors (`тильда слэш точка конфиг` →
`~/.config`, `alt dash talk` → `alt-talk`). Russian and English names are
supported. Disable with `SHIPBOARD_NORMALIZE=0`.

## Run

```bash
# as a systemd user service
install -m 0644 shipboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now shipboard

# or from a compositor (niri / Hyprland autostart):
#   spawn-at-startup "shipboard"   /   exec-once = shipboard
```

A single-instance flock guards against double starts.

## Compositor keybinds

The daemon does **not** need keybinds — but the compositor should swallow the
keys so they never reach the focused app (kitty encodes unbound keys as
`CSI …u` sequences). See the parent README's Troubleshooting section.

## Configuration

Settings come from, in order of precedence: built-in defaults < the TOML
config file (<code>~/.config/shipboard/shipboard.toml</code>) <
environment variables (<code>SHIPBOARD_*</code>).

```bash
shipboard setup   # TUI: edit settings, test STT, compositor snippets,
                    # restart the daemon (stdlib curses, no dependencies)
shipboard config   # generate the config file (if missing) and show the
                    # effective settings
shipboard status   # daemon state, keys, last transcript
```

See <code>shipboard.toml.example</code> for the documented template. Point
<code>whisper_url</code> at a tailnet host to transcribe through a remote
machine's GPU (see the parent README's Remote use section).

## Manual / debug

```bash
shipboard --send                 # one-shot paste + Enter (refuses while recording)
shipboard --file /tmp/x.wav      # transcribe an existing file
SHIPBOARD_DRY_RUN=1 shipboard --send   # print instead of injecting
```

## Config

See the env/TOML table in the parent README (`SHIPBOARD_*` variables and the
matching TOML keys). Highlights:

- **Triggers** — `key_record` (default `pause`), `key_send` (default
  `scrolllock`), optional `key_record_send`; any of them empty = that trigger
  is disabled. `key_record_mode` is `hold` (record while held) or `toggle`
  (press to start / press again to stop).
- **Paste combo** — `paste_combo` defaults to `ctrl+shift+v` (kitty/browser);
  modifiers `ctrl`/`shift`/`alt`/`super` plus a final key from `a`–`z`,
  `0`–`9`, `f1`–`f24`, `v`, `insert`, `enter`, `space`, `tab`, `home`, `end`,
  `pageup`, `pagedown`, `delete`, `backspace` — e.g. set
  `SHIPBOARD_PASTE_COMBO=ctrl+v` if your target app wants plain Ctrl+V.
- **Enter flags** — `send_enter` is the global default; `scroll_send_enter`
  and `both_send_enter` override it per trigger (all three default on).
- **Recording** — `max_hold` caps recording length in seconds (single value,
  stuck-key guard); `record_rate` / `record_channels` control the
  `pw-record` input (defaults 16000 Hz / 1 ch).
- **Platform** — `whisper_language` is the language hint sent to the STT
  server (`auto`/`ru`/`en`/…, `auto` by default); `kws_threads` sets the
  wake-word (sherpa KWS) onnxruntime thread count (default 2).
