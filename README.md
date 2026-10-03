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
| **whisper.cpp container** | `whisper-local` via docker compose, Vulkan build, `large-v3-turbo` | packaged asset `assets/docker-compose.yml`, started by `shipboard backend up` |
| **wake proxy** | tiny HTTP proxy: wakes the container on the first request, then relays | packaged asset `assets/scripts/whisper_wake_proxy.py` + `whisper-tailnet-proxy.service` |
| **idle-stop** | systemd timer, checks every minute, `docker stop` after 5 min of silence | packaged asset `assets/scripts/whisper_idle_stop.sh` + `whisper-idle-stop.{service,timer}` |

The daemon only needs the proxy's URL — it never talks to docker itself,
except for the on-demand `docker start` when it wakes the container directly.

Every deploy asset ships **inside the wheel** (`shipboard/assets/`), so
nothing depends on a checkout: `shipboard backend up` renders the units with
absolute installed paths and points compose at the packaged file.

## Requirements

- Linux with Docker and a GPU supported by whisper.cpp's Vulkan build
  (AMD/Intel/NVIDIA; the compose file exposes `/dev/dri/renderD128`)
- PipeWire (`pw-cat`/`pw-record`), `wl-clipboard`, `python-evdev`
- systemd user session (for the proxy/idle-stop units)

## Quick start

```bash
pipx install shipboard          # or: pip install shipboard

shipboard backend up            # container + volume + proxy unit + whisper_url
                                # (first run downloads the model into the volume)

shipboard setup                 # keys / STT / wake words (TUI, or --cli)
systemctl --user enable --now shipboard
```

`backend up` is idempotent: it creates the external volume if missing, starts
the container, waits on the healthcheck, installs and enables the proxy and
idle-stop units, and writes `whisper_url` / `whisper_health_url` into
`~/.config/shipboard/shipboard.toml` (add `--no-config` to skip that).
Working from a checkout instead? `pip install -e .` and the same commands apply.

```bash
shipboard backend status        # container / model / proxy / URL, one exit code
shipboard backend status --json # same, for scripts (glimpse doctor and friends)
```

## STT: the whisper container

The container is **not** running all the time:

1. The **wake proxy** (port 10301) accepts requests; the first request does
   `docker start whisper-local` and waits for `/health`, then relays.
2. Every request **touches an idle marker** (`/tmp/whisper-local-last-use`).
3. The **idle-stop timer** checks every minute: if the container is running
   and the marker is older than `WHISPER_IDLE_SECONDS` (default 300 s), it
   runs `docker stop`. Result: zero VRAM footprint while you're not talking.

So from the daemon's point of view STT is just two URLs:

```toml
whisper_url        = "http://100.64.0.1:10301/inference"
whisper_health_url = "http://100.64.0.1:10301/health"
whisper_container  = "whisper-local"
```

Tune the model via compose environment (`WHISPER_MODEL`, `WHISPER_LANGUAGE`,
`WHISPER_BEAM`, VAD knobs — see `shipboard/assets/docker-compose.yml`).
Check health directly: `curl 127.0.0.1:10302/health`.

### Remote use (Tailscale, optional)

`backend up` listens on this machine's tailnet IP when `tailscale` reports
one, so other devices can transcribe through your GPU; point their
`whisper_url` at `http://<tailnet-ip>:10301/inference`. Without a tailnet it
falls back to `127.0.0.1`. Pin it explicitly:

```bash
shipboard backend up --host 100.64.0.1 --port 10301
```

The unit is rewritten on every `backend up`; values that must survive that
belong in a drop-in:
`~/.config/systemd/user/whisper-tailnet-proxy.service.d/override.conf`.

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

**Tap = one-press dictation.** A tap — and likewise a quick press on a bind
with `toggle` set (toggle overrides `tap`, so that press *is* the toggle) —
starts recording with no release to stop it, so the recording auto-finishes
after `tap_stop_silence` seconds of quiet (default: same as
`wakeword_stop_silence`; `0` disables). Set it to 0 only if you want the
latch behaviour (press again to stop). The finish notification names its
trigger and duration — e.g. `Processing speech... (toggle Rightalt, 12s)` —
and says when a recording ran into `max_hold`.

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
| `shipboard process PATH` | transcribe an already-recorded file to stdout (`--copy` to clipboard) |
| `shipboard --seconds N` | one-shot: record N seconds, transcribe |
| `shipboard --file PATH` | one-shot: transcribe an audio file |
| `shipboard --send` | one-shot: paste clipboard + Enter |
| `shipboard --no-copy` | with `--file`/`--seconds`: print instead of copying |
| `shipboard --timestamps[=json]` | with `process`/`--file`: emit per-segment timings |

`process` is the one to reach for on anything already on disk — it writes to
stdout so it pipes into a file or another tool, never silently succeeds (a
failed transcription is a non-zero exit plus a message on stderr), and scales
its HTTP timeout to the audio length, so lectures are not cut off at the old
fixed 120 s. It also refreshes the whisper idle marker during the request, so
`whisper-idle-stop.timer` cannot SIGTERM the container mid-transcription.

```sh
shipboard process lecture.wav > lecture.md   # straight into a note
shipboard process lecture.wav --copy         # clipboard instead
```

### Segment timestamps

For anything that has to line audio up with something else — subtitles,
frame-to-paragraph binding, alignment:

```sh
shipboard process lecture.wav --timestamps          # [hh:mm:ss] text per line
shipboard process lecture.wav --timestamps json     # raw segments array
```

Times are **seconds** as floats under the hood, rendered as `[hh:mm:ss]`.
Stripping the `[hh:mm:ss] ` prefixes from the text form and re-joining with
spaces reproduces the normalized transcript. The `json` form additionally
carries per-word `start`/`end`, plus `tokens`, `temperature` and `avg_logprob`
per segment — which is what makes a downstream alignment check possible rather
than a guess.

Two implementation facts worth knowing, both measured rather than documented:

- whisper.cpp answers the **default** `response_format=json` with
  `{"text": ...}` and nothing else. Segments only appear under
  `verbose_json`, which shipboard requests exclusively on this path — the plain
  transcript request is byte-for-byte what it always was.
- A server or proxy that ignores `verbose_json` makes this path fail loudly
  with a message naming the format, rather than quietly returning a transcript
  with no timings attached.

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

**The container doesn't wake.** `shipboard backend status` says which link is
broken (container down / model absent / proxy unit inactive / URL
unreachable). Raw checks: `curl 127.0.0.1:10302/health` for the container and
`systemctl --user status whisper-tailnet-proxy` for the proxy.

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
