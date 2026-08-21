#!/usr/bin/env python3
"""shipboard: voice daemon for Pause / Scroll Lock — record, copy, send.

Three modes:
  * Pause held ......... record -> whisper -> wl-copy (clipboard only)
  * Scroll Lock tap .... paste current clipboard + Enter (send only)
  * Pause + ScrollLock . record -> whisper -> wl-copy -> paste + Enter
                         (auto-send after processing)

WM-agnostic: the daemon watches evdev directly, so no compositor keybinds
are needed (and none should exist — a WM bind would double-fire). Runs via
compositor autostart (`spawn-at-startup "shipboard"` / `exec-once`), guarded
by a single-instance flock.

Key injection uses a uinput virtual keyboard (python-evdev) — modifier combos
are layout-independent, so Cyrillic clipboard text pastes correctly in any
layout. Falls back to wtype if uinput is unavailable.

Requires: pw-record (PipeWire), python-evdev, wl-clipboard, whisper-local
container (see ~/.config/shipboard/docker-compose.yml + whisper-wake-proxy).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import queue
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import platform_adapters as _plat  # noqa: E402  (same-dir module)

# --------------------------------------------------------------------------
# Configuration: defaults < TOML config file < environment variables
# --------------------------------------------------------------------------
DEFAULT_CONFIG_PATH = Path(
    "~/.config/shipboard/shipboard.toml"
).expanduser()
STATE_PATH = Path(
    os.environ.get(
        "SHIPBOARD_STATE", "~/.local/state/shipboard/state.json"
    )
).expanduser()

DEFAULT_CONFIG_TEXT = """\
# shipboard configuration
# Precedence: defaults < this file < environment variables (SHIPBOARD_*).

# Speech-to-text server (local wake proxy or a remote tailnet host)
whisper_url = "http://127.0.0.1:10300/inference"
whisper_health_url = "http://127.0.0.1:10300/health"
whisper_container = "whisper-local"
whisper_language = "auto"  # whisper language: auto / ru / en / ...

# Recording
max_hold = 60          # seconds; force-finish a stuck recording
min_recording = 0.5    # seconds; shorter recordings are discarded
record_rate = 16000    # sample rate for pw-record and the wake listener
record_channels = 1    # channels for pw-record and the wake listener

# Keys -> behaviour: each physical key maps to tap / hold / toggle.
# tap  = short press (< hold_threshold), hold = long press (>= hold_threshold),
# toggle = press to start/stop (overrides tap when set). Combine hold+tap or hold+toggle on the same key — idempotent.
# actions: "" (off), "record" (record->copy), "record_send" (record->copy->paste+Enter), "paste" (paste clipboard)
# Any key 1-3 bindings, everything optional, no overlapping keys. Example — rightalt tap=copy, hold=send:
# [[key_bind]]
# key = "rightalt"
# tap = "record"
# hold = "record_send"
# toggle = ""
# hold_threshold = 0.25
# [[key_bind]]
# key = "f13"
# tap = "paste"
# hold = ""
# toggle = "record"
# New: key -> behaviour (any 1-3 keys, any combo of tap/hold/toggle, no overlaps).
# Hold = long press (>= hold_threshold), tap = short press, toggle = press to start/stop (overrides tap).
# Example — your rightalt: tap=record (copy only), hold=record_send (copy+paste+Enter), tweakable per key:
# [[key_bind]]
# key = "rightalt"
# tap = "record"
# hold = "record_send"
# toggle = ""
# hold_threshold = 0.25

# Send mode
paste_combo = "ctrl+shift+v"   # injected as a modifier combo (layout-proof)
send_enter = true              # also press Enter after pasting

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

# Wake word listener (sherpa-onnx KWS).
wakeword_enabled = false
wakeword_cooldown = 2.0           # seconds between triggers
wakeword_grace = 3.0              # keep recording through silence right after
                                  # a trigger, so you have time to start speaking
wakeword_stop_silence = 1.5       # seconds of silence end the recording
wakeword_action = "record"        # record (clipboard) | record_send (paste+Enter)
wakeword_silence_level = 500      # RMS below this counts as silence (0..32768)
wakeword_sherpa_score = 1.0       # sherpa KWS: boost for matched keywords
wakeword_sherpa_threshold = 0.25  # sherpa KWS: trigger bar (lower=easier)
kws_threads = 2                   # sherpa KWS: onnxruntime threads
# Wake phrases per action, comma-separated alternatives (at least two words
# each to avoid random triggers; edit with `shipboard setup`):
wakeword_record = "copy it, take it, grab it, catch it"  # record -> clipboard only
wakeword_send = "push it, ship it, send it, drop it"     # record -> clipboard -> paste+Enter
wakeword_paste = "paste it, insert it, stick it"         # paste clipboard + Enter
# Raw phrase list override, used only when all three keys above are empty:
# wakeword_keywords = "alter capture:record, alter send:record_send"
wakeword_debug = false            # log the mic level every second
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


_KEY_CODE_CACHE: dict[str, int] = {}


def _key_code(name: str) -> int:
    """evdev key name ('pause', 'scrolllock', 'f13', ...) -> input code."""
    code = _KEY_CODE_CACHE.get(name)
    if code is not None:
        return code
    import evdev

    try:
        code = int(getattr(evdev.ecodes, f"KEY_{name.upper()}"))
    except AttributeError as exc:
        raise SystemExit(f"shipboard: unknown key name {name!r}") from exc
    _KEY_CODE_CACHE[name] = code
    return code


_KEY_LABELS = {
    "pause": "Pause", "scrolllock": "Scroll Lock", "insert": "Insert",
    "home": "Home", "end": "End", "pageup": "Page Up", "pagedown": "Page Down",
    "delete": "Delete", "f13": "F13", "f14": "F14", "f15": "F15",
    "f16": "F16", "f17": "F17", "f18": "F18", "f19": "F19", "f20": "F20",
}


def _key_label(name: str) -> str:
    if not name:
        return "off"
    return _KEY_LABELS.get(name, name.replace("_", " ").title())


WHISPER_URL = _cfg("whisper_url", "WHISPER_CPP_URL",
                   "http://127.0.0.1:10300/inference")
HEALTH_URL = _cfg("whisper_health_url", "WHISPER_CPP_HEALTH_URL",
                  "http://127.0.0.1:10300/health")
WHISPER_CONTAINER = _cfg("whisper_container", "WHISPER_CONTAINER",
                         "whisper-local")
WHISPER_LANGUAGE = _cfg("whisper_language", "SHIPBOARD_WHISPER_LANGUAGE",
                        "auto")
IDLE_MARKER = Path(_cfg("idle_marker", "WHISPER_IDLE_MARKER",
                        "/tmp/whisper-local-last-use"))
MAX_HOLD = _cfg("max_hold", "SHIPBOARD_MAX_HOLD", 60.0, float)
MIN_RECORDING = _cfg("min_recording", "SHIPBOARD_MIN_RECORDING", 0.5, float)
# (no legacy key vars — configure via [[key_bind]] only)

# --- key -> behaviour bindings ---
_ALLOWED_ACTIONS = {"", "record", "record_send", "paste"}
_HOLD_THRESHOLD_DEFAULT = 0.25

def _normalize_action(v) -> str:
    v = str(v or "").strip().lower()
    if v == "":
        return ""
    if v in _ALLOWED_ACTIONS:
        return v
    # invalid non-empty -> keep raw lower for parser to error, but also return "" for UI previews
    return v

def _parse_key_bindings(cfg: dict) -> list:
    raw = cfg.get("key_bind")
    if raw is None:
        raw = cfg.get("key_binding")
    if raw is None:
        raw = cfg.get("key_bindings")
    bindings = []
    if isinstance(raw, list) and raw:
        for idx, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise SystemExit(f"shipboard: [[key_bind]] #{idx+1} must be a table")
            key = str(entry.get("key", "")).strip().lower()
            if not key:
                raise SystemExit(f"shipboard: [[key_bind]] #{idx+1} missing 'key'")
            # validate actions strictly: empty = off, else must be allowed
            def _strict_action(raw):
                s = str(raw or "").strip().lower()
                if s == "":
                    return ""
                if s in _ALLOWED_ACTIONS:
                    return s
                raise SystemExit(f"shipboard: [[key_bind]] #{idx+1} bad action {raw!r} (allowed: record/record_send/paste)")
            tap = _strict_action(entry.get("tap", ""))
            hold = _strict_action(entry.get("hold", ""))
            toggle = _strict_action(entry.get("toggle", ""))
            try:
                thr = float(entry.get("hold_threshold", _HOLD_THRESHOLD_DEFAULT))
            except Exception:
                raise SystemExit(f"shipboard: [[key_bind]] #{idx+1} bad hold_threshold")
            if thr <= 0 or thr > 5:
                raise SystemExit(f"shipboard: [[key_bind]] #{idx+1} hold_threshold out of range (0..5)")
            # toggle overrides tap — warn but allow (hold+tap / hold+toggle idempotent: short release=tap/toggle, long=hold)
            bindings.append({"key": key, "tap": tap, "hold": hold, "toggle": toggle, "hold_threshold": thr})
    else:
        bindings = []
    # no overlapping check
    seen = {}
    for b in bindings:
        k = b["key"]
        if k in seen:
            raise SystemExit(f"shipboard: duplicate key {k!r} in [[key_bind]] — no overlapping")
        seen[k] = True
        # validate evdev name early
        try:
            _key_code(k)
        except SystemExit as e:
            raise SystemExit(f"shipboard: {e}") from None
    return bindings

KEY_BINDINGS = _parse_key_bindings(_CFG)

RATE = _cfg("record_rate", "SHIPBOARD_RECORD_RATE", 16000, int)
CHANNELS = _cfg("record_channels", "SHIPBOARD_RECORD_CHANNELS", 1, int)
LOCK_PATH = Path(_cfg("lock_path", "SHIPBOARD_LOCK", "/tmp/shipboard.lock"))
DAEMON_LOCK_PATH = Path(_cfg("daemon_lock_path", "SHIPBOARD_DAEMON_LOCK",
                             "/tmp/shipboard.daemon.lock"))
PASTE_COMBO = _cfg("paste_combo", "SHIPBOARD_PASTE_COMBO",
                   "ctrl+shift+v", str.lower)
SEND_ENTER = _cfg("send_enter", "SHIPBOARD_SEND_ENTER", True, _as_bool)
# Per-trigger "Enter after paste" (fall back to the global send_enter):
# Scroll Lock tap / wake word "paste" vs record+send paths differ on purpose.
SCROLL_SEND_ENTER = _cfg("scroll_send_enter", "SHIPBOARD_SCROLL_SEND_ENTER",
                         SEND_ENTER, _as_bool)
BOTH_SEND_ENTER = _cfg("both_send_enter", "SHIPBOARD_BOTH_SEND_ENTER",
                       SEND_ENTER, _as_bool)
# Wake word listener (engine WIP — config schema is ready)
WAKEWORD_ENABLED = _cfg("wakeword_enabled", "SHIPBOARD_WAKEWORD_ENABLED",
                        False, _as_bool)
WAKEWORD_COOLDOWN = _cfg("wakeword_cooldown", "SHIPBOARD_WAKEWORD_COOLDOWN",
                         2.0, float)
WAKEWORD_GRACE = _cfg("wakeword_grace", "SHIPBOARD_WAKEWORD_GRACE",
                      3.0, float)
WAKEWORD_STOP_SILENCE = _cfg("wakeword_stop_silence",
                             "SHIPBOARD_WAKEWORD_STOP_SILENCE", 1.5, float)
WAKEWORD_ACTION = _cfg("wakeword_action", "SHIPBOARD_WAKEWORD_ACTION",
                       "record")
WAKEWORD_SILENCE_LEVEL = _cfg("wakeword_silence_level",
                              "SHIPBOARD_WAKEWORD_SILENCE_LEVEL", 500, float)
# Sherpa KWS firing knobs: score boosts matched keywords, threshold is the
# bar they must clear (lower = easier to trigger, more false positives).
WAKEWORD_SHERPA_SCORE = _cfg("wakeword_sherpa_score",
                             "SHIPBOARD_WAKEWORD_SHERPA_SCORE", 1.0, float)
WAKEWORD_SHERPA_THRESHOLD = _cfg("wakeword_sherpa_threshold",
                                 "SHIPBOARD_WAKEWORD_SHERPA_THRESHOLD",
                                 0.25, float)
KWS_THREADS = _cfg("kws_threads", "SHIPBOARD_KWS_THREADS", 2, int)
# Per-action wake words (edited with `shipboard setup`) compose the KWS
# phrase list; the raw wakeword_keywords key stays for hand-edited configs.
_WAKE_WORD_ACTIONS = (
    ("wakeword_record", "record"),
    ("wakeword_send", "record_send"),
    ("wakeword_paste", "paste"),
)


def _compose_keywords(cfg: dict) -> str | None:
    """'wakeword_send: "ship it, send it"' ->
    'ship it:record_send, send it:record_send, ...' (or None).
    Commas or '|' separate alternative phrases for the same action."""
    words = [str(cfg.get(k, "")).strip() for k, _ in _WAKE_WORD_ACTIONS]
    if not any(words):
        return None
    out = []
    for (k, action), w in zip(_WAKE_WORD_ACTIONS, words):
        for variant in re.split(r"[|,]", w):
            variant = variant.replace(":", " ").strip()
            if variant:
                out.append(f"{variant}:{action}")
    return ", ".join(out)


_env_kw = os.environ.get("SHIPBOARD_WAKEWORD_KEYWORDS")
if _env_kw:
    WAKEWORD_KEYWORDS = _env_kw
else:
    WAKEWORD_KEYWORDS = _compose_keywords(_CFG) or _cfg(
        "wakeword_keywords", "SHIPBOARD_WAKEWORD_KEYWORDS",
        "alter capture:record, alter send:record_send")
WAKEWORD_DEBUG = _cfg("wakeword_debug", "SHIPBOARD_WAKEWORD_DEBUG",
                      False, _as_bool)
DRY_RUN = _cfg("dry_run", "SHIPBOARD_DRY_RUN", False, _as_bool)
NORMALIZE = _cfg("normalize", "SHIPBOARD_NORMALIZE", True, _as_bool)
PROMPT = _cfg("prompt", "SHIPBOARD_PROMPT", "")
_keep_audio_dir = _cfg("keep_audio_dir", "SHIPBOARD_KEEP_AUDIO_DIR", "")
KEEP_AUDIO_DIR = Path(_keep_audio_dir).expanduser() if _keep_audio_dir else None
# "default"/"auto"/"" = record from the system default source (PipeWire
# picks it, e.g. the EasyEffects chain); any other value = explicit device.
RECORD_TARGET = _cfg("record_target", "SHIPBOARD_RECORD_TARGET", "")
if RECORD_TARGET.lower() in ("default", "auto", "system"):
    RECORD_TARGET = ""


def _notify(title: str, msg: str) -> None:
    _plat.notify(title, msg)


# --------------------------------------------------------------------------
# Recording (PipeWire)
# --------------------------------------------------------------------------
def _log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except Exception:
        pass


def start_recording(path: Path) -> subprocess.Popen:
    return _plat.start_recording(
        path, rate=RATE, channels=CHANNELS,
        target=RECORD_TARGET or None,
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
# Wake word listener (sherpa-onnx KWS, streaming via parec)
# --------------------------------------------------------------------------
_WAKE_MODELS_DIR = Path.home() / ".local/share/shipboard/models"
_WAKE_VENV = Path.home() / ".local/share/shipboard-venv"
_WAKE_SILENCE_RMS = WAKEWORD_SILENCE_LEVEL / 32768.0
_SHERPA_MODEL_DIR = _WAKE_MODELS_DIR / \
    "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
_SHERPA_KEYWORDS_FILE = _WAKE_MODELS_DIR / "sherpa-kws-keywords.txt"
_SHERPA_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
               "kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
               ".tar.bz2")


def _parse_keywords(spec: str) -> list[tuple[str, str]]:
    """'alter capture:record, alter send:record_send' -> [(phrase, action)]."""
    out: list[tuple[str, str]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            phrase, action = part.rsplit(":", 1)
            out.append((phrase.strip(), action.strip()))
        else:
            out.append((part, WAKEWORD_ACTION))
    return out


def _ensure_sherpa_model() -> bool:
    needed = ["tokens.txt", "en.phone",
              "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
              "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
              "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx"]
    if all((_SHERPA_MODEL_DIR / f).is_file() for f in needed):
        return True
    _WAKE_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    _log("wakeword: downloading sherpa-onnx KWS model (~17MB) ...")
    import tarfile
    try:
        tmp = _WAKE_MODELS_DIR / "sherpa-kws.tar.bz2.tmp"
        urllib_request.urlretrieve(_SHERPA_URL, tmp)
        with tarfile.open(tmp, "r:bz2") as tf:
            tf.extractall(_WAKE_MODELS_DIR, filter="data")
        tmp.unlink(missing_ok=True)
        return all((_SHERPA_MODEL_DIR / f).is_file() for f in needed)
    except Exception as exc:
        _log(f"wakeword: sherpa model download failed: {exc}")
        return False


def _ensure_sherpa_keywords() -> bool:
    """Tokenize the configured phrases into the KWS keywords file.

    The generated file is cached across restarts, but regenerated whenever
    the configured phrases change (so editing wake words takes effect).
    text2token runs PER PHRASE: its multi-line mode misaligns lines when a
    phrase is missing from the en.phone lexicon, so one bad word must not
    corrupt the whole file — it is skipped and logged instead.
    """
    tokens = _SHERPA_MODEL_DIR / "tokens.txt"
    lexicon = _SHERPA_MODEL_DIR / "en.phone"
    raw = _WAKE_MODELS_DIR / "sherpa-kws-raw.txt"
    # zh-en model: phone+ppinyin keywords; the original phrase goes after
    # '@' with spaces replaced by underscores (the spotter reports that
    # form), so 'alter send' comes back as 'ALTER_SEND'.
    lines = [f"{phrase.upper()} @{phrase.upper().replace(' ', '_')}"
             for phrase, _ in _parse_keywords(WAKEWORD_KEYWORDS)]
    raw_text = "\n".join(lines) + "\n"
    if (_SHERPA_KEYWORDS_FILE.is_file() and raw.is_file()
            and raw.read_text() == raw_text):
        return True
    raw.write_text(raw_text)
    cli = _WAKE_VENV / "bin" / "sherpa-onnx-cli"
    outputs: list[str] = []
    try:
        for i, line in enumerate(lines):
            one_raw = _WAKE_MODELS_DIR / f"sherpa-kws-one-{i}.raw"
            one_out = _WAKE_MODELS_DIR / f"sherpa-kws-one-{i}.txt"
            try:
                one_raw.write_text(line + "\n")
                r = subprocess.run(
                    [str(cli), "text2token", str(one_raw),
                     "--tokens", str(tokens),
                     "--tokens-type", "phone+ppinyin",
                     "--lexicon", str(lexicon), str(one_out)],
                    capture_output=True, timeout=60,
                )
                if r.returncode == 0 and one_out.is_file():
                    tokenized = one_out.read_text().strip()
                    if tokenized:
                        outputs.append(tokenized)
                        continue
                _log(f"wakeword: keyword {line!r} not tokenized"
                     f" (missing from en.phone lexicon?) — skipped")
            finally:
                one_raw.unlink(missing_ok=True)
                one_out.unlink(missing_ok=True)
    except Exception as exc:
        _log(f"wakeword: text2token failed: {exc}")
        return False
    if not outputs:
        _log("wakeword: no keywords tokenized — listener off")
        return False
    _SHERPA_KEYWORDS_FILE.write_text("\n".join(outputs) + "\n")
    return True


class _SherpaKws:
    """sherpa-onnx open-vocabulary keyword spotter (phrase -> action)."""

    def __init__(self) -> None:
        import sherpa_onnx
        self.pairs = _parse_keywords(WAKEWORD_KEYWORDS)
        self.actions = {phrase.casefold(): action
                        for phrase, action in self.pairs}
        # provider string "cpu:<config>" lets sherpa forward onnxruntime
        # session config entries (e.g. allow_spinning=0 -> no idle CPU burn).
        _ort_cfg = Path(__file__).resolve().parent / "ort-nospin.config"
        _provider = f"cpu:{_ort_cfg}" if _ort_cfg.is_file() else "cpu"
        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(_SHERPA_MODEL_DIR / "tokens.txt"),
            encoder=str(_SHERPA_MODEL_DIR /
                        "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=str(_SHERPA_MODEL_DIR /
                        "decoder-epoch-13-avg-2-chunk-16-left-64.onnx"),
            joiner=str(_SHERPA_MODEL_DIR /
                       "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=str(_SHERPA_KEYWORDS_FILE),
            num_threads=KWS_THREADS, provider=_provider,
            keywords_score=WAKEWORD_SHERPA_SCORE,
            keywords_threshold=WAKEWORD_SHERPA_THRESHOLD,
        )
        self.stream = self.spotter.create_stream()

    def feed(self, audio) -> str | None:
        """Returns the detected phrase (or None)."""
        self.stream.accept_waveform(16000, audio)
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
        result = self.spotter.get_result(self.stream)
        if result:
            self.spotter.reset_stream(self.stream)
            # zh-en model reports the '@' original: underscores instead of
            # spaces ("ALTER_SEND") — normalize back to the phrase form.
            return result.strip().replace("_", " ")
        # NOTE: no reset_stream() here! is_ready()==False just means all
        # frames were consumed; resetting would wipe the decoder's keyword
        # hypotheses every chunk (80 ms) so a phrase spanning ~1s could
        # never accumulate enough score to trigger.
        return None

    def action_for(self, phrase: str) -> str:
        return self.actions.get(phrase.casefold(), WAKEWORD_ACTION)

    def describe(self) -> str:
        return "sherpa: " + ", ".join(p for p, _ in self.pairs)


def _wake_listen(self, stop_event: threading.Event) -> None:
    """Continuous listener: pw-cat -> detector -> record until silence."""
    # onnxruntime's intra-op threads busy-spin when idle (3 sessions =
    # encoder/decoder/joiner = ~3 cores of pure spin). Make them sleep.
    os.environ.setdefault("ORT_DISABLE_SPIN_WAIT", "1")
    # sherpa-onnx lives in the shipboard venv; make it
    # importable from the system python the daemon runs under.
    try:
        for sp in (_WAKE_VENV / "lib").glob("python*/site-packages"):
            sys.path.insert(0, str(sp))
        import numpy as np
    except Exception as exc:
        _log(f"wakeword: deps unavailable ({exc}) — listener off")
        return
    try:
        if not _ensure_sherpa_model() or not _ensure_sherpa_keywords():
            _log("wakeword: sherpa model/keywords unavailable — listener off")
            return
        detector = _SherpaKws()
        _log(f"wakeword: listening ({detector.describe()})")
        _notify("shipboard", f"Wake word on: {detector.describe()}")
    except Exception as exc:
        _log(f"wakeword: engine init failed: {exc}")
        return

    cmd = ["pw-cat", "--record", "--rate", str(RATE), "--channels",
           str(CHANNELS), "--format", "s16", "--raw", "-"]
    if RECORD_TARGET:
        cmd += ["--target", RECORD_TARGET]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        _log(f"wakeword: pw-cat failed: {exc}")
        return
    import atexit
    atexit.register(proc.terminate)

    last_trigger = 0.0
    silence_since: float | None = None
    next_level_log = 0.0
    try:
        while not stop_event.is_set():
            raw = proc.stdout.read(int(RATE * CHANNELS * 2 * 0.08))  # 80 ms
            if not raw:
                break
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            now = time.monotonic()
            rms = float(np.sqrt(np.mean(audio ** 2)))

            # Debug: mic level bar every second (see it "trying to catch").
            if WAKEWORD_DEBUG and now >= next_level_log:
                next_level_log = now + 1.0
                bar = "▁▂▃▄▅▆▇█"[min(7, int(rms * 40))]
                _log(f"wakeword: lvl {rms:.3f} {bar}")

            # While a wake-triggered recording is running: stop on silence
            # or the max-record cap. Silence is ignored during the post-
            # trigger grace period (user needs a beat to start dictating).
            if self.recording and self.wake_rec:
                in_grace = now - self.rec_t0 < WAKEWORD_GRACE
                if rms < _WAKE_SILENCE_RMS and not in_grace:
                    if silence_since is None:
                        silence_since = now
                    elif now - silence_since >= WAKEWORD_STOP_SILENCE:
                        _log("wakeword: silence — finishing recording")
                        self._finish_record(from_wake=True)
                        self.wake_rec = False
                        silence_since = None
                else:
                    silence_since = None
                if now - self.rec_t0 > MAX_HOLD:
                    _log("wakeword: max record — finishing")
                    self._finish_record(from_wake=True)
                    self.wake_rec = False
                continue

            if self.recording or now - last_trigger < WAKEWORD_COOLDOWN:
                continue
            phrase = detector.feed(audio)
            if phrase:
                last_trigger = now
                action = detector.action_for(phrase)
                _log(f"wakeword: DETECTED {phrase!r} -> {action}")
                if action == "paste":
                    _notify("shipboard", f"Paste (wake word: {phrase})")
                    self._inject_q.put(SCROLL_SEND_ENTER)
                    continue
                _notify("shipboard", f"Wake word detected: {phrase}")
                self.wake_rec = True
                self._start_record()
                # autosend is set AFTER _start_record: the latter resets
                # autosend=False at the start of every recording, so setting
                # it before was silently wiped (wake record_send never sent).
                # Same order as the key path (_on_pause/_on_scrolllock).
                self.autosend = action == "record_send" and self.recording
                _log(f"wakeword: trigger autosend={self.autosend} wake_rec={self.wake_rec}")
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
    boundary = f"----shipboard-{uuid.uuid4().hex}"
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
    fields = {"language": WHISPER_LANGUAGE}
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
    _plat.copy_to_clipboard(text)


# --------------------------------------------------------------------------
# Key injection (platform adapter: uinput -> wtype, pynput elsewhere)
# --------------------------------------------------------------------------
def send_keys(combo: str = PASTE_COMBO, enter: bool = SEND_ENTER) -> None:
    if DRY_RUN:
        print(f"[dry-run] send combo={combo!r} enter={enter}")
        return
    try:
        _plat.get_inject_backend()(combo, enter)
    except Exception as exc:
        raise RuntimeError(f"failed to inject keys: {exc}") from exc


# --------------------------------------------------------------------------
# evdev device discovery
# --------------------------------------------------------------------------
def _watch_devices():
    import evdev

    wanted = {_key_code(b["key"]) for b in KEY_BINDINGS if b.get("key")}
    if not wanted:
        return []
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities(verbose=False)
            keys = caps.get(evdev.ecodes.EV_KEY, [])
            if keys and wanted & set(keys):
                devices.append(dev)
        except Exception:
            continue
    return devices


def _input_access_issue() -> str:
    """Diagnose why key triggers aren't readable from /dev/input.

    Empty string = access is fine (keys simply aren't mapped on any device).
    Otherwise a short, actionable hint (e.g. user not in the `input` group),
    so the otherwise-silent "keys not found" becomes debuggable.
    """
    import evdev
    import glob
    import grp
    import os

    # Scan ALL input nodes (glob, not evdev.list_devices, which only returns
    # nodes this process can already open) so an unreadable keyboard is seen.
    paths = sorted(glob.glob("/dev/input/event*"))
    if not paths:
        return ""  # no input devices at all
    forbidden = False
    for p in paths:
        try:
            dev = evdev.InputDevice(p)
            try:
                dev.close()
            except Exception:
                pass
        except PermissionError:
            forbidden = True
            break
        except OSError:
            continue
    if not forbidden:
        return ""  # all devices openable; keys just not mapped on any
    gname = "input"
    try:
        gid = os.stat("/dev/input/event0").st_gid
        gname = grp.getgrgid(gid).gr_name
    except Exception:
        pass
    mine = {grp.getgrgid(g).gr_name for g in os.getgroups()}
    if gname in mine:
        return (f"keys off: /dev/input unreadable despite being in group "
                f"'{gname}' (session needs log out/in or a udev rule)")
    return (f"keys off: not in group '{gname}' — run "
            f"'sudo usermod -aG {gname} $USER' then log out/in")


# --------------------------------------------------------------------------
# Recording cycle (shared by daemon and one-shot modes)
# --------------------------------------------------------------------------
def _transcribe_copy(wav: Path) -> tuple[str, str]:
    """Transcribe -> normalize -> copy to clipboard. Returns (text, preview).
    Raises RuntimeError with a user-facing message on any failure."""
    try:
        text = transcribe(wav)
    except Exception as exc:
        raise RuntimeError(f"STT error: {exc}") from exc
    text = normalize_text(text)
    if not text:
        raise RuntimeError("Nothing recognized")
    try:
        copy_to_clipboard(text)
    except Exception as exc:
        raise RuntimeError(f"Copy failed: {exc}") from exc
    preview = text if len(text) <= 100 else text[:100] + "…"
    return text, preview


def run_record_cycle(autosend: bool, seconds: float = 0.0) -> int:
    """Record -> transcribe -> copy; auto-send if autosend. Returns exit code."""
    # one-shot wait: first binding with hold/record
    first_hold_key = None
    for b in KEY_BINDINGS:
        if b.get("hold") in ("record","record_send"):
            first_hold_key = b["key"]
            break
    if not first_hold_key and KEY_BINDINGS:
        # fallback: any key with tap/record
        for b in KEY_BINDINGS:
            if b.get("tap") in ("record","record_send") or b.get("toggle") in ("record","record_send"):
                first_hold_key = b["key"]
                break
    tmp_dir = Path(tempfile.mkdtemp(prefix="shipboard-"))
    try:
        wav_path = tmp_dir / "rec.wav"
        mode_word = "hold"
        label = _key_label(first_hold_key) if first_hold_key else "key"
        _notify(
            "shipboard",
            f"Recording... ({mode_word} {label})"
            + (" — release: will paste and send" if autosend else ""),
        )
        proc = start_recording(wav_path)
        t0 = time.monotonic()
        if seconds > 0:
            time.sleep(seconds)
        elif first_hold_key:
            import evdev

            deadline = time.monotonic() + MAX_HOLD
            live = _watch_devices()
            held = False
            code = _key_code(first_hold_key)
            while time.monotonic() < deadline:
                if not live:
                    break
                r, _, _ = select.select(live, [], [], 0.1)
                for dev in r:
                    try:
                        for event in dev.read():
                            if (
                                event.type == evdev.ecodes.EV_KEY
                                and event.code == code
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
            _notify("shipboard", "Recording too short")
            return 0
        if not wav_path.is_file() or wav_path.stat().st_size == 0:
            _notify("shipboard", "Error: empty recording file")
            return 1
        try:
            text, preview = _transcribe_copy(wav_path)
        except RuntimeError as exc:
            print(f"shipboard: {exc}", file=sys.stderr)
            _notify("shipboard", str(exc))
            return 1
        if autosend:
            try:
                send_keys(enter=BOTH_SEND_ENTER)
            except Exception as exc:
                _notify("shipboard", f"Copied, but not sent: {exc}")
                return 1
            _notify("shipboard", f"Sent: {preview}")
        else:
            _notify("shipboard", f"Copied: {preview}")
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
        self.grace_deadline: float | None = None
        self.pause_down = False
        self.wake_rec = False
        self._inject_q: "queue.Queue[bool]" = queue.Queue()
        # key->behaviour state
        self._key_down: dict[int, float] = {}  # code -> down_time
        self._hold_fired: set[int] = set()  # codes where hold threshold already fired
        self._pending_hold: dict[int, float] = {}  # code -> deadline
        self._rec_key: str | None = None  # which key started current recording
        self._rec_mode: str | None = None  # "hold" | "toggle" | "tap"
        # quick map code -> binding
        self._code_to_bind: dict[int, dict] = {}
        for b in KEY_BINDINGS:
            try:
                c = _key_code(b["key"])
                self._code_to_bind[c] = b
            except SystemExit:
                pass

    def _binding_for(self, code: int) -> dict | None:
        return self._code_to_bind.get(code)

    def _start_record(self, key: str | None = None, mode: str | None = None, notify_label: str | None = None) -> None:
        if self.recording:
            return
        self.autosend = False
        self.grace_deadline = None
        self._cycle_lock = open(LOCK_PATH, "w")
        try:
            fcntl.flock(self._cycle_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._cycle_lock.close()
            self._cycle_lock = None
            return
        tmp_dir = Path(tempfile.mkdtemp(prefix="shipboard-"))
        wav = tmp_dir / "rec.wav"
        self.rec_proc = start_recording(wav)
        self.recording = True
        self.rec_t0 = time.monotonic()
        self._tmp_dir = tmp_dir
        self._rec_key = key
        self._rec_mode = mode
        _write_state(state="recording")
        _log(f"record start key={key!r} mode={mode} -> {wav}")
        label = notify_label or (f"{_key_label(key)}" if key else "key")
        mode_word = mode or "hold"
        _notify("shipboard", f"Recording... ({mode_word} {label})")

    def _finish_record(self, from_wake: bool = False) -> None:
        if not self.recording or self.rec_proc is None:
            return
        stop_recording(self.rec_proc)
        self.recording = False
        duration = time.monotonic() - self.rec_t0
        autosend = self.autosend
        self.autosend = False
        _log(f"finish: autosend={autosend} from_wake={from_wake} dur={duration:.1f} key={self._rec_key} mode={self._rec_mode}")
        was_key = self._rec_key
        was_mode = self._rec_mode
        self._rec_key = None
        self._rec_mode = None
        cycle_lock = getattr(self, "_cycle_lock", None)
        self._cycle_lock = None
        wav = Path(self._tmp_dir) / "rec.wav"
        try:
            if duration < MIN_RECORDING:
                _notify("shipboard", "Recording too short")
                return
            if not wav.is_file() or wav.stat().st_size == 0:
                _notify("shipboard", "Error: empty recording file")
                return
            if KEEP_AUDIO_DIR is not None:
                KEEP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy(wav, KEEP_AUDIO_DIR / f"rec-{int(time.time())}.wav")
            _notify("shipboard", "Processing speech...")
            _write_state(state="processing")
            try:
                text, preview = _transcribe_copy(wav)
                _log(f"stt ok ({len(text)} chars) result: {preview!r}")
            except RuntimeError as exc:
                _log(f"stt error: {exc}")
                _write_state(
                    state="idle" if str(exc) == "Nothing recognized" else "error",
                    text=str(exc)[:200],
                )
                _notify("shipboard", str(exc))
                return
            if autosend:
                if from_wake:
                    self._inject_q.put(BOTH_SEND_ENTER)
                else:
                    try:
                        send_keys(enter=BOTH_SEND_ENTER)
                    except Exception as exc:
                        _write_state(state="error", text=str(exc)[:200])
                        _notify("shipboard", f"Copied, but not sent: {exc}")
                        return
                _write_state(state="sent", text=preview)
                _notify("shipboard", f"Sent: {preview}")
            else:
                _write_state(state="copied", text=preview)
                _notify("shipboard", f"Copied: {preview}")
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

    def _do_action(self, action: str, key: str, mode: str) -> None:
        if not action:
            return
        if action == "paste":
            try:
                send_keys(enter=SEND_ENTER)
                _log(f"paste action key={key} mode={mode}")
            except Exception as exc:
                _notify("shipboard", f"Failed to send: {exc}")
            return
        if action in ("record", "record_send"):
            # toggle/tap/hold share same start; autosend differs
            autosend = (action == "record_send")
            if self.recording:
                # if same key toggles, finish; else ignore if another recording active
                if self._rec_key == key:
                    # finishing same key — autosend already set? override if needed
                    self.autosend = autosend or self.autosend
                    self._finish_record()
                return
            self._start_record(key=key, mode=mode)
            if self.recording:
                self.autosend = autosend
            return

    def _on_key_press(self, code: int) -> None:
        bind = self._binding_for(code)
        if bind is None:
            return
        key = bind["key"]
        tap = bind.get("tap", "") or ""
        hold = bind.get("hold", "") or ""
        toggle = bind.get("toggle", "") or ""
        thr = float(bind.get("hold_threshold", _HOLD_THRESHOLD_DEFAULT))
        # toggle overrides tap
        if toggle:
            tap = ""
        # toggle path: press toggles recording
        if toggle:
            # if already recording from this key in toggle mode -> stop
            if self.recording and self._rec_key == key and self._rec_mode == "toggle":
                self._finish_record()
                return
            # if not recording, decide if this is hold vs toggle based on pending hold
            # need to defer: if hold also present, don't start toggle immediately; wait for threshold
            if hold and not self.recording:
                # defer toggle/hold decision: set pending deadline
                self._key_down[code] = time.monotonic()
                self._pending_hold[code] = time.monotonic() + thr
                return
            # no hold, immediate toggle start
            if not self.recording:
                self._do_action(toggle, key, "toggle")
            return
        # no toggle: handle hold+tap
        if hold and tap:
            # defer decision until release or threshold
            self._key_down[code] = time.monotonic()
            self._pending_hold[code] = time.monotonic() + thr
            return
        if hold and not tap:
            # only hold: wait for threshold then start; short press ignored (idempotent)
            self._key_down[code] = time.monotonic()
            self._pending_hold[code] = time.monotonic() + thr
            return
        if tap and not hold:
            # only tap: defer to release (release starts or stops recording for record, or pastes for paste)
            # idempotent with hold: hold+tap handled above, this is tap-only
            self._key_down[code] = time.monotonic()
            return
        # fallback: nothing

    def _on_key_release(self, code: int) -> None:
        bind = self._binding_for(code)
        if bind is None:
            return
        key = bind["key"]
        tap = bind.get("tap", "") or ""
        hold = bind.get("hold", "") or ""
        toggle = bind.get("toggle", "") or ""
        if toggle:
            tap = ""
        was_down = self._key_down.pop(code, None)
        pending = self._pending_hold.pop(code, None)
        fired = code in self._hold_fired
        if fired:
            self._hold_fired.discard(code)
            # hold was active, release finishes recording
            if self.recording and self._rec_key == key and self._rec_mode == "hold":
                self._finish_record()
            return
        # hold not yet fired
        if toggle and hold and pending is not None:
            # short release before threshold -> toggle action
            if was_down is not None:
                self._do_action(toggle, key, "toggle")
            return
        if hold and tap and pending is not None:
            # short release -> tap, else would have fired hold
            self._do_action(tap, key, "tap")
            return
        if hold and not tap and pending is not None:
            # only hold, short press ignored (or treat as hold if you want)
            # idempotent: do nothing on short tap when only hold is set
            return
        if tap and not hold:
            # only tap: release fires tap — tap as toggle (first release starts, second release stops)
            if self.recording and self._rec_key == key and self._rec_mode == "tap":
                self._finish_record()
                return
            if not self.recording:
                self._do_action(tap, key, "tap")
            return

    # (legacy shims removed — see _on_key_press/_on_key_release)

    def run(self) -> None:
        import evdev

        _write_state(state="running", pid=os.getpid())
        stop_event = threading.Event()
        if WAKEWORD_ENABLED:
            threading.Thread(
                target=_wake_listen, args=(self, stop_event), daemon=True
            ).start()
        devices = _watch_devices()
        if not devices and KEY_BINDINGS:
            hint = _input_access_issue()
            msg = "Daemon: trigger keys not found on evdev (wake words still on)"
            if hint:
                msg += f"\n{hint}"
            _notify("shipboard", msg)
            if hint:
                print(f"shipboard: {hint}", file=sys.stderr)
        # build code map
        code_map = {}
        for b in KEY_BINDINGS:
            try:
                code_map[_key_code(b["key"])] = b
            except SystemExit:
                pass
        while True:
            now = time.monotonic()
            while True:
                try:
                    _enter = self._inject_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    send_keys(enter=_enter)
                except Exception as exc:
                    _notify("shipboard", f"Failed to send: {exc}")
            # hold threshold expiry
            for code, deadline in list(self._pending_hold.items()):
                if code not in self._key_down:
                    continue
                if now >= deadline and code not in self._hold_fired:
                    bind = self._binding_for(code)
                    if bind is None:
                        continue
                    hold = bind.get("hold", "") or ""
                    if not hold:
                        continue
                    # fire hold
                    self._hold_fired.add(code)
                    key = bind["key"]
                    # if not already recording, start hold recording
                    if not self.recording:
                        self._do_action(hold, key, "hold")
                        # _do_action may have started; ensure mode is hold
                        if self.recording:
                            self._rec_mode = "hold"
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
                        code = event.code
                        if code not in code_map and code not in self._key_down and code not in self._hold_fired:
                            continue
                        if event.value == 1:
                            self._key_down.setdefault(code, now)
                            self._on_key_press(code)
                        elif event.value == 0:
                            self._on_key_release(code)
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
    if KEY_BINDINGS:
        hdr = "keys:"
        for b in KEY_BINDINGS:
            k = _key_label(b["key"])
            thr = b.get("hold_threshold", _HOLD_THRESHOLD_DEFAULT)
            parts = []
            if b.get("toggle"): parts.append(f"toggle={b['toggle']}")
            if b.get("hold"): parts.append(f"hold={b['hold']}@{thr:g}s")
            if b.get("tap"): parts.append(f"tap={b['tap']}")
            print(f"{hdr:<11} {k:<14} {' | '.join(parts) if parts else '(off)'}")
            hdr = "           "
    else:
        print("keys:      (none — wake words only)")
    print(f"paste:     {PASTE_COMBO} | Enter: {'yes' if SEND_ENTER else 'no'}"
          f" | max_hold {MAX_HOLD}s | min_rec {MIN_RECORDING}s")
    print(f"normalize: {'on' if NORMALIZE else 'off'}")
    print(f"wakeword:  {'on (sherpa)' if WAKEWORD_ENABLED else 'off'}")
    if WAKEWORD_ENABLED:
        print(f"           {WAKEWORD_KEYWORDS}")
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
            last_state = st.get("state", "?")
            note = ""
            if last_state == "running" and not _daemon_running():
                note = " (daemon stopped)"
            print(f"last:      {last_state} @ {when}{note}")
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
    print(f"recording: max_hold {MAX_HOLD}s, min {MIN_RECORDING}s")
    print(f"normalize: {'on' if NORMALIZE else 'off'}")
    return 0


# --------------------------------------------------------------------------
# TUI setup (stdlib curses, no dependencies)
# --------------------------------------------------------------------------
_SETUP_FIELDS = [
    # ── Essential (most people only touch these) ──
    ("paste_combo",        "Paste shortcut",                      str, "Essentials"),
    ("send_enter",         "Press Enter after paste",             bool, "Essentials"),
    ("normalize",          "Smart symbols  (тильда слэш -> ~/) ", bool, "Essentials"),
    ("prompt",             "Whisper prompt  (domain words, optional)", str, "Essentials"),
    ("whisper_language",   "Language  (auto / ru / en / ...)",    str, "Essentials"),
    # ── Recording (rarely tweaked) ──
    ("max_hold",           "Max hold  (stuck guard, seconds)",    float, "Recording"),
    ("min_recording",      "Ignore shorter than  (seconds)",      float, "Recording"),
    ("record_target",      "Mic source  (default = system)",      str, "Recording"),
    # (keys configured via [[key_bind]] — use the Keys screen [k])
    # ── Behind "Advanced" ──
    ("whisper_url",        "STT server URL",                      str, "Advanced"),
    ("whisper_health_url", "Health URL",                          str, "Advanced"),
    ("whisper_container",  "Docker container to wake",            str, "Advanced"),
    ("record_rate",        "Sample rate",                         int, "Advanced"),
    ("record_channels",    "Channels",                            int, "Advanced"),
    # Wake words
    ("wakeword_enabled",   "Wake word listener (engine WIP)",    bool, "Advanced"),
    ("wakeword_cooldown",  "Wake word cooldown, seconds",        float, "Advanced"),
    ("wakeword_grace",     "Wake word grace, seconds",           float, "Advanced"),
    ("wakeword_stop_silence", "Wake word stop on silence, s",    float, "Advanced"),
    ("wakeword_action",    "Wake word action (record/record_send)", str, "Advanced"),
    ("wakeword_silence_level", "Wake word silence RMS level",    float, "Advanced"),
    ("wakeword_sherpa_score", "Sherpa KWS score boost (sensitivity)", float, "Advanced"),
    ("wakeword_sherpa_threshold", "Sherpa KWS threshold (lower=easier)", float, "Advanced"),
    ("kws_threads",        "Sherpa KWS onnxruntime threads",     int, "Advanced"),
    ("wakeword_record",    "Wake word: record (copy only)",        str, "Advanced"),
    ("wakeword_send",      "Wake word: record+send (paste+Enter)", str, "Advanced"),
    ("wakeword_paste",     "Wake word: paste (clipboard)",         str, "Advanced"),
    ("wakeword_debug",     "Wake word mic level log",             bool, "Advanced"),
    # Platform
    ("keep_audio_dir",     "Keep recordings in dir ('' = delete)", str, "Advanced"),
    ("dry_run",            "Dry run (log actions, do nothing)",    bool, "Advanced"),
    ("inject_backend",     "Key inject backend (auto/uinput/wtype)", str, "Advanced"),
    ("notify_backend",     "Notify backend (auto/notify-send/...)", str, "Advanced"),
    ("clipboard_backend",  "Clipboard backend (auto/wl-copy/xclip)", str, "Advanced"),
    ("record_backend",     "Record backend (auto/pw-record/ffmpeg)", str, "Advanced"),
    ("input_device_glob",  "Input device glob (evdev, e.g. event*)", str, "Advanced"),
]


def _field_defaults() -> dict:
    return {
        "whisper_url": "http://127.0.0.1:10300/inference",
        "whisper_health_url": "http://127.0.0.1:10300/health",
        "whisper_container": "whisper-local",
        "whisper_language": "auto",
        "record_target": "default",
        "record_rate": 16000,
        "record_channels": 1,
        "paste_combo": "ctrl+shift+v",
        "send_enter": True,
        "max_hold": 60.0,
        "min_recording": 0.5,
        "normalize": True,
        "prompt": "",
        "wakeword_enabled": False,
        "wakeword_cooldown": 2.0,
        "wakeword_grace": 3.0,
        "wakeword_stop_silence": 1.5,
        "wakeword_action": "record",
        "wakeword_silence_level": 500.0,
        "wakeword_sherpa_score": 1.0,
        "wakeword_sherpa_threshold": 0.25,
        "kws_threads": 2,
        "wakeword_record": "copy it, take it, grab it, catch it",
        "wakeword_send": "push it, ship it, send it, drop it",
        "wakeword_paste": "paste it, insert it, stick it",
        "wakeword_debug": False,
        "keep_audio_dir": "",
        "dry_run": False,
        "inject_backend": "auto",
        "notify_backend": "auto",
        "clipboard_backend": "auto",
        "record_backend": "auto",
        "input_device_glob": "",
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
        "# shipboard configuration (edited with 'shipboard setup')",
        "# Precedence: defaults < this file < environment variables.",
        "",
    ]
    for key, _label, conv, _section in _SETUP_FIELDS:
        v = values[key]
        if conv is str:
            lines.append(f'{key} = "{v}"')
        elif conv is bool:
            lines.append(f"{key} = {'true' if v else 'false'}")
        else:
            lines.append(f"{key} = {v}")
    composed = _compose_keywords(values)
    if composed:
        lines.append(f'wakeword_keywords = "{composed}"')
    # emit key_bind tables LAST so bare keys are not swallowed (TOML: bare keys after [[array]] belong to that table)
    binds = values.get("_key_binds")
    if isinstance(binds, list) and binds:
        for b in binds:
            lines.append("")
            lines.append("[[key_bind]]")
            lines.append(f'key = "{b.get("key","")}"')
            if b.get("tap"): lines.append(f'tap = "{b["tap"]}"')
            if b.get("hold"): lines.append(f'hold = "{b["hold"]}"')
            if b.get("toggle"): lines.append(f'toggle = "{b["toggle"]}"')
            try:
                thr = float(b.get("hold_threshold", _HOLD_THRESHOLD_DEFAULT))
                if thr != _HOLD_THRESHOLD_DEFAULT:
                    lines.append(f"hold_threshold = {thr:g}")
            except Exception:
                pass
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
    # Match only the daemon python process (not bash wrappers / other CLIs).
    # The daemon is always launched as `python <path>` with NO subcommand;
    # `shipboard setup` / `status` / `--send` etc. carry extra argv entries
    # and must never be killed by stop/restart.
    out = subprocess.run(["pgrep", "-f", "python3 .*shipboard"],
                         capture_output=True, text=True)
    pids = []
    for pid in out.stdout.split():
        try:
            pid = int(pid)
        except ValueError:
            continue
        if pid == os.getpid():
            continue  # never kill our own CLI process
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            argv = [a.decode(errors="replace") for a in argv if a]
        except OSError:
            continue  # process exited between pgrep and read
        if len(argv) != 2:
            continue  # has a subcommand/flag -> not the daemon
        pids.append(pid)
    return pids


def _restart_daemon() -> str:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "shipboard"],
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
  autostart:  spawn-at-startup "shipboard"

Hyprland:
    bind = , Pause, exec, true
    bind = , Scroll_Lock, exec, true
  autostart:  exec-once = shipboard
"""


def _setup_prefill(values: dict) -> None:
    """Old configs only have wakeword_keywords; show the first live phrase
    per action in the new per-action word fields."""
    for key, action in _WAKE_WORD_ACTIONS:
        if key not in _CFG:
            for phrase, act in _parse_keywords(WAKEWORD_KEYWORDS):
                if act == action:
                    values[key] = phrase
                    break
    # populate key binds for setup editor
    if "_key_binds" not in values:
        values["_key_binds"] = [dict(b) for b in KEY_BINDINGS]


def _setup_main() -> int:
    """Interactive CLI menu (no curses — inherits the terminal theme)."""
    values = _field_defaults()
    values.update(_CFG)
    _setup_prefill(values)
    def _capture_key_cli() -> str | None:
        hint = "Press the key to bind (8s, Esc cancels)…"
        print(f" {hint}")
        try:
            import evdev, select
            devs = []
            for path in evdev.list_devices():
                try:
                    d = evdev.InputDevice(path)
                    caps = d.capabilities(verbose=False)
                    if caps.get(evdev.ecodes.EV_KEY):
                        devs.append(d)
                except Exception:
                    continue
            import time as _time
            deadline = _time.monotonic() + 8
            while _time.monotonic() < deadline:
                r,_,_ = select.select(devs, [], [], 0.1)
                for d in r:
                    try:
                        for ev in d.read():
                            if ev.type == evdev.ecodes.EV_KEY and ev.value == 1:
                                raw = None
                                if hasattr(evdev.ecodes, "KEY"):
                                    raw = evdev.ecodes.KEY.get(ev.code)
                                if raw is None:
                                    for attr in dir(evdev.ecodes):
                                        if attr.startswith("KEY_") and getattr(evdev.ecodes, attr) == ev.code:
                                            raw = attr; break
                                name = raw[4:].lower() if raw and raw.startswith("KEY_") else str(ev.code)
                                print(f"  captured: {name} ({_key_label(name)})")
                                return name
                    except Exception:
                        pass
        except Exception:
            pass
        return None

    def _pick_action_cli(prompt, default=""):
        opts = [("record","copy only"), ("record_send","copy+paste+Enter"), ("paste","paste clipboard"), ("","off")]
        # show numbered picker
        print(f"  {prompt}")
        for i,(v,lbl) in enumerate(opts, start=1):
            cur = " ←" if v==default else ""
            print(f"   {i}) {v or 'off':14} {lbl}{cur}")
        raw = input("  choose 1-4 [Enter keeps]: ").strip()
        if not raw:
            return default
        try:
            idx = int(raw)-1
            if 0 <= idx < len(opts):
                return opts[idx][0]
        except ValueError:
            low = raw.lower()
            if low in ("off","-",""): return ""
            if low in ("record","record_send","paste"): return low
        return default

    show_advanced = False
    message = ""
    while True:
        print("\n" + "─" * 60)
        print(f" shipboard setup — {DEFAULT_CONFIG_PATH}   (daemon: {'on' if _daemon_running() else 'off'})")
        print("─" * 60)
        # ── Keys quick-card (always on top — this is what people tweak most) ──
        binds = values.get("_key_binds") or []
        if binds:
            print("  Keys  (tap = short press, hold = long press, toggle = press to start/stop)")
            for idx, b in enumerate(binds, start=1):
                tap = b.get("tap",""); hold = b.get("hold",""); tog = b.get("toggle","")
                thr = b.get("hold_threshold", 0.25)
                parts = []
                if tog: parts.append(f"toggle={tog}")
                if hold: parts.append(f"hold {hold} @ {thr:g}s")
                if tap: parts.append(f"tap {tap}")
                hint = "  (toggle overrides tap)" if tog and (tap or hold) else ""
                print(f"   • {_key_label(b.get('key','')):<12}  {'  ·  '.join(parts) if parts else '(off)'}{hint}")
        else:
            print("  Keys  (none yet — add your first binding)")

        print("     [k] keys: add / edit / remove")
        # ── Essentials + Recording + collapsed sections ──
        last_section = None
        for i, (key, label, conv, section) in enumerate(_SETUP_FIELDS, start=1):
            is_adv = (section == "Advanced")
            if is_adv:
                if not show_advanced and section != last_section:
                    adv_count = sum(1 for _,_,_,s in _SETUP_FIELDS if s == "Advanced")
                    print(f"\n  … Advanced  ({adv_count} settings)  [a] show / hide")
                    last_section = section
                    continue
                elif not show_advanced:
                    last_section = section
                    continue
            if section != last_section:
                print(f"\n  == {section} ==")
                last_section = section
            mark = "*" if key in _CFG else " "
            print(f" {i:2}) {mark} {label:<40} {_fmt_value(values[key])}")
        print("─" * 60)
        print(" [num] edit setting  ·  [k] keys  ·  [a] advanced  ·  [s] save  ·  [t] test STT  ·  [p] compositor"
              "  ·  [r] restart  ·  [q] quit")
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
        if choice == "a":
            show_advanced = not show_advanced
            message = "advanced shown" if show_advanced else "advanced hidden"
        elif choice == "k":
            binds = values.setdefault("_key_binds", [])
            while True:
                print("\n" + "─"*50)
                print(" Keys  (SEPARATE PAGE — press to capture, then pick tap/hold/toggle one by one)")
                if not binds:
                    print("  (no keys — press a to add)")
                else:
                    for i, b in enumerate(binds, start=1):
                        tap = b.get("tap","") or "off"; hold = b.get("hold","") or "off"; tog = b.get("toggle","") or "off"; thr = b.get("hold_threshold",0.25)
                        print(f"  {i}. {_key_label(b['key']):12}  tap={tap:12}  hold={hold:12}@{thr:g}s  toggle={tog:12}")
                    print("  toggle overrides tap; hold+tap idempotent")
                print("  [a] add (press key)  [e] edit  [d] delete  [q] back")
                sub = input(" keys> ").strip().lower()
                if sub in ("q", "back", "b", ""):
                    break
                if sub in ("a", "1"):
                    cap = _capture_key_cli()
                    k = (cap or input(" key (e.g. rightalt/f13) > ").strip().lower())
                    if not k:
                        message = "no key given"
                    else:
                        try:
                            _key_code(k)
                        except SystemExit as e:
                            message = str(e)
                        else:
                            if any(b.get("key")==k for b in binds):
                                message = f"duplicate key {k!r} — each key once"
                            else:
                                tap = _pick_action_cli("tap (short press) —", "")
                                hold = _pick_action_cli("hold (long press) —", "")
                                tog = _pick_action_cli("toggle (press to start/stop) —", "")
                                thr_raw = input("  hold threshold [0.25] > ").strip() or "0.25"
                                try:
                                    thr = float(thr_raw)
                                    assert 0 < thr <= 5
                                except Exception:
                                    message = "bad threshold (0..5)"
                                else:
                                    binds.append({"key":k,"tap":tap,"hold":hold,"toggle":tog,"hold_threshold":thr})
                                    message = f"added {k}  tap={tap or 'off'} hold={hold or 'off'} toggle={tog or 'off'}"
                elif sub in ("e", "2"):
                    if not binds:
                        message = "no binds yet — add one first"
                    else:
                        for i,b in enumerate(binds, start=1):
                            print(f"  {i}) {_key_label(b['key']):12}  tap={b.get('tap','') or 'off'}  hold={b.get('hold','') or 'off'}  toggle={b.get('toggle','') or 'off'}  @{b.get('hold_threshold',0.25):g}s")
                        raw = input(" edit which > ").strip()
                        try:
                            idx = int(raw)-1
                            b = binds[idx]
                        except Exception:
                            message = "invalid index"
                        else:
                            print(f" editing {b['key']} — press new key or Enter keeps")
                            cap = _capture_key_cli()
                            nk = (cap or input(f"  key [{b['key']}] > ").strip().lower() or b['key'])
                            if nk != b['key'] and any(x.get("key")==nk for x in binds):
                                message = f"duplicate key {nk!r}"
                            else:
                                try:
                                    _key_code(nk)
                                except SystemExit as e:
                                    message = str(e)
                                else:
                                    b["tap"] = _pick_action_cli(f"tap (was {b.get('tap','') or 'off'}) —", b.get('tap',''))
                                    b["hold"] = _pick_action_cli(f"hold (was {b.get('hold','') or 'off'}) —", b.get('hold',''))
                                    b["toggle"] = _pick_action_cli(f"toggle (was {b.get('toggle','') or 'off'}) —", b.get('toggle',''))
                                    thr_raw = input(f"  threshold [{b.get('hold_threshold',0.25):g}] > ").strip()
                                    if thr_raw:
                                        try:
                                            b["hold_threshold"] = float(thr_raw)
                                        except Exception:
                                            message = "bad threshold, kept old"
                                    b["key"] = nk
                                    if "bad threshold" not in (message or ""):
                                        message = f"updated {nk}"
                elif sub in ("d", "3"):
                    if not binds:
                        message = "no binds"
                    else:
                        for i,b in enumerate(binds, start=1):
                            print(f"  {i}) {_key_label(b['key']):12} {b['key']}")
                        raw = input(" remove which > ").strip()
                        try:
                            idx = int(raw)-1
                            b = binds.pop(idx)
                            message = f"removed {b['key']}"
                        except Exception:
                            message = "invalid index"
                else:
                    if sub:
                        print(f"  unknown: {sub}")
        elif choice == "s":
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
                key, label, conv, _section = _SETUP_FIELDS[i]
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


def _tui_main() -> int:
    """Full-screen curses editor for _SETUP_FIELDS (alternative to `setup`).

    ↑/↓ navigate · Enter edit · Space/y/n toggle bools · s save ·
    t test STT · p compositor binds · r restart daemon · q quit.
    Falls back to `shipboard setup` when curses is unavailable.
    """
    try:
        import curses
    except ImportError:
        print("curses unavailable, use: shipboard setup", file=sys.stderr)
        return 1
    if os.environ.get("TERM") == "dumb" or not sys.stdin.isatty():
        print("curses unavailable, use: shipboard setup", file=sys.stderr)
        return 1

    values = _field_defaults()
    values.update(_CFG)
    _setup_prefill(values)
    show_advanced = False  # TUI also collapses Advanced by default

    def _build_rows():
        rows_ = []
        # Keys card as a navigable row (not a SETUP_FIELD)
        rows_.append(("keys", None))
        last_sec = None
        for i, (key, label, conv, section) in enumerate(_SETUP_FIELDS):
            if section == "Advanced" and not show_advanced:
                if last_sec != "Advanced":
                    rows_.append(("section", "… Advanced  [a] show/hide"))
                    last_sec = "Advanced"
                continue
            if section != last_sec:
                rows_.append(("section", section))
                last_sec = section
            rows_.append(("field", i))
        return rows_

    rows = _build_rows()

    def _next_field(sel: int, step: int) -> int:
        i = sel + step
        while 0 <= i < len(rows) and rows[i][0] not in ("field", "keys"):
            i += step
        return i if 0 <= i < len(rows) else sel

    def _keys_summary() -> str:
        binds = values.get("_key_binds") or []
        if not binds:
            return "Keys: (none yet — press k to add)"
        parts = []
        for b in binds:
            tap = b.get("tap",""); hold = b.get("hold",""); tog = b.get("toggle",""); thr = b.get("hold_threshold",0.25)
            segs = []
            if tog: segs.append(f"toggle={tog}")
            if hold: segs.append(f"hold {hold}@{thr:g}s")
            if tap: segs.append(f"tap {tap}")
            label = _key_label(b.get("key",""))
            parts.append(f"{label}: {' · '.join(segs) if segs else '(off)'}")
        return "Keys: " + "  |  ".join(parts)

    def _prompt_line(stdscr_, prompt, default=""):
        h_, w_ = stdscr_.getmaxyx()
        try:
            stdscr_.move(h_-1, 0); stdscr_.clrtoeol()
            stdscr_.addnstr(h_-1, 0, prompt + (f" [{default}]" if default else "") + " ", w_-1)
            stdscr_.noutrefresh(); curses.doupdate()
        except curses.error:
            pass
        curses.echo()
        try: curses.curs_set(1)
        except curses.error: pass
        try:
            raw = stdscr_.getstr(h_-1, len(prompt)+2 + (len(default)+3 if default else 1), 50)
            s = raw.decode(errors="replace").strip()
        except curses.error:
            s = ""
        finally:
            curses.noecho()
            try: curses.curs_set(0)
            except curses.error: pass
        return s if s else default

    def _capture_key(stdscr_) -> str | None:
        # Wait for next evdev press and return its key name; fallback to curses if no device
        hint = "Press the key to bind  (Esc cancels)…"
        h_, w_ = stdscr_.getmaxyx()
        try:
            stdscr_.move(h_-1, 0); stdscr_.clrtoeol()
            stdscr_.addnstr(h_-1, 0, hint, w_-1)
            stdscr_.noutrefresh(); curses.doupdate()
        except curses.error:
            pass
        try:
            import evdev
            import select
            wanted = set()
            devs = []
            for path in evdev.list_devices():
                try:
                    d = evdev.InputDevice(path)
                    caps = d.capabilities(verbose=False)
                    keys = caps.get(evdev.ecodes.EV_KEY, [])
                    if keys:
                        devs.append(d)
                except Exception:
                    continue
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                r, _, _ = select.select(devs, [], [], 0.1)
                for d in r:
                    try:
                        for ev in d.read():
                            if ev.type == evdev.ecodes.EV_KEY and ev.value == 1:
                                name = evdev.ecodes.keys.get(ev.code) or evdev.ecodes.KEY[ev.code] if ev.code in getattr(evdev.ecodes, "KEY", {}) else None
                                # ecodes.keys or KEY maps code -> "KEY_xxx"
                                raw = None
                                if hasattr(evdev.ecodes, "KEY"):
                                    raw = evdev.ecodes.KEY.get(ev.code)
                                if raw is None:
                                    # fallback via attr scan
                                    for attr in dir(evdev.ecodes):
                                        if attr.startswith("KEY_"):
                                            if getattr(evdev.ecodes, attr) == ev.code:
                                                raw = attr
                                                break
                                if raw and raw.startswith("KEY_"):
                                    name = raw[4:].lower()
                                else:
                                    name = str(ev.code)
                                return name
                    except Exception:
                        pass
                # also poll curses for Esc
                try:
                    stdscr_.nodelay(True)
                    ch = stdscr_.getch()
                    stdscr_.nodelay(False)
                    if ch == 27:
                        return None
                    if ch != -1:
                        stdscr_.nodelay(False)
                        # ignore
                        pass
                except curses.error:
                    pass
                try:
                    stdscr_.nodelay(False)
                except curses.error:
                    pass
            stdscr_.nodelay(False)
            return None
        except Exception:
            try:
                stdscr_.nodelay(False)
            except curses.error:
                pass
            # fallback: ask via curses key name
            try:
                stdscr_.move(h_-1, 0); stdscr_.clrtoeol()
                stdscr_.addnstr(h_-1, 0, "evdev unavailable — type key name: ", w_-1)
                stdscr_.noutrefresh(); curses.doupdate()
            except curses.error:
                pass
            return None

    def _choose_action(stdscr_, prompt, current=""):
        items = [("record", "copy only"), ("record_send", "copy+paste+Enter"), ("paste", "paste clipboard"), ("", "off")]
        sel = 0
        for i,(v,_) in enumerate(items):
            if v == current:
                sel = i; break
        while True:
            h_, w_ = stdscr_.getmaxyx()
            try:
                stdscr_.move(h_-1, 0); stdscr_.clrtoeol()
                line = prompt + "  "
                for i,(v,lbl) in enumerate(items):
                    tag = f"[{v or 'off'}:{lbl}]"
                    if i == sel:
                        tag = f"[{v or 'off'}]"
                    line += ("▶" if i==sel else " ") + tag + " "
                stdscr_.addnstr(h_-1, 0, line[:w-1], w_-1)
                stdscr_.addnstr(h_-1, max(0,w_-28), " ↑↓ choose · Enter ok · Esc cancel", 28)
                stdscr_.noutrefresh(); curses.doupdate()
            except curses.error:
                pass
            ch = stdscr_.getch()
            if ch in (curses.KEY_UP, curses.KEY_LEFT):
                sel = (sel-1) % len(items)
            elif ch in (curses.KEY_DOWN, curses.KEY_RIGHT, 9):  # Tab
                sel = (sel+1) % len(items)
            elif ch in (10,13,curses.KEY_ENTER, ord(" ")):
                return items[sel][0]
            elif ch == 27:
                return current

    def _edit_keys_flow(stdscr_) -> None:
        binds = values.setdefault("_key_binds", [])
        def _render_keys_page(sel=0):
            stdscr_.erase()
            h__, w__ = stdscr_.getmaxyx()
            try:
                stdscr_.addnstr(0, 0, " Keys  (SEPARATE PAGE — press to capture, then pick tap/hold/toggle) " + "─"*(max(0,w__-65)), w__-1, curses.A_REVERSE)
            except curses.error: pass
            if not binds:
                try: stdscr_.addnstr(2, 2, "(no keys — press a to add)", w__-4)
                except curses.error: pass
                y0 = 4
            else:
                y0 = 2
                for i, b in enumerate(binds):
                    tap = b.get("tap","") or "off"; hold = b.get("hold","") or "off"; tog = b.get("toggle","") or "off"; thr = b.get("hold_threshold",0.25)
                    line = f" {i+1}. {_key_label(b['key']):14}  tap={tap:12}  hold={hold:12}@{thr:g}s  toggle={tog:12}"
                    if i == sel:
                        try: stdscr_.addnstr(y0+i, 1, line[:w__-2], w__-2, curses.A_REVERSE)
                        except curses.error: pass
                    else:
                        try: stdscr_.addnstr(y0+i, 1, line[:w__-2], w__-2)
                        except curses.error: pass
                try: stdscr_.addnstr(y0+len(binds)+1, 2, "toggle overrides tap; hold+tap idempotent", w__-4, curses.A_DIM)
                except curses.error: pass
            hint = " a add (press key) · e edit · d delete · Esc/q back"
            try: stdscr_.addnstr(h__-1, 0, hint, w__-1)
            except curses.error: pass
            stdscr_.noutrefresh(); curses.doupdate()
        sel = 0
        _render_keys_page(sel)
        while True:
            ch = stdscr_.getch()
            if ch in (27, ord("q"), ord("Q")):
                return
            elif ch == curses.KEY_UP:
                if binds: sel = (sel - 1) % len(binds); _render_keys_page(sel)
            elif ch == curses.KEY_DOWN:
                if binds: sel = (sel + 1) % len(binds); _render_keys_page(sel)
            elif ch in (ord("a"), ord("A")):
                if len(binds) >= 3: continue
                name = _capture_key(stdscr_)
                if not name:
                    name = _prompt_line(stdscr_, "key name (e.g. rightalt)", "").strip().lower()
                    if not name: _render_keys_page(sel); continue
                try: _key_code(name)
                except SystemExit: _render_keys_page(sel); continue
                if any(b.get("key")==name for b in binds): _render_keys_page(sel); continue
                tap = _choose_action(stdscr_, "tap (short press) —", "")
                hold = _choose_action(stdscr_, "hold (long press) —", "")
                tog = _choose_action(stdscr_, "toggle (press to start/stop) —", "")
                thr_raw = _prompt_line(stdscr_, "hold threshold seconds", "0.25").strip() or "0.25"
                try: thr = float(thr_raw); assert 0 < thr <= 5
                except Exception: _render_keys_page(sel); continue
                binds.append({"key":name,"tap":tap,"hold":hold,"toggle":tog,"hold_threshold":thr})
                sel = len(binds)-1; _render_keys_page(sel)
            elif ch in (ord("e"), 10, 13, curses.KEY_ENTER, ord(" ")) and binds:
                b = binds[sel]
                rec = _capture_key(stdscr_)
                nk = rec if rec else _prompt_line(stdscr_, f"key [{b['key']}] — press key or Enter keeps", b['key']).strip().lower() or b['key']
                if nk != b['key'] and any(x.get("key")==nk for x in binds): _render_keys_page(sel); continue
                try: _key_code(nk)
                except SystemExit: _render_keys_page(sel); continue
                tap = _choose_action(stdscr_, f"tap (was {b.get('tap','') or 'off'}) →", b.get('tap',''))
                b["tap"] = tap
                hold = _choose_action(stdscr_, f"hold (was {b.get('hold','') or 'off'}) →", b.get('hold',''))
                b["hold"] = hold
                tog = _choose_action(stdscr_, f"toggle (was {b.get('toggle','') or 'off'}) →", b.get('toggle',''))
                b["toggle"] = tog
                thr_raw = _prompt_line(stdscr_, f"threshold [{b.get('hold_threshold',0.25):g}]", "").strip()
                if thr_raw:
                    try: b["hold_threshold"] = float(thr_raw)
                    except Exception: pass
                b["key"] = nk; _render_keys_page(sel)
            elif ch in (ord("d"), ord("x"), curses.KEY_DC, 127) and binds:
                binds.pop(sel)
                if sel >= len(binds) and sel > 0: sel -= 1
                _render_keys_page(sel)

    def _show_binds(stdscr) -> None:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        for y, ln in enumerate(_COMPOSITOR_SNIPPETS.splitlines()):
            if y >= h - 1:
                break
            try:
                stdscr.addnstr(y, 0, ln, w - 1)
            except curses.error:
                pass
        try:
            stdscr.addnstr(h - 1, 0, " q / Esc to close ", w - 1, curses.A_REVERSE)
        except curses.error:
            pass
        stdscr.noutrefresh()
        curses.doupdate()
        while True:
            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q"), 27):
                break

    def _edit_field(stdscr, sel: int, message: str) -> str:
        field = rows[sel][1]
        key, label, conv, _section = _SETUP_FIELDS[field]
        h, w = stdscr.getmaxyx()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        if conv is bool:
            prompt = f" {label} [{_fmt_value(values[key])}]   space/y = yes · n = no · Esc = keep"
            try:
                stdscr.move(h - 1, 0)
                stdscr.clrtoeol()  # clear the navigation hint line first
                stdscr.addnstr(h - 1, 0, prompt, w - 1)
                stdscr.noutrefresh()
                curses.doupdate()
            except curses.error:
                pass
            while True:
                ch = stdscr.getch()
                if ch in (27,):
                    break
                if ch == ord(" "):
                    values[key] = not values[key]
                    break
                if ch in (ord("y"), ord("Y")):
                    values[key] = True
                    break
                if ch in (ord("n"), ord("N")):
                    values[key] = False
                    break
            message = f"{key} = {_fmt_value(values[key])}"
        else:
            buf = _fmt_value(values[key])
            while True:
                prompt = f" {label} [{buf}]"
                try:
                    stdscr.move(h - 1, 0)
                    stdscr.clrtoeol()  # clear the navigation hint line first
                    stdscr.addnstr(h - 1, 0, prompt, w - 1)
                    stdscr.noutrefresh()
                    curses.doupdate()
                except curses.error:
                    pass
                ch = stdscr.getch()
                if ch in (27,):
                    break
                if ch in (10, 13, curses.KEY_ENTER):
                    try:
                        values[key] = _parse_value(buf, conv)
                        message = f"{key} = {_fmt_value(values[key])}"
                    except ValueError:
                        message = "invalid value, not saved"
                    break
                if ch in (8, 127, curses.KEY_BACKSPACE):
                    buf = buf[:-1]
                elif 32 <= ch < 127:
                    buf += chr(ch)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        return message

    def run(stdscr) -> int:
        nonlocal rows, show_advanced
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        sel, scroll = 0, 0
        while sel < len(rows) and rows[sel][0] not in ("field", "keys"):
            sel += 1
        message = "↑/↓ move · Enter/k edit keys · a advanced · s save · t test · p compositor · r restart · q quit"
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            positions = []
            y = 1
            for kind, payload in rows:
                positions.append((kind, payload, y))
                y += 1
            sel_y = positions[sel][2]
            if sel_y - scroll < 1:
                scroll = sel_y - 1
            elif sel_y - scroll > h - 3:
                scroll = sel_y - (h - 3)
            title = f" shipboard setup — {DEFAULT_CONFIG_PATH}  ({'on' if _daemon_running() else 'off'}) "
            try:
                stdscr.addnstr(0, 0, title, w - 1, curses.A_REVERSE)
            except curses.error:
                pass
            for kind, payload, py in positions:
                yy = py - scroll
                if yy < 1 or yy >= h - 1:
                    continue
                try:
                    if kind == "section":
                        stdscr.addnstr(yy, 0, f"  == {payload} ==", w - 1,
                                       curses.A_BOLD)
                    elif kind == "keys":
                        selected = rows[sel][0] == "keys"
                        attrs = curses.A_REVERSE if selected else curses.A_BOLD
                        stdscr.addnstr(yy, 0, f"  {_keys_summary()}", w - 1, attrs)
                    else:
                        key, label, conv, _section = _SETUP_FIELDS[payload]
                        mark = "*" if key in _CFG else " "
                        selected = kind == "field" and payload == rows[sel][1]
                        attrs = curses.A_REVERSE if selected else 0
                        stdscr.addnstr(yy, 0,
                                       f" {mark} {label:<46} {_fmt_value(values[key])}",
                                       w - 1, attrs)
                except curses.error:
                    pass
            try:
                stdscr.addnstr(h - 1, 0, message, w - 1)
            except curses.error:
                pass
            stdscr.noutrefresh()
            curses.doupdate()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP,):
                sel = _next_field(sel, -1)
            elif ch in (curses.KEY_DOWN,):
                sel = _next_field(sel, 1)
            elif ch in (curses.KEY_RESIZE,):
                continue
            elif ch in (10, 13, curses.KEY_ENTER):
                if rows[sel][0] == "keys":
                    _edit_keys_flow(stdscr)
                    message = "keys: " + _keys_summary()
                else:
                    message = _edit_field(stdscr, sel, message)
            elif ch == ord("k"):
                _edit_keys_flow(stdscr)
                message = "keys: " + _keys_summary()
            elif ch == ord("a"):
                show_advanced = not show_advanced
                rows = _build_rows()
                sel = 0
                while sel < len(rows) and rows[sel][0] not in ("field", "keys"):
                    sel += 1
                message = "advanced shown" if show_advanced else "advanced hidden"
            elif ch == ord("s"):
                _save_config_file(values)
                message = "saved to ~/.config/shipboard/shipboard.toml - restart daemon to apply (r)"
            elif ch == ord("t"):
                message = "STT health: " + _health_check(values["whisper_health_url"])
            elif ch == ord("p"):
                _show_binds(stdscr)
            elif ch == ord("r"):
                message = _restart_daemon()
            elif ch in (ord("q"), ord("Q")):
                return 0
        return 0

    try:
        return curses.wrapper(run)
    except KeyboardInterrupt:
        return 0


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------
def _daemon_main() -> int:
    lock_fh = open(DAEMON_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("shipboard: daemon already running", file=sys.stderr)
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


def _start_daemon() -> int:
    """Start the daemon detached; alias for the `daemon`/`start` subcommands."""
    if _daemon_pids():
        print("shipboard: daemon already running", file=sys.stderr)
        return 0
    subprocess.Popen(
        [sys.executable, str(Path(sys.argv[0]).resolve())],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("shipboard: daemon started (detached)")
    return 0


def _stop_daemon() -> int:
    """SIGTERM every daemon process (the `stop` subcommand)."""
    pids = _daemon_pids()
    if not pids:
        print("shipboard: no daemon running", file=sys.stderr)
        return 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    print(f"shipboard: stopped daemon ({len(pids)} process(es))")
    return 0


def _restart_main() -> int:
    """Restart the daemon (the `restart` subcommand)."""
    print(f"shipboard: {_restart_daemon()}")
    return 0


def _send_main() -> int:
    """One-shot paste + Enter (manual/backup trigger, e.g. a WM bind)."""
    # Refuse while a recording is in flight so stale content never pastes.
    try:
        lock_fh = open(LOCK_PATH, "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fh.close()
    except OSError:
        _notify("shipboard", "Recording in progress — skipping paste")
        return 0
    try:
        send_keys(enter=SCROLL_SEND_ENTER)
    except Exception as exc:
        _notify("shipboard", f"Failed to send: {exc}")
        return 1
    return 0


def main() -> int:
    if sys.argv[1:2] == ["status"]:
        return _status_main()
    if sys.argv[1:2] == ["config"]:
        return _config_main()
    if sys.argv[1:2] == ["setup"]:
        return _setup_main()
    if sys.argv[1:2] in (["tui"], ["setup-tui"]):
        return _tui_main()
    if sys.argv[1:2] in (["daemon"], ["start"]):
        return _start_daemon()
    if sys.argv[1:2] == ["stop"]:
        return _stop_daemon()
    if sys.argv[1:2] == ["restart"]:
        return _restart_main()

    parser = argparse.ArgumentParser(
        description=(
            "shipboard: voice daemon (Pause/Scroll Lock) + helpers.\n"
            "CLI subcommands: daemon/start (run detached), stop (SIGTERM), "
            "restart (systemd or respawn), status (state), "
            "config (TOML editor), --send (paste+Enter).\n"
            "Interactive: setup (numbered CLI dialog), "
            "tui/setup-tui (full-screen curses setup)."
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
                _notify("shipboard", "Processing speech...")
                try:
                    text = transcribe(wav)
                except Exception as exc:
                    _notify("shipboard", f"Recognition error: {exc}")
                    return 1
                text = normalize_text(text)
                if args.no_copy:
                    print(text)
                else:
                    copy_to_clipboard(text)
                    _notify("shipboard", f"Copied: {text[:100]}")
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
