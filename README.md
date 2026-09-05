# shipboard

On-demand local speech-to-text for Linux desktops, plus a voice input daemon
for agent TUIs.

A whisper.cpp server that **sleeps when idle** (frees ~1.5 GiB of VRAM) and
**wakes on the first request**, paired with `shipboard` — a compositor-agnostic
daemon that turns trigger keys (and optional wake words) into dictation:
record → whisper → clipboard → paste → Enter.

Everything runs locally — no cloud, no audio leaves your machine.

```
┌────────────┐  evdev   ┌──────────────┐  pw-record  ┌──────────────┐
│ trigger key│ ───────► │ shipboard   │ ──────────► │  audio.wav   │
│ / wake word│          │ (daemon)     │             └──────┬───────┘
└────────────┘          └──────┬───────┘                    ▼
                               │ wl-copy           ┌─────────────────┐
                               ▼                   │ whisper.cpp     │
                        ┌──────────────┐  HTTP     │ container       │
                        │  clipboard   │ ◄─────────┴────────┬────────┘
                        └──────┬───────┘   wakes on demand,
                               ▼           sleeps when idle
                        paste + Enter (send modes)
```

## How the pieces fit

| Piece | What it is | Where it lives |
|---|---|---|
| **shipboard daemon** | evdev key listener + optional sherpa-onnx wake words + pw-record capture + whisper HTTP client | installed Python package (`src/shipboard/`) |
| **whisper.cpp container** | `whisper-local` via docker compose, Vulkan build, `large-v3-turbo` | `docker-compose.yml` in this repo |
| **wake proxy** | tiny HTTP proxy: wakes the container on the first request, then relays | `scripts/whisper_wake_proxy.py` + `systemd/whisper-wake-proxy.service` |
| **idle-stop** | systemd timer, checks every minute, `docker stop` after 5 min of silence | `scripts/whisper_idle_stop.sh` + `systemd/whisper-idle-stop.{service,timer}` |

The daemon only needs the proxy's URL — it never talks to docker itself,
except for the on-demand `docker start` when it wakes the container directly.

## Requirements

- Linux with Docker and a GPU supported by whisper.cpp's Vulkan build
  (AMD/Intel/NVIDIA; the compose file exposes `/dev/dri/renderD128`)
- PipeWire (`pw-cat`/`pw-record`), `wl-clipboard`, `python-evdev`
- systemd user session (for the proxy/idle-stop units)

## Quick start

```bash
git clone git@github.com:nicklin11/shipboard.git ~/Coding/shipboard
cd ~/Coding/shipboard

# 1. Python package + `shipboard` command (editable install)
pip install --user -e .

# 2. STT backend: start the whisper container (downloads the model on first run)
docker compose up -d

# 3. systemd glue: wake proxy + idle-stop timer
./scripts/install.sh

# 4. Configure keys / STT / wake words (guided, TUI or numbered CLI)
shipboard setup

# 5. Run the daemon
shipboard daemon          # or: systemctl --user enable --now shipboard
```

## STT: the whisper container

The container is **not** running all the time:

1. The **wake proxy** (port 10300) accepts requests; the first request does
   `docker start whisper-local` and waits for `/health`, then relays.
2. Every request **touches an idle marker** (`/tmp/whisper-local-last-use`).
3. The **idle-stop timer** checks every minute: if the container is running
   and the marker is older than `WHISPER_IDLE_SECONDS` (default 300 s), it
   runs `docker stop`. Result: zero VRAM footprint while you're not talking.

So from the daemon's point of view STT is just two URLs:

```toml
whisper_url        = "http://127.0.0.1:10300/inference"
whisper_health_url = "http://127.0.0.1:10300/health"
whisper_container  = "whisper-local"
```

Tune the model via compose environment (`WHISPER_MODEL`, `WHISPER_LANGUAGE`,
`WHISPER_BEAM`, VAD knobs — see `docker-compose.yml`). Check health:
`curl 127.0.0.1:10300/health`.

### Remote use (Tailscale, optional)

To transcribe from another machine (e.g. a laptop) through this machine's
whisper: copy `systemd/whisper-tailnet-proxy.service.example` to
`~/.config/systemd/user/whisper-tailnet-proxy.service`, set
`WHISPER_PROXY_HOST` to this machine's tailnet IP, enable it, and point the
client's `whisper_url` at `http://<tailnet-ip>:<port>/inference`.

## Keys

Triggers are plain evdev keys read directly by the daemon (the compositor
only has to *swallow* them so they don't leak into apps — see
Troubleshooting). Keys are configured as `[[key_bind]]` tables in
`~/.config/shipboard/shipboard.toml`, or interactively in `shipboard setup`
(Keys screen — press the key you want to bind and it gets captured).

```toml
[[key_bind]]
key = "pause"            # evdev name, with or without the KEY_ prefix
tap = "record"           # short press  ("" = nothing)
hold = "record_send"     # held >= hold_threshold  ("" = nothing)
toggle = ""              # press-start / press-stop (overrides tap)
hold_threshold = 0.25    # seconds
```

| Field | Meaning |
|---|---|
| `key` | evdev key name: `pause`, `scrolllock`, `f13`, `rightalt`, … (`""` disables the binding) |
| `tap` | action on a short press: `record` / `record_send` / `paste` |
| `hold` | action when held past `hold_threshold` |
| `toggle` | action toggled by presses (overrides `tap` when set) |
| `hold_threshold` | tap vs hold boundary, seconds |

Actions: `record` → transcribe → clipboard; `record_send` → transcribe →
clipboard → paste (+Enter per the flags below); `paste` → paste current
clipboard (+Enter per `scroll_send_enter`).

**Tap = one-press dictation.** A tap starts recording with no release to
stop it, so the recording auto-finishes after `tap_stop_silence` seconds of
quiet (default: same as `wakeword_stop_silence`; `0` disables). Set it to 0
only if you want the old latch behaviour (tap again to stop).

Rules: 1–3 bindings, one action set per key, no overlapping keys.

### Enter after paste (three flags on purpose)

| Option | Meaning |
|---|---|
| `send_enter` | global default for every paste |
| `scroll_send_enter` | override for the `paste` action (tap/wake paste) |
| `both_send_enter` | override for `record_send` paths |

## Wake words

Optional hands-free trigger: a sherpa-onnx KWS listener starts recording when
it hears a phrase. Configured in `shipboard setup` (Wake words section) or
directly in the TOML:

```toml
wakeword_enabled = false
wakeword_record = "copy it, take it, grab it, catch it"     # → record
wakeword_send   = "push it, ship it, send it, drop it"      # → record_send
wakeword_paste  = "paste it, insert it, stick it"           # → paste
wakeword_sherpa_threshold = 0.2   # lower = easier to trigger
wakeword_grace = 3.0              # ignore silence right after a trigger
wakeword_stop_silence = 2.5       # seconds of silence end the recording
```

The listener lives in a separate venv (`~/.local/share/shipboard-venv`,
sherpa-onnx + numpy); models go to `~/.local/share/shipboard/models/`.

## CLI reference

| Command | What it does |
|---|---|
| `shipboard daemon` (alias `start`) | run the daemon detached (reports if already running) |
| `shipboard stop` | SIGTERM to all daemon processes |
| `shipboard restart` | restart via systemd if installed, else respawn detached |
| `shipboard status` | daemon / STT / keys / wake-word state |
| `shipboard setup` | numbered CLI dialog (sections: STT, Recording, Send, Keys, Wake words, Platform) |
| `shipboard tui` (alias `setup-tui`) | full-screen curses setup: `↑/↓` navigate · `Enter` edit · `s` save · `t` test STT · `p` compositor bind snippets · `r` restart daemon · `q` quit |
| `shipboard config` | interactive TOML editor |
| `shipboard --seconds N` | one-shot: record N seconds, transcribe |
| `shipboard --file PATH` | one-shot: transcribe an audio file |
| `shipboard --send` | one-shot: paste clipboard + Enter |
| `shipboard --no-copy` | with `--file`/`--seconds`: print instead of copying |

State lives in `~/.local/state/shipboard/state.json`; personal config in
`~/.config/shipboard/shipboard.toml` (created/edited by `setup`; never
committed). Every TOML option also has an env override — see the defaults at
the top of `src/shipboard/config.py`.

## Troubleshooting

**Pressing the trigger key types garbage like `[57362u` into TUIs.**
The compositor must swallow the keys (bind them to a no-op); the daemon still
sees them via evdev. `shipboard setup` / `shipboard tui` (`p`) prints the
snippets:

```kdl
// niri — KDL comments use //
Pause repeat=false { spawn "true"; }
Scroll_Lock repeat=false { spawn "true"; }
```

```ini
# Hyprland
bind = , Pause, exec, true
bind = , Scroll_Lock, exec, true
```

**The container doesn't wake.** Check the proxy: `curl 127.0.0.1:10300/health`
and `systemctl --user status whisper-wake-proxy`.

**VRAM is still used after idle.** The idle-stop timer fires every minute; the
container stops after `WHISPER_IDLE_SECONDS` (default 300) with no requests.
A manually started container always gets a 5-minute grace period first.

**Dictation gets cut mid-speech.** Check `tap_stop_silence` /
`wakeword_stop_silence` (silence windows) and `max_hold` (absolute cap)
against how long you actually pause.

## Credits

- [whisper.cpp](https://github.com/ggml-org/whisper.cpp) and its
  `ghcr.io/ggml-org/whisper.cpp:main-vulkan` image
- [Silero VAD](https://github.com/snakers4/silero-vad) for voice activity
  detection
- [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) for keyword spotting

## License

MIT
