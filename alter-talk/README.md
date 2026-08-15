# alter-talk

Voice input daemon for agent TUIs: hold-to-talk transcription with a
paste-and-send mode, wired straight into your keyboard via evdev.

Part of [whisper-local](../README.md). Requires the local whisper.cpp server
(see the parent compose file and wake proxy).

## How it works

The daemon reads `KEY_PAUSE` and `KEY_SCROLLLOCK` directly from evdev, so it
works identically on any Wayland/X11 compositor (niri, Hyprland, sway, …) with
no compositor-specific code. Key injection for the send modes uses a uinput
virtual keyboard — modifier combos are layout-independent, so non-Latin text
pastes correctly regardless of the active layout.

## Modes

- **Pause held** — record → whisper → `wl-copy` (clipboard only)
- **Scroll Lock tap** — paste current clipboard + Enter (send only)
- **Pause + Scroll Lock** — record → whisper → clipboard → paste → Enter
  (auto-send after recognition). The keys can be pressed in either order; a
  150 ms grace window catches near-simultaneous presses.

Notifications: Запись… / Обработка голоса… / Скопировано… / Отправлено…

## Run

```bash
# as a systemd user service
install -m 0644 alter-talk.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now alter-talk

# or from a compositor (niri / Hyprland autostart):
#   spawn-at-startup "alter-talk"   /   exec-once = alter-talk
```

A single-instance flock guards against double starts.

## Compositor keybinds

The daemon does **not** need keybinds — but the compositor should swallow the
keys so they never reach the focused app (kitty encodes unbound keys as
`CSI …u` sequences). See the parent README's Troubleshooting section.

## Manual / debug

```bash
alter-talk --send                 # one-shot paste + Enter (refuses while recording)
alter-talk --file /tmp/x.wav      # transcribe an existing file
ALTER_TALK_DRY_RUN=1 alter-talk --send   # print instead of injecting
```

## Config

See the env table in the parent README (`ALTER_TALK_*` variables). The paste
combo defaults to `ctrl+shift+v` (kitty/browser); set
`ALTER_TALK_PASTE_COMBO=ctrl+v` if your target app wants plain Ctrl+V.
