"""Configuration: defaults < TOML config file < environment variables."""

from __future__ import annotations

import os
import re
from pathlib import Path

from .keys import _key_code

# --------------------------------------------------------------------------
# Configuration: defaults < TOML config file < environment variables
# --------------------------------------------------------------------------
DEFAULT_CONFIG_PATH = Path(
    "~/.config/shipboard/shipboard.toml"
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
# Tap-started recording auto-stops after this much silence (single-press
# flow: tap -> speak -> quiet -> processed). 0 disables. Default follows
# wakeword_stop_silence so mid-speech pauses behave the same everywhere.
TAP_STOP_SILENCE = float(_cfg("tap_stop_silence", "SHIPBOARD_TAP_STOP_SILENCE",
                              WAKEWORD_STOP_SILENCE, float))
TAP_START_GRACE = 1.0  # seconds after the tap before silence may count
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


# --------------------------------------------------------------------------
# Setup fields — structured section metadata (drives the categorized
# nested menus in setup_tui: CLI + TUI mirror the same tree).
#
# Home screen shows the 5 named sections + "Advanced" collapsed behind [a].
# Field tuple: (key, label, description, conv, accepted-format hint).
# Section 1 ("Recording & keys") additionally holds the [[key_bind]] list,
# edited as binds inside setup_tui (press-to-capture + action pickers).
# --------------------------------------------------------------------------

SECTION_RECORDING = "Recording & keys"
SECTION_STT = "STT / whisper"
SECTION_WAKE = "Wake words"
SECTION_SENDING = "Sending"
SECTION_SYSTEM = "System"
SECTION_ADVANCED = "Advanced"

_SETUP_SECTIONS = [
    (SECTION_RECORDING, (
        ("max_hold", "Max hold",
         "Force-finish a stuck recording after this long", float, "seconds"),
        ("min_recording", "Min recording",
         "Shorter recordings are discarded as noise", float, "seconds"),
        ("tap_stop_silence", "Tap stop silence",
         "Tap-started recording auto-stops after this much silence",
         float, "seconds, 0 disables"),
    )),
    (SECTION_STT, (
        ("whisper_url", "STT server URL",
         "Inference endpoint the daemon posts audio to", str, "http URL"),
        ("whisper_health_url", "Health URL",
         "Endpoint probed by 'test STT' and the status line", str, "http URL"),
        ("whisper_container", "Container",
         "Docker container to wake before recording", str, "container name"),
        ("whisper_language", "Language",
         "Whisper decode language", str, "auto / ru / en / ..."),
        ("prompt", "Prompt",
         "Optional initial hint sent with every request (domain vocabulary)",
         str, "text"),
        ("normalize", "Normalize text",
         'Rewrite spoken phrases into symbols ("тильда слэш" -> "~/.config")',
         bool, "y / n"),
    )),
    (SECTION_WAKE, (
        ("wakeword_enabled", "Enabled",
         "Listen for wake phrases in the background (engine WIP)", bool, "y / n"),
        ("wakeword_record", "Phrases: record",
         "Say these to record to clipboard only", str, "comma-separated phrases"),
        ("wakeword_send", "Phrases: send",
         "Say these to record + paste + Enter", str, "comma-separated phrases"),
        ("wakeword_paste", "Phrases: paste",
         "Say these to paste the clipboard", str, "comma-separated phrases"),
        ("wakeword_sherpa_threshold", "Trigger threshold",
         "Sherpa KWS trigger bar the keywords must clear",
         float, "0..1, lower = easier"),
        ("wakeword_sherpa_score", "Keyword score boost",
         "Boost applied to matched keywords", float, "e.g. 1.0"),
        ("wakeword_grace", "Grace",
         "Keep recording through silence right after a trigger", float, "seconds"),
        ("wakeword_stop_silence", "Stop on silence",
         "Seconds of silence that end a wake recording", float, "seconds"),
        ("wakeword_cooldown", "Cooldown",
         "Ignore re-triggers for this long", float, "seconds"),
        ("wakeword_debug", "Debug log",
         "Log the mic level every second", bool, "y / n"),
    )),
    (SECTION_SENDING, (
        ("paste_combo", "Paste combo",
         "Modifier combo injected to paste (layout-proof)", str, "e.g. ctrl+shift+v"),
        ("send_enter", "Enter after paste",
         "Also press Enter after pasting", bool, "y / n"),
        ("scroll_send_enter", "Enter after paste: Scroll Lock",
         "Per-trigger Enter override for the Scroll Lock send", bool, "y / n"),
        ("both_send_enter", "Enter after paste: combined",
         "Per-trigger Enter override for the paste-after-record path", bool, "y / n"),
    )),
    (SECTION_SYSTEM, (
        ("record_target", "Mic source",
         "Recording source; default = the system default source",
         str, 'device name or "default"'),
        ("record_rate", "Sample rate",
         "Sample rate for pw-record and the wake listener", int, "Hz, e.g. 16000"),
        ("record_channels", "Channels",
         "Channels for pw-record and the wake listener", int, "1 or 2"),
        ("keep_audio_dir", "Keep recordings",
         "Save raw recordings here instead of deleting", str, "directory, empty = delete"),
        ("dry_run", "Dry run",
         "Log actions but do not inject keys or paste", bool, "y / n"),
    )),
    (SECTION_ADVANCED, (
        ("inject_backend", "Inject backend",
         "Key injection implementation", str, "auto / uinput / wtype"),
        ("notify_backend", "Notify backend",
         "Desktop notification implementation", str, "auto / notify-send / ..."),
        ("clipboard_backend", "Clipboard backend",
         "Clipboard implementation", str, "auto / wl-copy / xclip"),
        ("record_backend", "Record backend",
         "Audio recording implementation", str, "auto / pw-record / ffmpeg"),
        ("input_device_glob", "Input device glob",
         "Restrict evdev device discovery by glob", str, "e.g. event*, empty = all"),
        ("kws_threads", "KWS threads",
         "onnxruntime threads for the wake listener", int, "number"),
        ("wakeword_silence_level", "Silence RMS level",
         "RMS below this counts as silence for wake recordings", float, "0..32768"),
        ("wakeword_action", "Wake action",
         "Action for wake words configured via wakeword_keywords only",
         str, "record / record_send"),
        ("idle_marker", "Idle marker",
         "File touched when whisper goes idle", str, "path"),
        ("lock_path", "Lock path",
         "Flock file guarding a single recording", str, "path"),
        ("daemon_lock_path", "Daemon lock path",
         "Flock file guarding a single daemon instance", str, "path"),
    )),
]

# Flat field table: (key, label, conv, section, description, format-hint).
# Emitted by _save_config_file in this order — [[key_bind]] tables still go
# LAST so bare keys after them are not swallowed by the TOML array.
_SETUP_FIELDS = [
    (key, label, conv, section, desc, fmt)
    for section, fields in _SETUP_SECTIONS
    for (key, label, desc, conv, fmt) in fields
]

# Fields with a small known value set: setup TUI shows a popup list instead
# of a free-text prompt. record_target is special-cased in setup_tui.py
# (choices come from `pactl list sources short` at runtime).
_FIELD_CHOICES = {
    "inject_backend": ["auto", "uinput", "wtype", "pynput"],
    "notify_backend": ["auto", "notify-send", "osascript", "powershell"],
    "clipboard_backend": ["auto", "wl-copy", "xclip", "pbcopy", "clip"],
    "record_backend": ["auto", "pw-record", "ffmpeg"],
    "wakeword_action": ["record", "record_send"],
}


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
        "tap_stop_silence": 1.5,
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
        # per-trigger "Enter after paste" overrides (Sending section)
        "scroll_send_enter": True,
        "both_send_enter": True,
        # Advanced paths (same defaults as the module constants above)
        "idle_marker": "/tmp/whisper-local-last-use",
        "lock_path": "/tmp/shipboard.lock",
        "daemon_lock_path": "/tmp/shipboard.daemon.lock",
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
    for key, _label, conv, _section, _desc, _fmt in _SETUP_FIELDS:
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


def _setup_prefill(values: dict) -> None:
    """Old configs only have wakeword_keywords; show the first live phrase
    per action in the new per-action word fields."""
    from .wake import _parse_keywords  # deferred: config must not import wake at module level (cycle)
    for key, action in _WAKE_WORD_ACTIONS:
        if key not in _CFG:
            for phrase, act in _parse_keywords(WAKEWORD_KEYWORDS):
                if act == action:
                    values[key] = phrase
                    break
    # populate key binds for setup editor
    if "_key_binds" not in values:
        values["_key_binds"] = [dict(b) for b in KEY_BINDINGS]
