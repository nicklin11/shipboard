"""Recording (PipeWire pw-record via the platform adapter)."""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path

from . import platform_adapters as _plat
from .config import CHANNELS, RATE, RECORD_TARGET

def start_recording(path: Path) -> subprocess.Popen:
    return _plat.start_recording(
        path, rate=RATE, channels=CHANNELS,
        target=RECORD_TARGET or None,
    )


def stop_recording(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
