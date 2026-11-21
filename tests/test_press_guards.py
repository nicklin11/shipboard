#!/usr/bin/env python3
"""Debounce and stop-guard keep accidental / too-fast presses harmless."""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from shipboard import daemon as sbd  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


sbd.KEY_DEBOUNCE = 0.05
sbd.STOP_GUARD = 0.6
sbd.TAP_STOP_SILENCE = 0  # no silence-watcher thread


def make(bind):
    d = object.__new__(sbd._Daemon)
    d.recording = False
    d.rec_t0 = 0.0
    d.autosend = False
    d._rec_key = d._rec_mode = None
    d._rec_gen = 0
    d._key_down, d._hold_fired, d._pending_hold = {}, set(), {}
    d._last_key_event, d._ignored_down = {}, set()
    d.finished = 0
    d._binding_for = lambda code: bind

    def start(key, mode):
        d.recording, d.rec_t0, d._rec_key, d._rec_mode = True, time.monotonic(), key, mode

    def finish(from_wake=False):
        d.finished += 1
        d.recording = False

    d._start_record, d._finish_record = start, finish
    return d


# --- debounce -----------------------------------------------------------------
d = make(None)
check("first press accepted", d._debounced(1, 10.00) is False)
check("release of accepted press recorded", d._note_release(1, 10.01) is True)
check("press 20 ms after release is bounce", d._debounced(1, 10.03) is True)
check("its release is swallowed", d._note_release(1, 10.04) is False)
check("press 100 ms later is real", d._debounced(1, 10.15) is False)
check("other keys are independent", d._debounced(2, 10.16) is False)
sbd.KEY_DEBOUNCE = 0
d = make(None)
d._debounced(1, 5.0)
check("key_debounce=0 disables", d._debounced(1, 5.0001) is False)
sbd.KEY_DEBOUNCE = 0.05

# --- stop guard: toggle -------------------------------------------------------
bind = {"key": "rightalt", "toggle": "record", "tap": "", "hold": ""}
d = make(bind)
d._on_key_press(1)
check("toggle press starts", d.recording and d._rec_mode == "toggle")
d._on_key_press(1)
check("fast second press does not stop", d.recording and d.finished == 0)
d.rec_t0 -= 1.0
d._on_key_press(1)
check("press after the guard stops", not d.recording and d.finished == 1)

# --- stop guard: tap-only (acts on release) ------------------------------------
bind = {"key": "pause", "tap": "record", "hold": "", "toggle": ""}
d = make(bind)
d._key_down[1] = 0
d._on_key_release(1)
check("tap release starts", d.recording and d._rec_mode == "tap")
d._key_down[1] = 0
d._on_key_release(1)
check("fast second tap does not stop", d.recording and d.finished == 0)
d.rec_t0 -= 1.0
d._key_down[1] = 0
d._on_key_release(1)
check("later tap stops", not d.recording and d.finished == 1)

# --- stop guard: hold release is never blocked ---------------------------------
bind = {"key": "k", "hold": "record", "tap": "", "toggle": ""}
d = make(bind)
d._start_record("k", "hold")
d._hold_fired.add(1)
d._on_key_release(1)
check("hold release stops immediately", d.finished == 1)

# --- stop_guard=0 disables ------------------------------------------------------
sbd.STOP_GUARD = 0
bind = {"key": "rightalt", "toggle": "record", "tap": "", "hold": ""}
d = make(bind)
d._on_key_press(1)
d._on_key_press(1)
check("stop_guard=0 allows instant stop", d.finished == 1)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all press-guard checks passed")
