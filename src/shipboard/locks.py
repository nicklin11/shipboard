"""Safe lock-file helpers (per-user directory, no symlink following)."""

from __future__ import annotations

import os
from pathlib import Path


def runtime_dir() -> Path:
    """Per-user directory for lock files: $XDG_RUNTIME_DIR/shipboard, else
    /tmp/shipboard-<uid>. Created 0700 by `open_lock` when first needed."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isabs(base):
        return Path(base) / "shipboard"
    return Path("/tmp") / f"shipboard-{os.getuid()}"


def open_lock(path: os.PathLike | str):
    """Open a flock file for read/write without truncating or following a
    symlink, creating its parent 0700 and the file 0600. Returns a file object
    (raises OSError, including ELOOP for a symlinked path).

    Lock files are never unlinked: removing one while another process holds a
    flock on it lets a third process lock a fresh inode at the same path.
    """
    path = Path(path)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError:
        pass
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    return os.fdopen(fd, "r+")
