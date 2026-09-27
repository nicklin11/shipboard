#!/usr/bin/env python3
"""Regression: "Processing speech..." names its trigger, and a cycle is
finished exactly once (issue #1: an unattributed notification surfaced when
a toggle-started recording hung until max_hold).

Stubs the side-effecting module functions (notify, stop, STT, state) and
drives the REAL _finish_record.
"""
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from shipboard import daemon as sbd  # noqa: E402

notifs: list[str] = []
stops = {"n": 0}
transcribes = {"n": 0}

sbd._notify = lambda title, msg: notifs.append(msg)
sbd.stop_recording = lambda proc: stops.__setitem__("n", stops["n"] + 1)
sbd._write_state = lambda **kw: None
sbd._log = lambda msg: None


def _fake_transcribe(wav):
    transcribes["n"] += 1
    return "hello", "hello"


sbd._transcribe_copy = _fake_transcribe
sbd.KEEP_AUDIO_DIR = None  # never copy fixtures into the live keep dir

_REAL_MAX_HOLD = sbd.MAX_HOLD


def make_daemon(backdate, mode="toggle", key="rightalt"):
    d = object.__new__(sbd._Daemon)
    d.recording = True
    d.rec_proc = object()
    d.rec_t0 = time.monotonic() - backdate
    d.autosend = False
    d._rec_key = key
    d._rec_mode = mode
    d._rec_claim = threading.Lock()
    d._cycle_lock = None
    d._inject_q = queue.Queue()
    tmp = Path(tempfile.mkdtemp(prefix="sb-test-"))
    (tmp / "rec.wav").write_bytes(b"\x00" * 64)
    d._tmp_dir = tmp
    return d


def processing_msgs():
    return [m for m in notifs if m.startswith("Processing speech")]


fails = []

# 1. normal toggle finish -> provenance names mode + key + duration
#    (_key_label title-cases the evdev name: rightalt -> Rightalt)
d = make_daemon(backdate=2.5)
d._finish_record()
msgs = processing_msgs()
if not msgs or not msgs[0].startswith("Processing speech... (toggle Rightalt, "):
    fails.append(f"provenance: {msgs!r}")
elif "max_hold" in msgs[0]:
    fails.append(f"provenance: normal finish mislabeled as max_hold: {msgs[0]!r}")
elif not any(m.startswith("Copied: hello") for m in notifs):
    fails.append("normal finish: missing Copied notification")

# 2. a recording that ran into max_hold says so explicitly
notifs.clear()
sbd.MAX_HOLD = 2.0
d = make_daemon(backdate=3.0)
d._finish_record()
msgs = processing_msgs()
if not msgs or "hit max_hold 2s" not in msgs[0]:
    fails.append(f"max_hold provenance: {msgs!r}")
sbd.MAX_HOLD = _REAL_MAX_HOLD

# 3. double finish (key path vs silence watcher race) -> exactly one stop,
#    one transcription, one Processing notification
notifs.clear()
stops["n"] = 0
transcribes["n"] = 0
d = make_daemon(backdate=2.5)
d._finish_record()
d._finish_record()  # concurrent loser: claim already taken
if stops["n"] != 1 or transcribes["n"] != 1:
    fails.append(f"single-claim: stops={stops['n']} transcribes={transcribes['n']}")
if len(processing_msgs()) != 1:
    fails.append(f"single-claim: {len(processing_msgs())} Processing notifications")
if d.recording:
    fails.append("single-claim: recording flag not cleared")

if fails:
    print("FAIL:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("OK: Processing provenance + single-finish claim (3 scenarios)")
