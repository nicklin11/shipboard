#!/usr/bin/env python3
"""Regression: whisper segment timestamps are exposed, and default output is unchanged.

Ground truth for this test came from the real server, not from the docs. Two
assumptions in the original issue were wrong and are pinned here:

  * The default `response_format=json` answers with {"text": ...} ONLY. There
    are no segments sitting in the payload waiting to be read — the server
    never sends them. `segments` appears only under `verbose_json`.
  * `start`/`end` are SECONDS as floats, not milliseconds. Measured on a 30 s
    probe: segments 0.00-2.44, 2.44-6.20, ... 28.44-29.23, duration 30.0.

So this stubs the HTTP response with both `text` and `segments` and drives the
real _transcribe_payload / transcribe / transcribe_segments and the real
_process_main with the network stubbed out.
"""
import io
import json
import re
import sys
import tempfile
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


def _write_wav(path: Path, seconds: float = 2.0, rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


# Measured shape, verbatim in structure from the live 30 s probe.
SEGMENTS = [
    {"id": 0, "start": 0.0, "end": 2.44,
     "text": " Я учебник Института интеллектуальных устройств, соответ"},
    {"id": 1, "start": 2.44, "end": 6.2,
     "text": "ственно, я вообще не ППС, то есть не профессорский состав,"},
    {"id": 2, "start": 6.2, "end": 9.87,
     "text": " преподаю просто, сам не знаю, исторически так получилось, и"},
]
PLAIN_TEXT = " ".join(s["text"].strip() for s in SEGMENTS)
VERBOSE_PAYLOAD = {
    "task": "transcribe", "language": "russian", "duration": 30.0,
    "text": PLAIN_TEXT, "segments": SEGMENTS,
}
# What the server actually sends for the DEFAULT response_format.
PLAIN_PAYLOAD = {"text": PLAIN_TEXT}

next_payload: dict = dict(PLAIN_PAYLOAD)
captured_reqs: list = []


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _fake_urlopen(req, timeout=None):
    captured_reqs.append(req)
    return _FakeResp(next_payload)


sbs._ensure_server = lambda *a, **k: None
sbs._idle_heartbeat = lambda *a, **k: __import__("contextlib").nullcontext()
sbs.urllib_request.urlopen = _fake_urlopen

tmp = Path(tempfile.mkdtemp())
clip = tmp / "clip.wav"
_write_wav(clip)


def _sent_fields() -> bytes:
    return captured_reqs[-1].data


# --- 1. default path is untouched: no verbose_json, plain text out ------------
next_payload = dict(PLAIN_PAYLOAD)
captured_reqs.clear()

text = sbs.transcribe(clip)
check("transcribe() returns plain text", text == PLAIN_TEXT.strip(), repr(text[:60]))
check("transcribe() does NOT request verbose_json",
      b"verbose_json" not in _sent_fields(),
      "plain path must keep the old request byte-for-byte")
check("transcribe() sends no response_format at all",
      b'name="response_format"' not in _sent_fields())

# --- 2. segments path asks for verbose_json and gets segments ---------------
next_payload = dict(VERBOSE_PAYLOAD)
captured_reqs.clear()

segs = sbs.transcribe_segments(clip)
check("segments request carries response_format=verbose_json",
      b'name="response_format"' in _sent_fields() and b"verbose_json" in _sent_fields())
check("transcribe_segments() returns the array", segs == SEGMENTS)
check("segment times are seconds, not ms",
      abs(segs[0]["end"] - 2.44) < 1e-9 and abs(segs[-1]["end"] - 9.87) < 1e-9)

# --- 3. a server that ignores verbose_json must fail loudly ------------------
next_payload = dict(PLAIN_PAYLOAD)
captured_reqs.clear()
try:
    sbs.transcribe_segments(clip)
    check("missing segments -> RuntimeError", False, "no exception raised")
except RuntimeError as exc:
    check("missing segments -> RuntimeError", True)
    check("error names the missing format", "verbose_json" in str(exc), str(exc)[:90])

# --- 4. timestamp formatting (seconds -> [hh:mm:ss]) -------------------------
# Full hh:mm:ss on purpose: a 73-minute lecture overruns mm:ss, and the issue
# specifies [hh:mm:ss].
check("0.0s -> [00:00:00]", sbs.format_timestamp(0.0) == "[00:00:00]", sbs.format_timestamp(0.0))
check("2.44s truncates to whole seconds [00:00:02]",
      sbs.format_timestamp(2.44) == "[00:00:02]", sbs.format_timestamp(2.44))
check("6.2s -> [00:00:06]", sbs.format_timestamp(6.2) == "[00:00:06]")
check("65.9s -> [00:01:05]", sbs.format_timestamp(65.9) == "[00:01:05]")
check("3725.0s -> [01:02:05]", sbs.format_timestamp(3725.0) == "[01:02:05]",
      sbs.format_timestamp(3725.0))
check("always 8 chars wide",
      all(len(sbs.format_timestamp(x)) == 10 for x in (0.0, 6.2, 65.9, 3725.0, 86399.0)))

# --- 5. text rendering is reconstructable by stripping the prefixes ----------
ts_text = sbs.segments_to_timestamped_text(SEGMENTS, normalize=False)
lines = ts_text.split("\n")
check("one line per segment", len(lines) == 3, str(len(lines)))
check("every line carries a timestamp",
      all(re.match(r"^\[\d{2}:\d{2}:\d{2}\] ", ln) for ln in lines), lines[0])
check("prefix values ascend",
      [ln[0:10] for ln in lines] == ["[00:00:00]", "[00:00:02]", "[00:00:06]"],
      str([ln[0:10] for ln in lines]))

stripped = " ".join(re.sub(r"^\[\d{2}:\d{2}:\d{2}\] ", "", ln) for ln in lines)
check("stripping the prefix recovers the transcript",
      stripped == " ".join(s["text"].strip() for s in SEGMENTS), stripped[:70])

# normalize must not eat the prefix
ts_norm = sbs.segments_to_timestamped_text(SEGMENTS, normalize=True)
check("normalize_text does not corrupt the timestamp prefix",
      all(re.match(r"^\[\d{2}:\d{2}:\d{2}\] ", ln) for ln in ts_norm.split("\n")),
      ts_norm.split("\n")[0])
check("glued symbols survive normalization under the prefix",
      "ППС" in ts_norm and "--" not in ts_norm.split("\n")[1],
      ts_norm.split("\n")[1])

# empty / malformed segments must not emit junk lines
check("empty segment text is dropped",
      sbs.segments_to_timestamped_text(
          [{"start": 0.0, "text": "   "}, {"start": 1.0, "text": "да"}]) == "[00:00:01] да")
check("missing start -> placeholder, not a crash",
      "[--:--:--] да" == sbs.segments_to_timestamped_text([{"text": "да"}]))

# --- 6. CLI: default output unchanged, --timestamps additive -----------------
lock = tmp / "shipboard.lock"
sbc.LOCK_PATH = lock
sbc._notify = lambda t, m: None
copied: list[str] = []
sbc.copy_to_clipboard = lambda t: copied.append(t)

sbc.transcribe = lambda wav: "  привет   мир  "
sbc.transcribe_segments = lambda wav: SEGMENTS

out = io.StringIO()
with redirect_stdout(out), redirect_stderr(io.StringIO()):
    rc = sbc._process_main([str(clip)])
check("plain run unchanged", rc == 0 and out.getvalue().strip() == "привет мир",
      repr(out.getvalue()))

out = io.StringIO()
with redirect_stdout(out), redirect_stderr(io.StringIO()):
    rc = sbc._process_main([str(clip), "--timestamps"])
got = out.getvalue()
check("--timestamps exits 0", rc == 0, f"rc={rc}")
check("--timestamps prints stamped lines",
      all(re.match(r"^\[\d{2}:\d{2}:\d{2}\] ", ln) for ln in got.strip().split("\n")),
      got.split("\n")[0])
check("--timestamps ignores --copy", copied == [], str(copied))

out = io.StringIO()
with redirect_stdout(out), redirect_stderr(io.StringIO()):
    rc = sbc._process_main([str(clip), "--timestamps", "json"])
parsed = json.loads(out.getvalue())
check("--timestamps json parses back to the segments", parsed == SEGMENTS, f"rc={rc}")
check("--timestamps json is valid UTF-8 (not \\u-escaped)",
      "Института" in out.getvalue())

# empty segments -> rc 1, never a silent success
sbc.transcribe_segments = lambda wav: []
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = sbc._process_main([str(clip), "--timestamps"])
check("empty segments -> rc 1", rc == 1, f"rc={rc}")
check("empty segments -> reason on stderr", "nothing recognized" in err.getvalue().lower(),
      err.getvalue().strip())

# a transport failure on the segments path still surfaces
def _boom(wav):
    raise RuntimeError("whisper.cpp returned no segments (needs response_format=verbose_json): {}")


sbc.transcribe_segments = _boom
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = sbc._process_main([str(clip), "--timestamps"])
check("segments failure -> rc 1", rc == 1, f"rc={rc}")
check("segments failure -> stderr reason", "verbose_json" in err.getvalue(),
      err.getvalue().strip())

# an invalid choice for the optional value is rejected by argparse
try:
    with redirect_stderr(io.StringIO()):
        sbc._process_main([str(clip), "--timestamps", "yaml"])
    check("--timestamps yaml rejected", False, "argparse accepted it")
except SystemExit as exc:
    check("--timestamps yaml rejected", exc.code != 0, f"code={exc.code}")

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    raise SystemExit(1)
print("all segment-timestamp checks passed")