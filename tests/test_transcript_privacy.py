#!/usr/bin/env python3
"""Transcript text stays out of notifications/logs/state by default (#31)."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from shipboard import stt  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


stt.transcribe = lambda wav: "my secret password is hunter2"
stt.copy_to_clipboard = lambda text: None

stt.SHOW_TRANSCRIPT = False
text, preview = stt._transcribe_copy(Path("x.wav"))
check("full text is still returned/copied", text == "my secret password is hunter2")
check("default preview has no transcript text",
      "hunter2" not in preview and preview == "29 chars", preview)

stt.SHOW_TRANSCRIPT = True
_, preview = stt._transcribe_copy(Path("x.wav"))
check("show_transcript=true shows the text", preview == "my secret password is hunter2")
stt.transcribe = lambda wav: "x" * 150
_, preview = stt._transcribe_copy(Path("x.wav"))
check("preview truncated at 100 chars", preview == "x" * 100 + "…")

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all transcript privacy checks passed")
