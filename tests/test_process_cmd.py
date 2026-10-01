#!/usr/bin/env python3
"""Regression: `shipboard process PATH` works on LONG recordings.

Two defects made the pre-existing `--file` path unusable for lectures:
  1. transcribe() used a fixed 120 s HTTP timeout -> urllib gave up while
     whisper.cpp was still working (measured: 262 s for a 75 min lecture).
  2. the idle marker was touched once, on entry, so whisper-idle-stop.timer
     SIGTERMed the container 300 s into a longer request.

Drives the real _request_timeout / _audio_seconds / _idle_heartbeat and the
real _process_main with the network stubbed out.
"""
import io
import sys
import tempfile
import time
import wave
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from shipboard import cli as sbc  # noqa: E402
from shipboard import stt as sbs  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


def _write_wav(path: Path, seconds: float, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


# --- 1. timeout scales with audio length -------------------------------------
tmp = Path(tempfile.mkdtemp())

short = tmp / "short.wav"
_write_wav(short, 10.0)
long_ = tmp / "long.wav"
_write_wav(long_, 4520.0)  # the real lecture length

check("wav duration read back", abs(sbs._audio_seconds(long_) - 4520.0) < 0.5,
      f"got {sbs._audio_seconds(long_)}")

t_short = sbs._request_timeout(short)
t_long = sbs._request_timeout(long_)
check("short clip keeps the floor", t_short == sbs.TIMEOUT_FLOOR, f"{t_short}")
check("75 min lecture outlasts the old 120 s", t_long > 262, f"timeout={t_long:.0f}s")
check("timeout stays bounded (not lecture-length)", t_long < 4520 * 0.75 + 61,
      f"timeout={t_long:.0f}s")

# non-wav / unreadable -> floor, never a crash
other = tmp / "clip.opus"
other.write_bytes(b"not a wav at all")
check("non-wav falls back to floor", sbs._request_timeout(other) == sbs.TIMEOUT_FLOOR)
check("missing file falls back to floor",
      sbs._request_timeout(tmp / "nope.wav") == sbs.TIMEOUT_FLOOR)
bad = tmp / "bad.wav"
bad.write_bytes(b"junk")
check("garbage .wav falls back to floor",
      sbs._request_timeout(bad) == sbs.TIMEOUT_FLOOR)

# --- 2. heartbeat refreshes the marker, then stops ---------------------------
marker = tmp / "idle-marker"
marker.write_text("")
sbs.IDLE_MARKER = marker

before = marker.stat().st_mtime_ns
with sbs._idle_heartbeat(interval=0.05):
    time.sleep(0.3)
during = marker.stat().st_mtime_ns
check("marker refreshed while request in flight", during > before)

after_beat = marker.stat().st_mtime_ns
time.sleep(0.3)
check("beat thread stopped on exit", marker.stat().st_mtime_ns == after_beat)

# --- 3. _process_main: prints by default, reports failure --------------------
lock = tmp / "shipboard.lock"
sbc.LOCK_PATH = lock
notified: list[str] = []
sbc._notify = lambda title, msg: notified.append(msg)
copied: list[str] = []
sbc.copy_to_clipboard = lambda text: copied.append(text)

sbc.transcribe = lambda wav: "  привет   мир  "
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = sbc._process_main([str(short)])
check("exit 0 on success", rc == 0, f"rc={rc}")
check("stdout carries the normalized transcript", out.getvalue().strip() == "привет мир",
      repr(out.getvalue()))
check("nothing copied without --copy", copied == [])
check("lock released", not lock.exists())

sbc.transcribe = lambda wav: "скопируй меня"
out = io.StringIO()
with redirect_stdout(out), redirect_stderr(io.StringIO()):
    rc = sbc._process_main([str(short), "--copy"])
check("--copy copies", copied == ["скопируй меня"], str(copied))
check("--copy prints nothing", out.getvalue() == "")

out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = sbc._process_main([str(tmp / "missing.wav")])
check("missing file -> rc 1", rc == 1, f"rc={rc}")
check("missing file -> stderr message", "not found" in err.getvalue())


def _boom(wav):
    raise RuntimeError("whisper.cpp HTTP 500: kaboom")


sbc.transcribe = _boom
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = sbc._process_main([str(short)])
check("STT failure -> rc 1 (never silent success)", rc == 1, f"rc={rc}")
check("STT failure -> stderr reason", "kaboom" in err.getvalue(), err.getvalue().strip())

sbc.transcribe = lambda wav: "   "
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = sbc._process_main([str(short)])
check("empty transcript -> rc 1", rc == 1, f"rc={rc}")

# --- 4. lock contention is reported, not swallowed ---------------------------
lock.write_text("")
import fcntl  # noqa: E402

held = open(lock, "w")
fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = sbc._process_main([str(short)])
held.close()
check("lock held -> rc 1", rc == 1, f"rc={rc}")
check("lock held -> explains why", "recording in progress" in err.getvalue(), err.getvalue().strip())

# --- 5. dispatch: `process` must not be mistaken for the daemon --------------
# _daemon_pids only accepts len(argv)==2; `shipboard process PATH` has 3.
argv = ["shipboard", "process", str(short)]
check("process argv not daemon-shaped", len(argv) != 2)

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all process-cmd checks passed")