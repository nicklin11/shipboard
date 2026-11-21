#!/usr/bin/env python3
"""Notification backends never interpolate text into a script (#30)."""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from shipboard import platform_adapters as pa  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


calls = []
subprocess.run = lambda cmd, **kw: calls.append(cmd)

evil = 'x" & do shell script "touch /tmp/pwn" & "'
pa._notify_macos("title", evil)
cmd = calls[-1]
check("macOS: message passed as argv, not in script text",
      cmd[-2] == evil and not any(evil in part for part in cmd[:-2]), str(cmd))

pa._notify_windows("t", "it’s ‘q’ 'a'")
script = calls[-1][-1]
check("windows: ASCII and typographic quotes doubled",
      "it’’s ‘‘q’’ ''a''" in script, script[-200:])

check("unused input layer is gone",
      not any(hasattr(pa, n) for n in ("InputListener", "get_input_backend", "key_code")))

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all notify adapter checks passed")
