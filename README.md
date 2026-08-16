# shipboard

On-demand local speech-to-text for Linux desktops, plus a hold-to-talk voice
input daemon for agent TUIs.

A whisper.cpp server that **sleeps when idle** (frees ~1.5 GiB of VRAM) and
**wakes on the first request**, paired with `shipboard` — a compositor-agnostic
daemon that turns Pause / Scroll Lock into:

| Keys | What happens |
|---|---|
| **Pause** (hold) | record → whisper → clipboard |
| **Scroll Lock** (tap) | paste clipboard → Enter (send) |
| **Pause + Scroll Lock** | record → whisper → clipboard → paste → Enter (auto-send) |

The triggers are configurable evdev keys (`key_record` / `key_send`, and an
optional third `key_record_send`), and the record trigger can be `hold`- or
`toggle`-based (`key_record_mode`).

Everything runs locally — no cloud, no audio leaves your machine.

```
┌────────────┐  evdev   ┌──────────────┐  pw-record  ┌──────────────┐
│ Pause/ScrLk│ ───────► │ shipboard   │ ──────────► │  audio.wav   │
│  keyboard  │          │ (daemon, WM- │             └──────┬───────┘
└────────────┘          │  agnostic)   │                    │
                        └──────┬───────┘                    ▼
                               │ wl-copy           ┌─────────────────┐
                               │                   │ whisper.cpp     │
                               ▼                   │ container (GPU) │
                        ┌──────────────┐  HTTP     └────────┬────────┘
                        │  clipboard   │ ◄───────────────────┘
                        └──────┬───────┘   wakes on demand,
                               │           sleeps when idle
                               ▼
                        paste + Enter (send modes)
```

## Why this exists

- **VRAM is precious.** The container is stopped by a systemd timer after
  idle time and started on demand by a wake proxy — zero GPU footprint until
  you actually speak.
- **Agent workflows.** Dictate a prompt, hit one key, and it lands in your
  TUI chat box (kitty, opencode, Hermes, whatever) and gets submitted.
- **Layout-proof input.** The paste is injected as a modifier combo via
  uinput, so Cyrillic and any other layout paste correctly.

## Requirements

- Linux with Docker and a GPU supported by whisper.cpp's Vulkan build
  (AMD/Intel/NVIDIA; the compose file exposes `/dev/dri/renderD128`)
- PipeWire (`pw-record`), `wl-clipboard`, `python-evdev` (for shipboard)
- systemd user session (for the units)

## Quick start

```bash
# 1. Start the whisper server (downloads the model on first run)
docker compose up -d

# 2. Install the wake-proxy + idle-stop units
./scripts/install.sh
#    proxy listens on 127.0.0.1:10300, wakes the container on demand,
#    stops it after 5 minutes of silence

# 3. Install the shipboard daemon
install -m 0755 shipboard/shipboard.py ~/.local/bin/shipboard
install -m 0644 shipboard/shipboard.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now shipboard
```

No compositor keybinds are needed for the keys themselves: the daemon reads
Pause / Scroll Lock straight from evdev. You only need one compositor tweak —
**swallow the keys** so they don't leak into apps (see Troubleshooting).

## shipboard CLI

The installed `shipboard` binary manages the daemon from the command line:

| Command | What it does |
|---|---|
| `shipboard daemon` (alias: `start`) | run the daemon detached (reports if it's already running) |
| `shipboard stop` | SIGTERM to all daemon processes |
| `shipboard restart` | restart via systemd, else respawn detached |
| `shipboard setup` | interactive TUI: configure + write keybinds |
| `shipboard status` | print daemon / whisper state |
| `shipboard config` | interactive TOML editor |
| `shipboard --send` | one-shot paste clipboard + Enter |

## shipboard usage

- Hold **Pause**, speak, release → transcript lands in the clipboard
  (notifications: Запись → Обработка голоса → Скопировано).
- Tap **Scroll Lock** → the current clipboard is pasted and Enter is pressed.
- Hold **Pause** and press **Scroll Lock** (any order, ~150 ms window) →
  the transcript is pasted and sent automatically after recognition.
- With `key_record_mode = "toggle"`, press **Pause** to start recording and
  press again to stop and process.
- If `key_record_send` is set, holding it records and sends in one go
  (record → clipboard → paste + Enter).

Config (env vars or TOML):

| Option | Env var | Default | Meaning |
|---|---|---|---|
| `whisper_url` | `WHISPER_CPP_URL` | `http://127.0.0.1:10300/inference` | STT endpoint (wake proxy) |
| `whisper_health_url` | `WHISPER_CPP_HEALTH_URL` | `http://127.0.0.1:10300/health` | health endpoint |
| `whisper_container` | `WHISPER_CONTAINER` | `whisper-local` | container to wake |
| `whisper_language` | `SHIPBOARD_WHISPER_LANGUAGE` | `auto` | language hint sent to whisper (`auto`/`ru`/`en`/…) |
| `key_record` | `SHIPBOARD_KEY_RECORD` | `pause` | record trigger, evdev key name (see below); empty = trigger disabled |
| `key_send` | `SHIPBOARD_KEY_SEND` | `scrolllock` | send trigger, evdev key name; empty = trigger disabled |
| `key_record_send` | `SHIPBOARD_KEY_RECORD_SEND` | `""` | optional third trigger: record → whisper → clipboard → paste + Enter in one go |
| `key_record_mode` | `SHIPBOARD_KEY_RECORD_MODE` | `hold` | `hold` = record while held, `toggle` = press to start / press again to stop |
| `paste_combo` | `SHIPBOARD_PASTE_COMBO` | `ctrl+shift+v` | paste shortcut to inject (see key set below) |
| `send_enter` | `SHIPBOARD_SEND_ENTER` | `1` | global default: also press Enter after paste |
| `scroll_send_enter` | `SHIPBOARD_SCROLL_SEND_ENTER` | *(= send_enter)* | Scroll Lock tap: also press Enter after paste |
| `both_send_enter` | `SHIPBOARD_BOTH_SEND_ENTER` | *(= send_enter)* | both-keys send: also press Enter after paste |
| `max_hold` | `SHIPBOARD_MAX_HOLD` | `60` | single cap on recording length, seconds (stuck guard) |
| `min_recording` | `SHIPBOARD_MIN_RECORDING` | `0.5` | minimum recording seconds |
| `grace` | `SHIPBOARD_GRACE` | `0.15` | both-keys detection window (s) |
| `record_rate` | `SHIPBOARD_RECORD_RATE` | `16000` | pw-record sample rate (Hz) |
| `record_channels` | `SHIPBOARD_RECORD_CHANNELS` | `1` | pw-record channel count |
| `kws_threads` | `SHIPBOARD_KWS_THREADS` | `2` | wake-word (sherpa KWS) onnxruntime threads |
| `normalize` | `SHIPBOARD_NORMALIZE` | `1` | dictation normalization: spoken punctuation names become glued symbols (`тильда слэш точка конфиг` → `~/.config`) |

Key names are evdev names from `linux/input-event-codes.h` (e.g. `pause`,
`scrolllock`, `insert`, `home`, `f13`). Setting `key_record` / `key_send` /
`key_record_send` to an empty string disables that trigger.

`paste_combo` is a `+`-separated combo: one or more modifiers
(`ctrl`/`shift`/`alt`/`super`) followed by a final key from letters `a`–`z`,
digits `0`–`9`, `f1`–`f24`, or `v`, `insert`, `enter`, `space`, `tab`, `home`,
`end`, `pageup`, `pagedown`, `delete`, `backspace`.

All of these can also be set in the TOML config file generated by
`shipboard config` (see `shipboard/shipboard.toml.example`); env vars win.

## Hermes integration

`scripts/whisper_cpp_stt.py` is a ready STT provider client for
[Hermes Agent](https://hermes-agent.nousresearch.com). Wire it with:

```bash
./scripts/install_hermes_stt.sh
```

## Remote use (Tailscale, optional)

Want to transcribe from another machine (a laptop) using this computer's GPU?
The wake proxy can listen on the tailnet instead of localhost:

1. Copy `systemd/whisper-tailnet-proxy.service.example` to
   `~/.config/systemd/user/whisper-tailnet-proxy.service`, set
   `WHISPER_PROXY_HOST` to this machine's tailnet IP (`tailscale ip -4`), and
   enable it.
2. On the client machine, point the client at
   `http://<this-machine-ip>:<port>/inference` (e.g. set `whisper_url` in the
   shipboard TOML config).

The proxy wakes the container on demand, so the GPU stays free until a
request actually arrives.

## Troubleshooting

**Pressing Pause/Scroll Lock types garbage like `[57362u` into TUIs.**
If the key is not bound in the compositor, it reaches the focused app, and
kitty's keyboard protocol encodes it as `CSI <code>u` (57362 = Pause) which
some TUIs render as literal text. Bind the keys to a no-op so the compositor
swallows them — the daemon still sees them via evdev:

```kdl
// niri (~/.config/niri/conf/binds.kdl) — note KDL comments use //
Pause repeat=false { spawn "true"; }
Scroll_Lock repeat=false { spawn "true"; }
```

```ini
# Hyprland
bind = , Pause, exec, true
bind = , Scroll_Lock, exec, true
```

**The container doesn't wake.** Check the proxy is listening
(`curl 127.0.0.1:10300/health`) and that `whisper-wake-proxy.service` is
running (`systemctl --user status whisper-wake-proxy`).

**VRAM is still used after idle.** The idle-stop timer checks every minute;
the container stops after `WHISPER_IDLE_SECONDS` (default 300) of no requests.

## Credits

- [whisper.cpp](https://github.com/ggml-org/whisper.cpp) and its
  `ghcr.io/ggml-org/whisper.cpp:main-vulkan` image
- [Silero VAD](https://github.com/snakers4/silero-vad) for voice activity
  detection
- python-evdev for key handling and injection

## License

MIT
