#!/usr/bin/env python3
"""Lock files: per-user dir, 0600, no symlink following, no truncation (#28)."""
import errno
import os
import stat
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from shipboard.locks import open_lock, runtime_dir  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="locktest-"))

# default dir is per-user
saved = os.environ.get("XDG_RUNTIME_DIR")
os.environ["XDG_RUNTIME_DIR"] = str(tmp / "run")
check("runtime_dir honours XDG_RUNTIME_DIR", runtime_dir() == tmp / "run" / "shipboard")
del os.environ["XDG_RUNTIME_DIR"]
check("runtime_dir falls back to a uid-specific /tmp dir",
      runtime_dir() == Path("/tmp") / f"shipboard-{os.getuid()}")
if saved is not None:
    os.environ["XDG_RUNTIME_DIR"] = saved

# creates parent 0700 and file 0600
target = tmp / "sub" / "a.lock"
fh = open_lock(target)
fh.close()
check("parent created 0700", stat.S_IMODE(target.parent.stat().st_mode) == 0o700)
check("file created 0600", stat.S_IMODE(target.stat().st_mode) == 0o600)

# does not truncate existing content
target.write_text("keep")
open_lock(target).close()
check("existing file not truncated", target.read_text() == "keep")

# refuses to follow a symlink, and leaves its target untouched
victim = tmp / "victim"
victim.write_text("precious")
link = tmp / "evil.lock"
link.symlink_to(victim)
try:
    open_lock(link)
    refused = False
except OSError as exc:
    refused = exc.errno == errno.ELOOP
check("symlinked lock path refused (ELOOP)", refused)
check("symlink target untouched", victim.read_text() == "precious")

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all lock checks passed")
