#!/usr/bin/env python3
"""alter-talk: voice daemon for Pause / Scroll Lock — record, copy, send.

Three modes:
  * Pause held ......... record -> whisper -> wl-copy (clipboard only)
  * Scroll Lock tap .... paste current clipboard + Enter (send only)
  * Pause + ScrollLock . record -> whisper -> wl-copy -> paste + Enter
                         (auto-send after processing)

WM-agnostic: the daemon watches evdev directly, so no compositor keybinds
are needed (and none should exist — a WM bind would double-fire). Runs via
compositor autostart (`spawn-at-startup "alter-talk"` / `exec-once`), guarded
by a single-instance flock.

Key injection uses a uinput virtual keyboard (python-evdev) — modifier combos
are layout-independent, so Cyrillic clipboard text pastes correctly in any
layout. Falls back to wtype if uinput is unavailable.

Requires: pw-record (PipeWire), python-evdev, wl-clipboard, whisper-local
container (see ~/.config/whisper-local/docker-compose.yml + whisper-wake-proxy).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

# --------------------------------------------------------------------------
# Configuration: defaults < TOML config file < environment variables
# --------------------------------------------------------------------------
KEY_PAUSE = 119       # linux/input-event-codes.h: KEY_PAUSE
KEY_SCROLLLOCK = 70   # KEY_SCROLLLOCK
KEY_V = 47            # KEY_V
KEY_INSERT = 110      # KEY_INSERT
KEY_ENTER = 28        # KEY_ENTER

DEFAULT_CONFIG_PATH = Path(
    "~/.config/whisper-local/alter-talk.toml"
).expanduser()
STATE_PATH = Path(
    os.environ.get(
        "ALTER_TALK_STATE", "~/.local/state/alter-talk/state.json"
    )
).expanduser()

DEFAULT_CONFIG_TEXT = """\
# alter-talk configuration
# Precedence: defaults < this file < environment variables (ALTER_TALK_*).

# Speech-to-text server (local wake proxy or a remote tailnet host)
whisper_url = "http://127.0.0.1:10300/inference"
whisper_health_url = "http://127.0.0.1:10300/health"
whisper_container = "whisper-local"

# Recording
max_hold = 60          # seconds; force-finish a stuck recording
min_recording = 0.5    # seconds; shorter recordings are discarded

# Send mode (Scroll Lock / both keys)
paste_combo = "ctrl+shift+v"   # injected as a modifier combo (layout-proof)
send_enter = true              # global default: also press Enter after pasting
scroll_send_enter = true       # Scroll Lock tap: Enter after paste
both_send_enter = true         # both keys held: Enter after paste

# Both-keys detection window (seconds): Scroll Lock pressed this long before
# Pause is still treated as "both held".
grace = 0.15

# Dictation normalization: "тильда слэш точка конфиг" -> "~/.config"
normalize = true

# Optional initial prompt sent to whisper (helps with domain vocabulary,
# e.g. "Короткие команды на русском и английском, термины: Docker, config").
prompt = ""

# Recording source for pw-record. "default" (or empty) = the system default
# source (EasyEffects chain etc.); a device name pins recording to it, e.g.:
#   record_target = "default"
#   record_target = "alsa_input.pci-0000_00_1f.3.analog-stereo"
record_target = "default"

# Wake word listener (OpenWakeWord engine — work in progress).
# Schema is ready; engine lands next. Keys keep working regardless.
wakeword_enabled = false
wakeword_model = "hey_jarvis"     # pretrained name or path to a .onnx
wakeword_sensitivity = 0.5        # 0..1 detection threshold
wakeword_cooldown = 2.0           # seconds between triggers
wakeword_max_record = 30.0        # cap for recording after a trigger
wakeword_stop_silence = 1.5       # seconds of silence end the recording
wakeword_action = "record"        # record (clipboard) | record_send (paste+Enter)
wakeword_silence_level = 500      # RMS below this counts as silence (0..32768)
"""


def _load_toml_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        import tomllib
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return {}


_CFG = _load_toml_config(DEFAULT_CONFIG_PATH)


def _cfg(name: str, env_name: str, default, conv=str):
    if env_name in os.environ:
        return conv(os.environ[env_name])
    if name in _CFG:
        return conv(_CFG[name])
    return default


def _as_bool(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "on")


WHISPER_URL = _cfg("whisper_url", "WHISPER_CPP_URL",
                   "http://127.0.0.1:10300/inference")
HEALTH_URL = _cfg("whisper_health_url", "WHISPER_CPP_HEALTH_URL",
                  "http://127.0.0.1:10300/health")
WHISPER_CONTAINER = _cfg("whisper_container", "WHISPER_CONTAINER",
                         "whisper-local")
IDLE_MARKER = Path(_cfg("idle_marker", "WHISPER_IDLE_MARKER",
                        "/tmp/whisper-local-last-use"))
MAX_HOLD = _cfg("max_hold", "ALTER_TALK_MAX_HOLD", 60.0, float)
MIN_RECORDING = _cfg("min_recording", "ALTER_TALK_MIN_RECORDING", 0.5, float)
GRACE = _cfg("grace", "ALTER_TALK_GRACE", 0.15, float)  # SL->Pause window
RATE = 16000
CHANNELS = 1
LOCK_PATH = Path(_cfg("lock_path", "ALTER_TALK_LOCK", "/tmp/alter-talk.lock"))
DAEMON_LOCK_PATH = Path(_cfg("daemon_lock_path", "ALTER_TALK_DAEMON_LOCK",
                             "/tmp/alter-talk.daemon.lock"))
PASTE_COMBO = _cfg("paste_combo", "ALTER_TALK_PASTE_COMBO",
                   "ctrl+shift+v", str.lower)
SEND_ENTER = _cfg("send_enter", "ALTER_TALK_SEND_ENTER", True, _as_bool)
# Per-trigger "Enter after paste" (fall back to the global send_enter)
SCROLL_SEND_ENTER = _cfg("scroll_send_enter", "ALTER_TALK_SCROLL_SEND_ENTER",
                         SEND_ENTER, _as_bool)
BOTH_SEND_ENTER = _cfg("both_send_enter", "ALTER_TALK_BOTH_SEND_ENTER",
                       SEND_ENTER, _as_bool)
# Wake word listener (engine WIP — config schema is ready)
WAKEWORD_ENABLED = _cfg("wakeword_enabled", "ALTER_TALK_WAKEWORD_ENABLED",
                        False, _as_bool)
WAKEWORD_MODEL = _cfg("wakeword_model", "ALTER_TALK_WAKEWORD_MODEL",
                      "hey_jarvis")
WAKEWORD_SENSITIVITY = _cfg("wakeword_sensitivity",
                            "ALTER_TALK_WAKEWORD_SENSITIVITY", 0.5, float)
WAKEWORD_COOLDOWN = _cfg("wakeword_cooldown", "ALTER_TALK_WAKEWORD_COOLDOWN",
                         2.0, float)
WAKEWORD_MAX_RECORD = _cfg("wakeword_max_record",
                           "ALTER_TALK_WAKEWORD_MAX_RECORD", 30.0, float)
WAKEWORD_STOP_SILENCE = _cfg("wakeword_stop_silence",
                             "ALTER_TALK_WAKEWORD_STOP_SILENCE", 1.5, float)
WAKEWORD_ACTION = _cfg("wakeword_action", "ALTER_TALK_WAKEWORD_ACTION",
                       "record")
WAKEWORD_SILENCE_LEVEL = _cfg("wakeword_silence_level",
                              "ALTER_TALK_WAKEWORD_SILENCE_LEVEL", 500, float)
DRY_RUN = _cfg("dry_run", "ALTER_TALK_DRY_RUN", False, _as_bool)
NORMALIZE = _cfg("normalize", "ALTER_TALK_NORMALIZE", True, _as_bool)
PROMPT = _cfg("prompt", "ALTER_TALK_PROMPT", "")
KEEP_AUDIO_DIR = Path(_cfg(
    "keep_audio_dir", "ALTER_TALK_KEEP_AUDIO_DIR", ""
)).expanduser() if _cfg("keep_audio_dir", "ALTER_TALK_KEEP_AUDIO_DIR", "") else None
# "default"/"auto"/"" = record from the system default source (PipeWire
# picks it, e.g. the EasyEffects chain); any other value = explicit device.
RECORD_TARGET = _cfg("record_target", "ALTER_TALK_RECORD_TARGET", "")
if RECORD_TARGET.lower() in ("default", "auto", "system"):
    RECORD_TARGET = ""

_COMBO_KEYS = {
    "ctrl": 29,        # KEY_LEFTCTRL
    "shift": 42,       # KEY_LEFTSHIFT
    "alt": 56,         # KEY_LEFTALT
    "super": 125,      # KEY_LEFTMETA
    "v": KEY_V,
    "insert": KEY_INSERT,
    "enter": KEY_ENTER,
}


def _notify(title: str, msg: str) -> None:
    try:
        subprocess.run(
            ["notify-send", "-a", "alter-talk", title, msg],
            timeout=5,
            capture_output=True,
        )
    except Exception:
        pass


# --------------------------------------------------------------------------
# Recording (PipeWire)
# --------------------------------------------------------------------------
def _log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except Exception:
        pass


def start_recording(path: Path) -> subprocess.Popen:
    cmd = ["pw-record", "--rate", str(RATE), "--channels", str(CHANNELS)]
    if RECORD_TARGET:
        cmd += ["--target", RECORD_TARGET]
    cmd.append(str(path))
    return subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_recording(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


# --------------------------------------------------------------------------
# Wake word listener (OpenWakeWord, streaming via parec)
# --------------------------------------------------------------------------
_WAKE_MODELS_DIR = Path.home() / ".local/share/alter-talk/models"
_WAKE_VENV = Path.home() / ".local/share/alter-talk-venv"
_WAKE_SILENCE_RMS = WAKEWORD_SILENCE_LEVEL / 32768.0


def _wake_model_path(name: str) -> Path:
    p = Path(name).expanduser()
    if p.is_file():
        return p
    if "/" in name or name.endswith(".onnx"):
        return p
    return _WAKE_MODELS_DIR / f"{name}.onnx"


def _ensure_wake_model(path: Path, name: str) -> bool:
    if path.is_file() and path.stat().st_size > 100_000:
        return True
    _WAKE_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    url = (f"https://github.com/dscripka/openWakeWord/releases/"
           f"download/v0.5.1/{name}_v0.1.onnx")
    _log(f"wakeword: downloading {name} model ...")
    try:
        tmp = path.with_suffix(".onnx.tmp")
        urllib_request.urlretrieve(url, tmp)
        tmp.replace(path)
        return path.stat().st_size > 100_000
    except Exception as exc:
        _log(f"wakeword: model download failed: {exc}")
        return False


def _wake_listen(self, stop_event: threading.Event) -> None:
    """Continuous listener: parec -> OpenWakeWord -> record until silence."""
    # OpenWakeWord lives in the alter-talk venv; make it importable from
    # the system python the daemon runs under.
    try:
        for sp in (_WAKE_VENV / "lib").glob("python*/site-packages"):
            sys.path.insert(0, str(sp))
        import numpy as np
        from openwakeword.model import Model
    except Exception as exc:
        _log(f"wakeword: openwakeword not available ({exc}) — listener off")
        return
    path = _wake_model_path(WAKEWORD_MODEL)
    if not _ensure_wake_model(path, WAKEWORD_MODEL):
        _log("wakeword: no model — listener off")
        return
    try:
        oww = Model(wakeword_model_paths=[str(path)])
        model_key = path.stem
    except Exception as exc:
        _log(f"wakeword: model load failed: {exc}")
        return

    cmd = ["parec", "--rate", "16000", "--channels", "1", "--format", "s16le"]
    if RECORD_TARGET:
        cmd += ["--device", RECORD_TARGET]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        _log(f"wakeword: parec failed: {exc}")
        return
    import atexit
    atexit.register(proc.terminate)
    _log(f"wakeword: listening ({WAKEWORD_MODEL}, sens {WAKEWORD_SENSITIVITY})")
    _notify("alter-talk", f"Wake word on: {WAKEWORD_MODEL}")

    last_trigger = 0.0
    silence_since: float | None = None
    try:
        while not stop_event.is_set():
            raw = proc.stdout.read(2560)  # 1280 samples = 80 ms
            if not raw:
                break
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            now = time.monotonic()
            rms = float(np.sqrt(np.mean(audio ** 2)))

            # While a wake-triggered recording is running: stop on silence
            # or the max-record cap.
            if self.recording and self.wake_rec:
                if rms < _WAKE_SILENCE_RMS:
                    if silence_since is None:
                        silence_since = now
                    elif now - silence_since >= WAKEWORD_STOP_SILENCE:
                        _log("wakeword: silence — finishing recording")
                        self._finish_record()
                        self.wake_rec = False
                        silence_since = None
                else:
                    silence_since = None
                if now - self.rec_t0 > WAKEWORD_MAX_RECORD:
                    _log("wakeword: max record — finishing")
                    self._finish_record()
                    self.wake_rec = False
                continue

            if self.recording or now - last_trigger < WAKEWORD_COOLDOWN:
                continue
            score = float(oww.predict(audio).get(model_key, 0.0))
            if score >= WAKEWORD_SENSITIVITY:
                last_trigger = now
                _log(f"wakeword: DETECTED ({score:.2f})")
                _notify("alter-talk", "Wake word detected")
                self.wake_rec = True
                self.autosend = WAKEWORD_ACTION == "record_send"
                self._start_record()
                silence_since = None
    except (OSError, ValueError):
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
    _log("wakeword: listener stopped")


# --------------------------------------------------------------------------
# Whisper.cpp (docker) transcription
# --------------------------------------------------------------------------
def _multipart_body(fields: dict[str, str], wav_path: Path) -> tuple[bytes, str]:
    boundary = f"----alter-talk-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n',
            b"Content-Type: audio/wav\r\n\r\n",
            wav_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _server_healthy() -> bool:
    try:
        with urllib_request.urlopen(HEALTH_URL, timeout=2) as resp:
            return 200 <= resp.status < 300
    except (OSError, urllib_error.URLError):
        return False


def _ensure_server(timeout: float = 60.0) -> None:
    """Touch the idle marker and wake the container if needed."""
    IDLE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    IDLE_MARKER.touch()
    if not _server_healthy():
        try:
            subprocess.run(
                ["docker", "start", WHISPER_CONTAINER],
                capture_output=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _server_healthy():
            return
        time.sleep(0.5)
    raise RuntimeError(f"whisper.cpp did not come up at {HEALTH_URL}")


def transcribe(wav_path: Path) -> str:
    _ensure_server()
    fields = {"language": "auto"}
    if PROMPT:
        fields["prompt"] = PROMPT
    body, content_type = _multipart_body(fields, wav_path)
    req = urllib_request.Request(
        WHISPER_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=120) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"whisper.cpp HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"whisper.cpp unavailable: {exc.reason}") from exc

    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"whisper.cpp returned non-JSON: {payload[:300]}") from exc

    text = result.get("text") if isinstance(result, dict) else result
    if not isinstance(text, str):
        raise RuntimeError(f"whisper.cpp: no text in response: {payload[:300]}")
    return text.strip()


# --------------------------------------------------------------------------
# Dictation normalization: spoken punctuation names -> symbols, glued.
# "тильда слэш точка конфиг" -> "~/.config", "alt dash talk" -> "alt-talk".
# --------------------------------------------------------------------------
_SYMBOL_MAP = {
    # Russian
    "слэш": "/", "слеш": "/", "точка": ".", "тильда": "~",
    "дефис": "-", "минус": "-", "запятая": ",", "точка с запятой": ";",
    "двоеточие": ":", "равно": "=", "амперсанд": "&", "процент": "%",
    "собака": "@", "нижнее подчёркивание": "_", "подчёркивание": "_",
    "звёздочка": "*", "решётка": "#", "решетка": "#", "плюс": "+",
    "вопрос": "?", "восклицание": "!", "кавычка": '"',
    "открывающая скобка": "(", "закрывающая скобка": ")",
    "апостроф": "'", "пробел": " ",
    # dash variants: whisper often transliterates these ("desh"/"defiz"/"tire")
    "дэш": "-", "деш": "-", "desh": "-", "defiz": "-",
    "тире": "-", "tire": "-", "tireh": "-",
    "слэж": "/",
    # English
    "slash": "/", "dot": ".", "tilde": "~", "tilda": "~", "dash": "-", "hyphen": "-",
    "comma": ",", "colon": ":", "semicolon": ";", "equals": "=",
    "equal": "=", "ampersand": "&", "percent": "%", "at": "@",
    "underscore": "_", "asterisk": "*", "star": "*", "hash": "#",
    "question mark": "?", "exclamation mark": "!", "exclamation": "!",
    "plus": "+", "minus": "-", "quote": '"', "apostrophe": "'",
    "open paren": "(", "close paren": ")", "space": " ",
}
_TOKEN_RE = re.compile(
    r"\b(" + "|".join(
        re.escape(tok) for tok in sorted(_SYMBOL_MAP, key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
)
# whisper sometimes glues the spoken word to its neighbor ("deshtag",
# "configdefizfile") — replace the token even as a word prefix then.
_GLUE_DASH_RE = re.compile(
    r"(?i)(?:дэш|деш|desh|defiz|тире|tire|tireh)(?=[a-zа-яё0-9])"
)


def normalize_text(text: str) -> str:
    """Spoken punctuation names -> symbols; command symbols glue, sentence
    punctuation keeps normal spacing (\"Привет. Это\" vs \"~/.config\")."""
    if not NORMALIZE or not text:
        return text
    # whisper returns segments joined with newlines (VAD cuts the speech);
    # the result must be a single line
    text = re.sub(r"\s+", " ", text)
    # whisper sometimes splits a spoken word ("сл эш" for "слэш") —
    # stitch the known splits back together
    text = text.replace("сл эш", "слэш")
    text = _TOKEN_RE.sub(lambda m: _SYMBOL_MAP[m.group(1).lower()], text)
    text = _GLUE_DASH_RE.sub("-", text)
    # the model often writes hyphens around the spoken word; collapse the
    # resulting runs of 3+ (keep "--" — legitimate flag prefix)
    text = re.sub(r"-{3,}", "--", text)
    # command symbols (paths, flags, URLs): never surrounded by spaces
    text = re.sub(r"\s*([~/_\-@#*+=:])\s*", r"\1", text)
    # dot: glued before a lowercase letter/digit ("файл.пи", "~/.config"),
    # sentence spacing otherwise ("Привет. Это")
    text = re.sub(r"\.\s+(?=[a-zа-яё0-9])", ".", text)
    text = re.sub(r"\s+\.", ".", text)
    # sentence punctuation: no space before, single space after
    text = re.sub(r"([,;!?])\s+", r"\1 ", text)
    text = re.sub(r"\s+([,;!?])", r"\1", text)
    return text.strip()


# --------------------------------------------------------------------------
# Clipboard
# --------------------------------------------------------------------------
def copy_to_clipboard(text: str) -> None:
    env = os.environ.copy()
    if not env.get("WAYLAND_DISPLAY"):
        for cand in ("wayland-1", "wayland-0"):
            sock = Path(f"/run/user/{os.getuid()}/{cand}")
            if sock.exists():
                env["WAYLAND_DISPLAY"] = cand
                break
    proc = subprocess.run(
        ["wl-copy"], input=text.encode("utf-8"), env=env, timeout=10
    )
    if proc.returncode != 0:
        raise RuntimeError("wl-copy failed (Wayland session up?)")


# --------------------------------------------------------------------------
# Key injection (uinput; wtype fallback)
# --------------------------------------------------------------------------
def _inject_via_wtype(combo: str, enter: bool) -> bool:
    wtype = shutil.which("wtype")
    if not wtype:
        return False
    parts = combo.split("+")
    if not parts or parts[-1] not in ("v", "insert"):
        return False
    cmd = [wtype]
    for mod in parts[:-1]:
        cmd += ["-M", mod]
    cmd.append(parts[-1])
    if enter:
        cmd += ["-m", parts[-1], "Return"]
    try:
        subprocess.run(cmd, timeout=5, capture_output=True)
        return True
    except Exception:
        return False


def _inject_uinput(combo: str, enter: bool) -> None:
    from evdev import UInput, ecodes

    parts = combo.split("+")
    if len(parts) < 2 or parts[-1] not in ("v", "insert", "enter"):
        raise ValueError(f"unsupported combo: {combo!r}")
    mods = []
    for part in parts[:-1]:
        code = _COMBO_KEYS.get(part)
        if code is None:
            raise ValueError(f"unknown key in combo: {part!r}")
        mods.append(code)
    key = _COMBO_KEYS[parts[-1]]

    keys = set(mods) | {key} | ({KEY_ENTER} if enter else set())
    ui = UInput(
        {ecodes.EV_KEY: list(keys)},
        name="alter-talk",
        phys="alter-talk",
    )
    try:
        delay = 0.03
        for mod in mods:
            ui.write(ecodes.EV_KEY, mod, 1)
            ui.syn()
        ui.write(ecodes.EV_KEY, key, 1)
        ui.syn()
        time.sleep(delay)
        ui.write(ecodes.EV_KEY, key, 0)
        ui.syn()
        for mod in reversed(mods):
            ui.write(ecodes.EV_KEY, mod, 0)
            ui.syn()
        time.sleep(delay)
        if enter:
            ui.write(ecodes.EV_KEY, KEY_ENTER, 1)
            ui.syn()
            time.sleep(delay)
            ui.write(ecodes.EV_KEY, KEY_ENTER, 0)
            ui.syn()
    finally:
        ui.close()


def send_keys(combo: str = PASTE_COMBO, enter: bool = SEND_ENTER) -> None:
    if DRY_RUN:
        print(f"[dry-run] send combo={combo!r} enter={enter}")
        return
    try:
        _inject_uinput(combo, enter)
    except Exception:
        if not _inject_via_wtype(combo, enter):
            raise RuntimeError("failed to inject keys (uinput unavailable, no wtype)")


# --------------------------------------------------------------------------
# evdev device discovery
# --------------------------------------------------------------------------
def _watch_devices():
    import evdev

    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities(verbose=False)
            keys = caps.get(evdev.ecodes.EV_KEY, [])
            if KEY_PAUSE in keys or KEY_SCROLLLOCK in keys:
                devices.append(dev)
        except Exception:
            continue
    return devices


# --------------------------------------------------------------------------
# Recording cycle (shared by daemon and one-shot modes)
# --------------------------------------------------------------------------
def run_record_cycle(autosend: bool, seconds: float = 0.0) -> int:
    """Record -> transcribe -> copy; auto-send if autosend. Returns exit code."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="alter-talk-"))
    try:
        wav_path = tmp_dir / "rec.wav"
        _notify(
            "alter-talk",
            "Recording... (hold Pause)"
            + (" — release: will paste and send" if autosend else ""),
        )
        proc = start_recording(wav_path)
        t0 = time.monotonic()
        if seconds > 0:
            time.sleep(seconds)
        else:
            import evdev

            deadline = time.monotonic() + MAX_HOLD
            live = _watch_devices()
            held = False
            while time.monotonic() < deadline:
                if not live:
                    break
                r, _, _ = select.select(live, [], [], 0.1)
                for dev in r:
                    try:
                        for event in dev.read():
                            if (
                                event.type == evdev.ecodes.EV_KEY
                                and event.code == KEY_PAUSE
                                and event.value == 0  # release
                            ):
                                held = True
                                break
                    except (OSError, ValueError):
                        live.remove(dev)
                if held:
                    break
        stop_recording(proc)
        duration = time.monotonic() - t0
        if duration < MIN_RECORDING:
            _notify("alter-talk", "Recording too short")
            return 0
        if not wav_path.is_file() or wav_path.stat().st_size == 0:
            _notify("alter-talk", "Error: empty recording file")
            return 1

        _notify("alter-talk", "Processing speech...")
        try:
            text = transcribe(wav_path)
        except Exception as exc:
            _notify("alter-talk", f"Recognition error: {exc}")
            print(f"alter-talk: {exc}", file=sys.stderr)
            return 1
        text = normalize_text(text)

        if not text:
            _notify("alter-talk", "Nothing recognized")
            return 0

        try:
            copy_to_clipboard(text)
        except Exception as exc:
            _notify("alter-talk", f"Failed to copy: {exc}")
            print(f"alter-talk: {exc}", file=sys.stderr)
            return 1
        preview = text if len(text) <= 100 else text[:100] + "…"

        if autosend:
            try:
                send_keys(enter=BOTH_SEND_ENTER)
            except Exception as exc:
                _notify("alter-talk", f"Copied, but not sent: {exc}")
                return 1
            _notify("alter-talk", f"Sent: {preview}")
        else:
            _notify("alter-talk", f"Copied: {preview}")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# Daemon
# --------------------------------------------------------------------------
class _Daemon:
    def __init__(self) -> None:
        self.recording = False
        self.rec_proc: subprocess.Popen | None = None
        self.rec_t0 = 0.0
        self.autosend = False
        self.grace_deadline: float | None = None  # SL tap waiting for Pause
        self.pause_down = False
        self.wake_rec = False  # recording started by the wake word listener

    def _start_record(self) -> None:
        if self.recording:
            return
        self.autosend = False
        self.grace_deadline = None
        # Cycle lock: prevents a concurrent one-shot --send from pasting
        # stale content while this recording is in flight.
        self._cycle_lock = open(LOCK_PATH, "w")
        try:
            fcntl.flock(self._cycle_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._cycle_lock.close()
            self._cycle_lock = None
            return
        tmp_dir = Path(tempfile.mkdtemp(prefix="alter-talk-"))
        wav = tmp_dir / "rec.wav"
        self.rec_proc = start_recording(wav)
        self.recording = True
        self.rec_t0 = time.monotonic()
        self._tmp_dir = tmp_dir
        _write_state(state="recording")
        _log(f"record start -> {wav}")
        _notify("alter-talk", "Recording... (hold Pause)")

    def _finish_record(self) -> None:
        if not self.recording or self.rec_proc is None:
            return
        stop_recording(self.rec_proc)
        self.recording = False
        duration = time.monotonic() - self.rec_t0
        autosend = self.autosend
        self.autosend = False
        cycle_lock = getattr(self, "_cycle_lock", None)
        self._cycle_lock = None
        wav = Path(self._tmp_dir) / "rec.wav"
        try:
            if duration < MIN_RECORDING:
                _notify("alter-talk", "Recording too short")
                return
            if not wav.is_file() or wav.stat().st_size == 0:
                _notify("alter-talk", "Error: empty recording file")
                return
            if KEEP_AUDIO_DIR is not None:
                KEEP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy(wav, KEEP_AUDIO_DIR / f"rec-{int(time.time())}.wav")
            _notify("alter-talk", "Processing speech...")
            _write_state(state="processing")
            try:
                text = transcribe(wav)
                _log(f"stt ok ({len(text)} chars)")
            except Exception as exc:
                _log(f"stt error: {exc}")
                _write_state(state="error", text=str(exc)[:200])
                _notify("alter-talk", f"Recognition error: {exc}")
                return
            text = normalize_text(text)
            if not text:
                _write_state(state="idle")
                _notify("alter-talk", "Nothing recognized")
                return
            try:
                copy_to_clipboard(text)
            except Exception as exc:
                _write_state(state="error", text=str(exc)[:200])
                _notify("alter-talk", f"Failed to copy: {exc}")
                return
            preview = text if len(text) <= 100 else text[:100] + "…"
            _log(f"result: {preview!r}")
            if autosend:
                try:
                    send_keys(enter=BOTH_SEND_ENTER)
                except Exception as exc:
                    _write_state(state="error", text=str(exc)[:200])
                    _notify("alter-talk", f"Copied, but not sent: {exc}")
                    return
                _write_state(state="sent", text=preview)
                _notify("alter-talk", f"Sent: {preview}")
            else:
                _write_state(state="copied", text=preview)
                _notify("alter-talk", f"Copied: {preview}")
        finally:
            try:
                shutil.rmtree(self._tmp_dir, ignore_errors=True)
            except AttributeError:
                pass
            if cycle_lock is not None:
                try:
                    cycle_lock.close()
                except OSError:
                    pass

    def _on_pause(self, value: int) -> None:
        if value == 1:  # press
            if self.recording:
                return
            # Was a Scroll Lock tap waiting in grace? Then this is both-held.
            was_grace = self.grace_deadline is not None
            self.grace_deadline = None
            self._start_record()
            if was_grace:
                self.autosend = True
        elif value == 0:  # release
            self.pause_down = False
            self._finish_record()

    def _on_scrolllock(self, value: int) -> None:
        if value == 1:  # press
            if self.recording:
                # Both held: mark auto-send, do NOT paste old clipboard.
                self.autosend = True
                self.grace_deadline = None
                return
            if self.grace_deadline is not None:
                return  # already waiting
            self.grace_deadline = time.monotonic() + GRACE
        # release (0) and repeat (2): nothing

    def run(self) -> None:
        import evdev

        _write_state(state="running", pid=os.getpid())
        stop_event = threading.Event()
        if WAKEWORD_ENABLED:
            threading.Thread(
                target=_wake_listen, args=(self, stop_event), daemon=True
            ).start()
        devices = _watch_devices()
        if not devices:
            _notify("alter-talk", "Daemon: Pause/ScrollLock keys not found on evdev")
        while True:
            now = time.monotonic()
            # Grace expiry: no Pause followed the Scroll Lock tap -> send.
            if self.grace_deadline is not None and now >= self.grace_deadline:
                self.grace_deadline = None
                try:
                    send_keys(enter=SCROLL_SEND_ENTER)
                except Exception as exc:
                    _notify("alter-talk", f"Failed to send: {exc}")
            # MAX_HOLD safety: force-finish a stuck recording.
            if self.recording and now - self.rec_t0 > MAX_HOLD:
                self._finish_record()

            if not devices:
                time.sleep(0.5)
                devices = _watch_devices()
                continue
            r, _, _ = select.select(devices, [], [], 0.05)
            for dev in r:
                try:
                    for event in dev.read():
                        if event.type != evdev.ecodes.EV_KEY:
                            continue
                        if event.value == 2:  # auto-repeat
                            continue
                        if event.code == KEY_PAUSE:
                            if event.value == 1:
                                self.pause_down = True
                            self._on_pause(event.value)
                        elif event.code == KEY_SCROLLLOCK:
                            self._on_scrolllock(event.value)
                except (OSError, ValueError):
                    devices = [d for d in devices if d != dev]
                    try:
                        dev.close()
                    except Exception:
                        pass


# --------------------------------------------------------------------------
# State + status/config helpers
# --------------------------------------------------------------------------
def _write_state(**fields: object) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {"ts": int(time.time())}
        data.update(fields)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(STATE_PATH)
    except Exception:
        pass


def _daemon_running() -> bool:
    try:
        fh = open(DAEMON_LOCK_PATH, "w")
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        fh.close()


def _status_main() -> int:
    print(f"daemon:    {'running' if _daemon_running() else 'stopped'}")
    print(f"config:    {DEFAULT_CONFIG_PATH}")
    print(f"stt:       {WHISPER_URL}")
    print("keys:      Pause = record->clipboard | Scroll Lock = paste+Enter |"
          " both = record->paste+Enter")
    print(f"paste:     {PASTE_COMBO} | Scroll Lock Enter: "
          f"{'yes' if SCROLL_SEND_ENTER else 'no'} | both Enter: "
          f"{'yes' if BOTH_SEND_ENTER else 'no'}"
          f" | grace {GRACE}s | max hold {MAX_HOLD}s | min rec {MIN_RECORDING}s")
    print(f"normalize: {'on' if NORMALIZE else 'off'}")
    print(f"wakeword:  {'on (' + WAKEWORD_MODEL + ', ' + WAKEWORD_ACTION + ')' if WAKEWORD_ENABLED else 'off'}")
    if RECORD_TARGET:
        src = RECORD_TARGET
    else:
        try:
            out = subprocess.run(["pactl", "get-default-source"],
                                 capture_output=True, text=True, timeout=3)
            src = f"default ({out.stdout.strip()})"
        except Exception:
            src = "default (system)"
    print(f"source:    {src}")
    if STATE_PATH.is_file():
        try:
            st = json.loads(STATE_PATH.read_text())
            when = time.strftime("%H:%M:%S", time.localtime(st.get("ts", 0)))
            print(f"last:      {st.get('state', '?')} @ {when}")
            if st.get("text"):
                print(f"           {st['text']}")
        except Exception:
            pass
    return 0


def _config_main() -> int:
    if not DEFAULT_CONFIG_PATH.is_file():
        DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_CONFIG_PATH.write_text(DEFAULT_CONFIG_TEXT)
        print(f"created:   {DEFAULT_CONFIG_PATH}")
        print("edit it, then restart the daemon")
    else:
        print(f"config:    {DEFAULT_CONFIG_PATH}")
    print(f"stt:       {WHISPER_URL}")
    print(f"paste:     {PASTE_COMBO}{' + Enter' if SEND_ENTER else ''}")
    print(f"recording: max_hold {MAX_HOLD}s, min {MIN_RECORDING}s,"
          f" grace {GRACE}s")
    print(f"normalize: {'on' if NORMALIZE else 'off'}")
    return 0


# --------------------------------------------------------------------------
# TUI setup (stdlib curses, no dependencies)
# --------------------------------------------------------------------------
_SETUP_FIELDS = [
    ("whisper_url",        "STT server URL (wake/tailnet proxy)", str),
    ("whisper_health_url", "Health URL",                          str),
    ("whisper_container",  "Docker container to wake",           str),
    ("record_target",      "Record source (default=system)",    str),
    ("paste_combo",        "Paste shortcut (uinput combo)",      str),
    ("send_enter",         "Enter after paste (global default)", bool),
    ("scroll_send_enter",  "Scroll Lock tap: Enter after paste", bool),
    ("both_send_enter",    "Both keys: Enter after paste",       bool),
    ("max_hold",           "Max hold, seconds (stuck guard)",    float),
    ("min_recording",      "Min recording, seconds",             float),
    ("grace",              "Both-keys window, seconds",          float),
    ("normalize",          "Dictation symbols (слэш/дэш/...) ",  bool),
    ("prompt",             "Initial whisper prompt (optional)",  str),
    ("wakeword_enabled",   "Wake word listener (engine WIP)",    bool),
    ("wakeword_model",     "Wake word model (pretrained)",       str),
    ("wakeword_sensitivity", "Wake word sensitivity (0..1)",     float),
    ("wakeword_cooldown",  "Wake word cooldown, seconds",        float),
    ("wakeword_max_record", "Wake word max record, seconds",     float),
    ("wakeword_stop_silence", "Wake word stop on silence, s",    float),
    ("wakeword_action",    "Wake word action (record/record_send)", str),
    ("wakeword_silence_level", "Wake word silence RMS level",    float),
]


def _field_defaults() -> dict:
    return {
        "whisper_url": "http://127.0.0.1:10300/inference",
        "whisper_health_url": "http://127.0.0.1:10300/health",
        "whisper_container": "whisper-local",
        "record_target": "default",
        "paste_combo": "ctrl+shift+v",
        "send_enter": True,
        "scroll_send_enter": True,
        "both_send_enter": True,
        "max_hold": 60.0,
        "min_recording": 0.5,
        "grace": 0.15,
        "normalize": True,
        "prompt": "",
        "wakeword_enabled": False,
        "wakeword_model": "hey_jarvis",
        "wakeword_sensitivity": 0.5,
        "wakeword_cooldown": 2.0,
        "wakeword_max_record": 30.0,
        "wakeword_stop_silence": 1.5,
        "wakeword_action": "record",
        "wakeword_silence_level": 500.0,
    }


def _fmt_value(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _parse_value(raw: str, conv) -> object:
    if conv is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on", "y")
    return conv(raw.strip())


def _save_config_file(values: dict) -> None:
    lines = [
        "# alter-talk configuration (edited with 'alter-talk setup')",
        "# Precedence: defaults < this file < environment variables.",
        "",
    ]
    for key, _label, conv in _SETUP_FIELDS:
        v = values[key]
        if conv is str:
            lines.append(f'{key} = "{v}"')
        elif conv is bool:
            lines.append(f"{key} = {'true' if v else 'false'}")
        else:
            lines.append(f"{key} = {v}")
    lines.append("")
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_CONFIG_PATH.write_text("\n".join(lines))


def _health_check(url: str) -> str:
    try:
        with urllib_request.urlopen(url, timeout=3) as resp:
            return "OK" if 200 <= resp.status < 300 else f"HTTP {resp.status}"
    except Exception as exc:
        return f"FAIL: {exc}"


def _daemon_pids() -> list[int]:
    import subprocess as sp

    # Match only the daemon python process (not bash wrappers / other CLIs).
    out = sp.run(["pgrep", "-f", "python3 .*alter-talk"],
                 capture_output=True, text=True)
    pids = []
    for pid in out.stdout.split():
        try:
            pid = int(pid)
        except ValueError:
            continue
        if pid == os.getpid():
            continue  # never kill our own CLI process
        pids.append(pid)
    return pids


def _restart_daemon() -> str:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "alter-talk"],
            capture_output=True, timeout=15,
        )
        if r.returncode == 0:
            return "restarted via systemd"
    except Exception:
        pass
    for pid in _daemon_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.4)  # let the old daemon release its flock
    subprocess.Popen(
        [sys.executable, str(Path(sys.argv[0]).resolve())],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return "restarted (detached)"


_COMPOSITOR_SNIPPETS = """\
Bind the keys to a no-op in your compositor so they don't leak into apps
(kitty encodes unbound keys as CSI-u text). The daemon reads them via evdev.

niri (~/.config/niri/conf/binds.kdl):
    Pause repeat=false { spawn "true"; }
    Scroll_Lock repeat=false { spawn "true"; }
  autostart:  spawn-at-startup "alter-talk"

Hyprland:
    bind = , Pause, exec, true
    bind = , Scroll_Lock, exec, true
  autostart:  exec-once = alter-talk
"""


def _setup_main() -> int:
    """Interactive CLI menu (no curses — inherits the terminal theme)."""
    values = _field_defaults()
    values.update(_CFG)
    message = ""
    while True:
        print("\n" + "─" * 60)
        print(f" alter-talk setup — {DEFAULT_CONFIG_PATH}")
        print("─" * 60)
        for i, (key, label, conv) in enumerate(_SETUP_FIELDS, start=1):
            mark = "*" if key in _CFG else " "
            print(f" {i:2}) {mark} {label:<40} {_fmt_value(values[key])}")
        print("─" * 60)
        print(" [num] edit · [s] save · [t] test STT · [p] compositor binds"
              " · [r] restart daemon · [q] quit")
        if message:
            print(f" {message}")
        try:
            choice = input(" > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        message = ""
        if choice in ("q", "quit", "exit"):
            return 0
        if choice == "s":
            _save_config_file(values)
            message = f"saved to {DEFAULT_CONFIG_PATH} — restart the daemon to apply (r)"
        elif choice == "t":
            message = "STT health: " + _health_check(values["whisper_health_url"])
        elif choice == "p":
            print("\n" + _COMPOSITOR_SNIPPETS)
            input(" [press Enter to return] ")
        elif choice == "r":
            message = _restart_daemon()
        elif choice.isdigit():
            i = int(choice) - 1
            if 0 <= i < len(_SETUP_FIELDS):
                key, label, conv = _SETUP_FIELDS[i]
                cur = _fmt_value(values[key])
                if conv is bool:
                    ans = input(f" {label} [{cur}] (y/n, Enter=keep): ").strip().lower()
                    if ans in ("y", "yes", "on", "1"):
                        values[key] = True
                        message = f"{key} = yes"
                    elif ans in ("n", "no", "off", "0"):
                        values[key] = False
                        message = f"{key} = no"
                    else:
                        message = f"{key} unchanged"
                else:
                    raw = input(f" {label} [{cur}] > ").strip()
                    if not raw:
                        message = f"{key} unchanged"
                    else:
                        try:
                            values[key] = _parse_value(raw, conv)
                            message = f"{key} = {_fmt_value(values[key])}"
                        except ValueError:
                            message = "invalid value, not saved"
            else:
                message = "no such field"
        else:
            message = f"unknown command: {choice}"


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------
def _daemon_main() -> int:
    lock_fh = open(DAEMON_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("alter-talk: daemon already running", file=sys.stderr)
        return 0
    try:
        _Daemon().run()
    except KeyboardInterrupt:
        pass
    finally:
        lock_fh.close()
        try:
            Path(DAEMON_LOCK_PATH).unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def _send_main() -> int:
    """One-shot paste + Enter (manual/backup trigger, e.g. a WM bind)."""
    # Refuse while a recording is in flight so stale content never pastes.
    try:
        lock_fh = open(LOCK_PATH, "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fh.close()
    except OSError:
        _notify("alter-talk", "Recording in progress — skipping paste")
        return 0
    try:
        send_keys(enter=SCROLL_SEND_ENTER)
    except Exception as exc:
        _notify("alter-talk", f"Failed to send: {exc}")
        return 1
    return 0


def main() -> int:
    if sys.argv[1:2] == ["status"]:
        return _status_main()
    if sys.argv[1:2] == ["config"]:
        return _config_main()
    if sys.argv[1:2] == ["setup"]:
        return _setup_main()

    parser = argparse.ArgumentParser(
        description=(
            "alter-talk: voice daemon (Pause/Scroll Lock) + helpers.\n"
            "Subcommands: status (state), config (TOML editor), "
            "setup (interactive TUI), --send (paste+Enter)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--send", action="store_true",
        help="one-shot: paste clipboard + Enter (skips daemon)",
    )
    parser.add_argument(
        "--seconds", type=float, default=0.0,
        help="record for a fixed duration (one-shot, no daemon)",
    )
    parser.add_argument(
        "--file", type=Path, default=None,
        help="transcribe an existing audio file and copy it (one-shot)",
    )
    parser.add_argument(
        "--no-copy", action="store_true",
        help="print the transcript instead of copying",
    )
    args = parser.parse_args()

    if args.send:
        return _send_main()

    if args.file is not None or args.seconds > 0 or args.no_copy:
        # Legacy one-shot record flow (no daemon).
        lock_fh = open(LOCK_PATH, "w")
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        try:
            if args.file is not None:
                wav = args.file.expanduser().resolve()
                if not wav.is_file():
                    print(f"File not found: {wav}", file=sys.stderr)
                    return 1
                _notify("alter-talk", "Processing speech...")
                try:
                    text = transcribe(wav)
                except Exception as exc:
                    _notify("alter-talk", f"Recognition error: {exc}")
                    return 1
                text = normalize_text(text)
                if args.no_copy:
                    print(text)
                else:
                    copy_to_clipboard(text)
                    _notify("alter-talk", f"Copied: {text[:100]}")
                return 0
            return run_record_cycle(autosend=False, seconds=args.seconds)
        finally:
            lock_fh.close()
            try:
                Path(LOCK_PATH).unlink(missing_ok=True)
            except OSError:
                pass

    return _daemon_main()


if __name__ == "__main__":
    raise SystemExit(main())
