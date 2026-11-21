"""Recordings whose transcription failed are kept for `shipboard retry`."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .logstate import STATE_PATH

_override = os.environ.get("SHIPBOARD_FAILED_DIR")
FAILED_DIR = Path(_override).expanduser() if _override else STATE_PATH.parent / "failed"
MAX_FAILED = 10  # newest kept; older ones are pruned


def list_failed(directory: Path | None = None) -> list[Path]:
    """Saved recordings, oldest first."""
    directory = directory or FAILED_DIR
    try:
        return sorted(directory.glob("rec-*.wav"))
    except OSError:
        return []


def save_failed(wav: Path, directory: Path | None = None,
                keep: int = MAX_FAILED) -> Path | None:
    """Move `wav` into the failed-recordings dir (0700; audio is private) and
    prune to the newest `keep`. Returns the new path, or None if it could not
    be saved (the caller then just loses the recording as before)."""
    directory = directory or FAILED_DIR
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        dest = directory / f"rec-{time.time_ns()}.wav"
        shutil.move(str(wav), dest)
        os.chmod(dest, 0o600)
        for old in list_failed(directory)[:-keep] if keep > 0 else []:
            old.unlink(missing_ok=True)
        return dest
    except OSError:
        return None
