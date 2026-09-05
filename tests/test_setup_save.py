#!/usr/bin/env python3
"""Setup regression harness: save roundtrip + categorized menu model.

Replaces the (lost) /tmp/shipboard_setup_test.py. Covers:
  * _SETUP_SECTIONS/_SETUP_FIELDS consistency + prefill completeness
  * _save_config_file -> tomllib reparse: [[key_bind]] tables LAST (no
    bare-key swallowing), bool/int/float/str conversions, bind roundtrip
  * the CLI menu (`_setup_main`) driven through a piped stdin: home shows
    the 5 sections + collapsed Advanced, section 1 holds the keys list,
    field edit / bind add-edit-remove / unknown-command paths all execute

Runs standalone (`python3 tests/test_setup_save.py`) or under pytest.
"""
import io
import re
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from shipboard import config  # noqa: E402
from shipboard import setup_tui as st  # noqa: E402

fails = []


def check(cond, what):
    if not cond:
        fails.append(what)


# ---------------------------------------------------------------------------
# Hermetic patches: never touch the real ~/.config/shipboard/shipboard.toml
# ---------------------------------------------------------------------------
_tmpdir = tempfile.mkdtemp(prefix="shipboard-setup-test-")
_TMP_CONFIG = Path(_tmpdir) / "shipboard.toml"

_orig = {name: getattr(config, name) for name in
         ("DEFAULT_CONFIG_PATH", "_CFG", "KEY_BINDINGS", "WAKEWORD_KEYWORDS")}
config.DEFAULT_CONFIG_PATH = _TMP_CONFIG
config._CFG = {}
config.KEY_BINDINGS = []
config.WAKEWORD_KEYWORDS = ""
st.DEFAULT_CONFIG_PATH = _TMP_CONFIG
st._CFG = {}
st._health_check = lambda url, timeout=3.0: "FAIL: (stubbed)"  # noqa: E731
st._daemon_running = lambda: False  # noqa: E731
st._capture_key_cli = lambda: None  # noqa: E731  (no evdev capture in tests)

# ---------------------------------------------------------------------------
# 1. FIELDS metadata: sectioned, complete, spec tree order
# ---------------------------------------------------------------------------
SECTION_ORDER = [s for s, _fields in config._SETUP_SECTIONS]
check(SECTION_ORDER == ["Recording & keys", "STT / whisper", "Wake words",
                        "Sending", "System", "Advanced"],
      f"section order wrong: {SECTION_ORDER}")
check(st.TOP_SECTIONS == ("Recording & keys", "STT / whisper", "Wake words",
                          "Sending", "System"),
      "home must show exactly the 5 non-Advanced sections")

flat_sections = list(dict.fromkeys(f[3] for f in config._SETUP_FIELDS))
check(flat_sections == SECTION_ORDER,
      "_SETUP_FIELDS must be grouped contiguously in section order")

field_keys = [f[0] for f in config._SETUP_FIELDS]
check(len(field_keys) == len(set(field_keys)), "duplicate field keys")
defaults = config._field_defaults()
check(set(field_keys) == set(defaults),
      f"fields/defaults mismatch: {set(field_keys) ^ set(defaults)}")

# every field carries label + description + format hint (spec: name, what it
# does, current value, accepted format)
for f in config._SETUP_FIELDS:
    check(len(f) == 6 and f[1] and f[4] and f[5],
          f"field {f[0]!r} missing label/description/format hint")

# spec menu tree placement spot-checks
_expect_section = {
    "max_hold": "Recording & keys",
    "min_recording": "Recording & keys",
    "tap_stop_silence": "Recording & keys",
    "whisper_url": "STT / whisper",
    "normalize": "STT / whisper",
    "prompt": "STT / whisper",
    "wakeword_enabled": "Wake words",
    "wakeword_sherpa_threshold": "Wake words",
    "wakeword_sherpa_score": "Wake words",
    "wakeword_cooldown": "Wake words",
    "wakeword_debug": "Wake words",
    "paste_combo": "Sending",
    "send_enter": "Sending",
    "scroll_send_enter": "Sending",
    "both_send_enter": "Sending",
    "record_target": "System",
    "record_rate": "System",
    "keep_audio_dir": "System",
    "dry_run": "System",
    "inject_backend": "Advanced",
    "input_device_glob": "Advanced",
    "kws_threads": "Advanced",
    "wakeword_silence_level": "Advanced",
    "wakeword_action": "Advanced",
    "idle_marker": "Advanced",
    "lock_path": "Advanced",
}
for key, section in _expect_section.items():
    got = next((f[3] for f in config._SETUP_FIELDS if f[0] == key), None)
    check(got == section, f"{key} should live in {section!r}, found {got!r}")

# nothing from the old flat table was dropped
_old_field_keys = {
    "paste_combo", "send_enter", "normalize", "prompt", "whisper_language",
    "max_hold", "min_recording", "record_target", "whisper_url",
    "whisper_health_url", "whisper_container", "record_rate",
    "record_channels", "wakeword_enabled", "wakeword_cooldown",
    "wakeword_grace", "tap_stop_silence", "wakeword_stop_silence",
    "wakeword_action", "wakeword_silence_level", "wakeword_sherpa_score",
    "wakeword_sherpa_threshold", "kws_threads", "wakeword_record",
    "wakeword_send", "wakeword_paste", "wakeword_debug", "keep_audio_dir",
    "dry_run", "inject_backend", "notify_backend", "clipboard_backend",
    "record_backend", "input_device_glob",
}
check(_old_field_keys <= set(field_keys),
      f"dropped fields: {_old_field_keys - set(field_keys)}")

# ---------------------------------------------------------------------------
# 2. prefill completeness
# ---------------------------------------------------------------------------
values = config._field_defaults()
config._setup_prefill(values)
check(isinstance(values.get("_key_binds"), list), "prefill: _key_binds missing")
check(all(k in values for k in field_keys),
      "prefill incomplete vs _SETUP_FIELDS")

# ---------------------------------------------------------------------------
# 3. save -> reparse roundtrip
# ---------------------------------------------------------------------------
values = config._field_defaults()
values.update({
    "paste_combo": "ctrl+alt+v",          # str
    "send_enter": False,                  # bool
    "normalize": True,
    "record_rate": 48000,                 # int
    "kws_threads": 4,
    "max_hold": 12.5,                     # float
    "min_recording": 0.75,
    "wakeword_silence_level": 250.0,
    "tap_stop_silence": 0.0,
    "prompt": "тест: Docker, config",
})
values["_key_binds"] = [
    {"key": "rightalt", "tap": "record", "hold": "record_send",
     "toggle": "", "hold_threshold": 0.25},
    {"key": "f13", "tap": "", "hold": "", "toggle": "paste",
     "hold_threshold": 0.4},
]
config._save_config_file(values)
raw = _TMP_CONFIG.read_text()
parsed = tomllib.loads(raw)

# ordering: every bare key BEFORE the first [[key_bind]]; last chunk is a bind
first_bind = raw.index("[[key_bind]]")
for key in field_keys:
    check(raw.index(f"\n{key} = ") < first_bind,
          f"{key} emitted after [[key_bind]] (would be swallowed)")
check(raw.rindex('wakeword_keywords = "') < first_bind,
      "composed wakeword_keywords must be emitted before [[key_bind]]")
chunks = [c for c in raw.split("\n\n") if c.strip()]
check(chunks[-1].startswith("[[key_bind]]"),
      "last TOML chunk must be a [[key_bind]] table")
check(all(re.findall(r"^\[\[([^\]]+)\]\]", c, re.M) == ["key_bind"]
          for c in chunks if "[[" in c),
      "only [[key_bind]] table headers expected")

# no bare keys swallowed into the array tables
_allowed_bind_keys = {"key", "tap", "hold", "toggle", "hold_threshold"}
binds = parsed.get("key_bind", [])
check(len(binds) == 2, f"expected 2 binds, got {len(binds)}")
for b in binds:
    check(set(b) <= _allowed_bind_keys,
          f"bare key swallowed into [[key_bind]]: {set(b) - _allowed_bind_keys}")

# conversions survive the roundtrip with the right TOML types
check(parsed["paste_combo"] == "ctrl+alt+v"
      and isinstance(parsed["paste_combo"], str), "str roundtrip: paste_combo")
check(parsed["send_enter"] is False and isinstance(parsed["send_enter"], bool),
      "bool roundtrip: send_enter")
check(parsed["record_rate"] == 48000
      and isinstance(parsed["record_rate"], int), "int roundtrip: record_rate")
check(parsed["kws_threads"] == 4 and isinstance(parsed["kws_threads"], int),
      "int roundtrip: kws_threads")
check(parsed["max_hold"] == 12.5
      and isinstance(parsed["max_hold"], float), "float roundtrip: max_hold")
check(parsed["min_recording"] == 0.75, "float roundtrip: min_recording")
check(parsed["wakeword_silence_level"] == 250.0,
      "float roundtrip: wakeword_silence_level")
check(parsed["tap_stop_silence"] == 0.0, "float roundtrip: tap_stop_silence")
check(parsed["prompt"] == "тест: Docker, config", "unicode prompt roundtrip")
check(parsed["whisper_url"] == "http://127.0.0.1:10300/inference",
      "default str preserved")

# the sample bind: rightalt tap=record, hold=record_send
# (save omits hold_threshold when it equals the 0.25 default — parser refills it)
b0 = dict(binds[0])
b0.setdefault("hold_threshold", config._HOLD_THRESHOLD_DEFAULT)
check(b0 == {"key": "rightalt", "tap": "record", "hold": "record_send",
             "hold_threshold": 0.25},
      f"rightalt bind roundtrip: {binds[0]}")
check(binds[1]["key"] == "f13" and binds[1]["toggle"] == "paste"
      and binds[1]["hold_threshold"] == 0.4,
      f"f13 bind roundtrip: {binds[1]}")
# and it parses back through the strict daemon-side parser
check(len(config._parse_key_bindings(parsed)) == 2,
      "reparsed binds rejected by _parse_key_bindings")

# ---------------------------------------------------------------------------
# 4. CLI menu drive: home -> section 1 -> field edit -> save (piped stdin)
# ---------------------------------------------------------------------------
def _drive(feeds):
    """Run _setup_main with a piped stdin; return (stdout, saved parse).
    Asserts every fed line was actually consumed (no prompt/feed drift)."""
    out = io.StringIO()
    stdin = io.StringIO("".join(f + "\n" for f in feeds))
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = stdin, out
    try:
        rc = st._setup_main()
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    check(rc == 0, "CLI drive must exit 0")
    check(stdin.read() == "", "CLI drive did not consume all fed lines")
    return out.getvalue(), tomllib.loads(_TMP_CONFIG.read_text())


# 4a. field edit path (section 1 -> field 2 = min_recording -> 0.4)
out, parsed = _drive(["1", "2", "0.4", "q", "s", "q"])
check("Recording & keys" in out, "home: section 1 not shown")
check("Advanced — collapsed" in out, "home: Advanced not collapsed")
check("daemon: off" in out, "home: daemon status line missing")
check("stt: FAIL: (stubbed)" in out, "home: stt status line missing")
check("== Recording & keys ==" in out, "section 1 page not rendered")
check("keys — one line per [[key_bind]]" in out, "keys list missing in section 1")
check("[n] add bind" in out and "[e] edit bind" in out,
      "bind commands missing in section 1")
check(parsed["min_recording"] == 0.4
      and isinstance(parsed["min_recording"], float),
      f"CLI field edit not saved: {parsed.get('min_recording')}")

# 4b. bind add -> edit -> remove (capture stubbed to None -> typed key name)
out, parsed = _drive(["1", "n", "f13", "1", "", "4", "0.3",
                      "e", "1", "", "3", "", "", "",
                      "d", "1", "q", "s", "q"])
check("added f13" in out, "bind add path did not run")
check("updated f13" in out, "bind edit path did not run")
check("removed f13" in out, "bind remove path did not run")
check("choose 1-4" in out, "action picker not rendered")
check("hold threshold" in out, "threshold prompt not rendered")
check("key_bind" not in parsed, "removed bind must not be saved")

# 4c. invalid inputs: unknown command, bad field number, bad field value
out, _parsed = _drive(["9", "2", "99", "", "3", "5", "abc", "", "q"])
check("unknown command: 9" in out, "unknown command path")
check("no field #99" in out, "bad field number path")
check("invalid value, not saved" in out, "bad field value path")
check("== STT / whisper ==" in out, "section 2 page not rendered")

# 4d. pure helpers
b = {"key": "rightalt", "tap": "record", "hold": "record_send",
     "toggle": "", "hold_threshold": 0.25}
check(st._bind_summary(b) == "hold=record_send@0.25s · tap=record",
      f"bind summary: {st._bind_summary(b)}")
check(st._bind_summary({"key": "x"}) == "(off)", "empty bind summary")
check("no keys yet" in st._binds_line({}), "empty binds line")
check(len(st._section_fields("Wake words")) == 10, "wake words section size")

# ---------------------------------------------------------------------------
if fails:
    print("FAIL:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("OK: setup save roundtrip + categorized menu model "
      f"({len(field_keys)} fields, {len(SECTION_ORDER)} sections, "
      "CLI drive: home/section/field-edit/bind add-edit-remove/bad input)")
