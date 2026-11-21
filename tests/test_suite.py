"""pytest entry point for the standalone check scripts in this directory.

Each ``test_*.py`` script runs its checks at import time and exits non-zero on
failure, so they are executed one per subprocess here. The child gets a
throwaway HOME / XDG dirs so no test reads or writes the developer's real
shipboard config, state or lock files.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = sorted(p for p in HERE.glob("test_*.py") if p.name != Path(__file__).name)
NEEDS_NUMPY = {"test_tap_silence.py"}


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script(script, tmp_path):
    if script.name in NEEDS_NUMPY and importlib.util.find_spec("numpy") is None:
        pytest.skip("numpy not installed")
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("SHIPBOARD_", "WHISPER_"))
    }
    env.update(
        HOME=str(tmp_path),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_RUNTIME_DIR=str(tmp_path / "run"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, env=env, timeout=300, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
