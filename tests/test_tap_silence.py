#!/usr/bin/env python3
"""Regression: tap/toggle-started recordings auto-stop on silence.

Runs the real shipboard daemon module with a stubbed pw-cat stream; verifies
one-press flow (press -> speak -> quiet -> processed) for BOTH tap and
toggle modes (toggle overrides tap, so a quick press starts a toggle with no
release-to-stop — issue #1), that hold recordings are not touched by the
watcher, and that a watcher left over from a previous cycle (stale gen) can
never stop the next recording.
"""
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

for sp in Path.home().joinpath(".local/share/shipboard-venv/lib").glob(
        "python*/site-packages"):
    sys.path.insert(0, str(sp))

sys.path.insert(0, str(REPO / "src"))
from shipboard import config  # noqa: E402  (thresholds are defined here)
from shipboard import daemon as sbd  # noqa: E402  (the watcher reads these from daemon's module globals)

assert sbd.TAP_STOP_SILENCE is config.TAP_STOP_SILENCE
assert sbd.TAP_START_GRACE is config.TAP_START_GRACE

import numpy as np  # noqa: E402  (must be importable like in the watcher)

# speed up: real thresholds are tuned for humans, not CI
sbd.TAP_STOP_SILENCE = 0.15
sbd.TAP_START_GRACE = 0.05

VOICE = b"\x00\x40"      # int16 0x4000 -> rms ~0.5, way above silence threshold
QUIET = b"\x00\x00"


class FakePopen:
    def __init__(self, chunks, **kw):
        self.chunks = list(chunks)
        self.terminated = False
        self.stdout = self

    def read(self, n):
        time.sleep(0.08)  # pace chunks like the real 80ms pw-cat stream
        if not self.chunks:
            return b""
        kind = self.chunks.pop(0)
        return (VOICE if kind == "v" else QUIET) * (n // 2)

    def terminate(self):
        self.terminated = True


def make_daemon(mode, gen=0):
    d = object.__new__(sbd._Daemon)
    d.recording = True
    d.rec_proc = None
    d.rec_t0 = time.monotonic()
    d.autosend = False
    d._rec_key = "rightalt"
    d._rec_mode = mode
    d._rec_gen = gen
    d.finished = []
    d._finish_record = lambda from_wake=False: (
        d.finished.append(time.monotonic()), setattr(d, "recording", False))
    return d


def run_watcher(chunks, mode="tap", gen=None):
    d = make_daemon(mode)
    t0 = time.monotonic()
    sbd.subprocess.Popen = lambda cmd, **kw: FakePopen(chunks)
    if gen is None:
        d._tap_silence_watch()
    else:
        d._tap_silence_watch(gen)
    return d, time.monotonic() - t0


fails = []

# 1. speech then silence -> finished, and not before speech ended
d, dt = run_watcher(["v"] * 12 + ["s"] * 10)
if not d.finished:
    fails.append("speech-then-silence: no finish")
elif dt > 2.0:
    fails.append(f"speech-then-silence: took {dt:.2f}s (too slow)")

# 2. accidental tap (no speech at all) -> finished after grace+silence
d, dt = run_watcher(["s"] * 10)
if not d.finished:
    fails.append("no-speech: no finish")

# 3. short pause mid-speech (80ms < stop_silence) -> NOT cut early,
#    finishes only on the final silence window
d, dt = run_watcher(["v"] * 8 + ["s"] * 1 + ["v"] * 8 + ["s"] * 6)
if not d.finished:
    fails.append("pause-mid-speech: no finish")
elif dt < 0.15:
    fails.append(f"pause-mid-speech: cut too early ({dt:.2f}s)")

# 4. hold-mode recording -> watcher must exit without touching it
d, dt = run_watcher(["s"] * 30, mode="hold")
if d.finished:
    fails.append("hold-mode: watcher finished a hold recording")

# 5. toggle-mode recording (quick press, toggle overrides tap) -> same
#    speech-then-silence auto-stop as tap; without it the recording hangs
#    until max_hold and resurfaces as an unexplained notification (issue #1)
d, dt = run_watcher(["v"] * 12 + ["s"] * 10, mode="toggle")
if not d.finished:
    fails.append("toggle speech-then-silence: no finish")
elif dt > 2.0:
    fails.append(f"toggle speech-then-silence: took {dt:.2f}s (too slow)")

# 6. stale watcher (older recording generation) -> must exit untouched even
#    though the CURRENT recording is mid-speech
d = make_daemon("toggle", gen=0)
sbd.subprocess.Popen = lambda cmd, **kw: FakePopen(["v"] * 5 + ["s"] * 30)
d._tap_silence_watch(d._rec_gen + 1)
if d.finished:
    fails.append("stale-gen: watcher finished a newer recording")

if fails:
    print("FAIL:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("OK: silence auto-stop for tap/toggle + stale-gen guard (6 scenarios)")
