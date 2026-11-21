#!/usr/bin/env python3
"""Failed transcriptions keep the audio; `shipboard retry` re-runs it (#19)."""
import io
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from shipboard import cli as sbc  # noqa: E402
from shipboard import failed  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


tmp = Path(tempfile.mkdtemp(prefix="failedtest-"))
fdir = tmp / "failed"

wavs = []
for i in range(4):
    w = tmp / f"in{i}.wav"
    w.write_bytes(b"RIFF" + bytes([i]))
    wavs.append(w)
saved = [failed.save_failed(w, fdir, keep=3) for w in wavs]
check("recording moved, not copied", all(s and s.exists() for s in saved[1:]) and not wavs[0].exists())
left = failed.list_failed(fdir)
check("pruned to the newest 3", len(left) == 3 and left[-1] == saved[3] and not saved[0].exists())
check("dir is 0700, file 0600",
      (fdir.stat().st_mode & 0o777) == 0o700 and (saved[3].stat().st_mode & 0o777) == 0o600)
check("unwritable destination -> None (recording not silently dropped by us)",
      failed.save_failed(tmp / "nope.wav", tmp / "x" / "y", keep=3) is None)

# retry: newest first, deleted on success, kept on failure
failed.FAILED_DIR = fdir
sbc.list_failed = lambda: failed.list_failed(fdir)
seen = []
sbc.transcribe = lambda wav: (seen.append(Path(wav).name) or "hello")
sbc.copy_to_clipboard = lambda text: None
sbc._notify = lambda *a: None
sbc.LOCK_PATH = tmp / "lock"
out, err = io.StringIO(), io.StringIO()
with redirect_stdout(out), redirect_stderr(err):
    rc = sbc._retry_main([])
check("retry succeeds on the newest", rc == 0 and seen == [saved[3].name], f"rc={rc} {seen}")
check("successful retry deletes the file", not saved[3].exists())

def boom(wav):
    raise RuntimeError("still down")
sbc.transcribe = boom
with redirect_stdout(out), redirect_stderr(err):
    rc = sbc._retry_main([])
check("failed retry keeps the file", rc == 1 and saved[2].exists())

out = io.StringIO()
with redirect_stdout(out):
    sbc._retry_main(["--list"])
check("--list prints newest first", out.getvalue().split() == [str(saved[2]), str(saved[1])])

for p in failed.list_failed(fdir):
    p.unlink()
with redirect_stdout(io.StringIO()), redirect_stderr(err):
    rc = sbc._retry_main([])
check("nothing to retry -> rc 1", rc == 1)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all failed-recording checks passed")
