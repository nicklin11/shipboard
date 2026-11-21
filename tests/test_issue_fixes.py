#!/usr/bin/env python3
"""Regressions for issues #16 (config save/load), #20 (wake_rec reset),
#21 (start_recording failure) and #27 (shutdown reaps the recorder).

Plain pytest functions (also runnable directly); hermetic: never reads or
writes the real user config.
"""
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from shipboard import config  # noqa: E402
from shipboard import daemon as sbd  # noqa: E402


class _Proc:
    def __init__(self):
        self.signals = []
        self._done = False

    def poll(self):
        return 0 if self._done else None

    def send_signal(self, sig):
        self.signals.append(sig)
        self._done = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._done = True


def _daemon(tmp, start):
    sbd.LOCK_PATH = str(Path(tmp) / "cycle.lock")
    sbd._notify = lambda *a, **k: notes.append(a)
    sbd._log = lambda *a, **k: None
    sbd._write_state = lambda **k: None
    sbd.start_recording = start
    return sbd._Daemon()


notes: list = []


# --- #16 ------------------------------------------------------------------
def test_save_escapes_and_roundtrips():
    tmp = Path(tempfile.mkdtemp(prefix="sb-test-"))
    orig = config.DEFAULT_CONFIG_PATH
    config.DEFAULT_CONFIG_PATH = tmp / "shipboard.toml"
    try:
        values = config._field_defaults()
        nasty = 'say "hi" \\ back\\slash\nnewline\x7f'
        text_keys = [f[0] for f in config._SETUP_FIELDS if f[2] is str]
        values[text_keys[0]] = nasty
        config._save_config_file(values)
        data = tomllib.loads(config.DEFAULT_CONFIG_PATH.read_text())
        assert data[text_keys[0]] == nasty
        assert not list(tmp.glob("*.tmp"))
    finally:
        config.DEFAULT_CONFIG_PATH = orig


def test_save_backs_up_invalid_existing_file():
    tmp = Path(tempfile.mkdtemp(prefix="sb-test-"))
    orig = config.DEFAULT_CONFIG_PATH
    path = tmp / "shipboard.toml"
    path.write_text('prompt = "broken\n')
    config.DEFAULT_CONFIG_PATH = path
    try:
        config._save_config_file(config._field_defaults())
        assert (tmp / "shipboard.toml.bak").read_text() == 'prompt = "broken\n'
        tomllib.loads(path.read_text())
    finally:
        config.DEFAULT_CONFIG_PATH = orig


def test_load_reports_parse_error(capsys=None):
    tmp = Path(tempfile.mkdtemp(prefix="sb-test-"))
    bad = tmp / "bad.toml"
    bad.write_text('x = "unterminated\n')
    assert config._load_toml_config(bad) == {}


# --- #21 / #20 ------------------------------------------------------------
def test_start_failure_releases_everything():
    tmp = tempfile.mkdtemp(prefix="sb-test-")

    def boom(_wav):
        raise RuntimeError("no pw-record")

    d = _daemon(tmp, boom)
    d.wake_rec = True
    before = set(Path(tempfile.gettempdir()).glob("shipboard-*"))
    d._start_record(key="x", mode="tap")  # must not raise
    assert not d.recording and d.rec_proc is None and d._cycle_lock is None
    assert d.wake_rec is False
    assert set(Path(tempfile.gettempdir()).glob("shipboard-*")) == before
    # lock is free again: a second attempt can take it
    import fcntl
    with open(sbd.LOCK_PATH, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert any("Cannot start recording" in str(n) for n in notes)


def test_wake_rec_cleared_on_finish():
    tmp = tempfile.mkdtemp(prefix="sb-test-")
    proc = _Proc()
    d = _daemon(tmp, lambda _wav: proc)
    sbd._transcribe_copy = lambda wav: ("t", "t")
    sbd.MIN_RECORDING = 99  # finish early path: "too short"
    d.wake_rec = True
    d._start_record()
    assert d.recording
    d._finish_record()  # finished by a key/max-hold, not the wake loop
    assert d.wake_rec is False


# --- #27 ------------------------------------------------------------------
def test_shutdown_reaps_recorder():
    tmp = tempfile.mkdtemp(prefix="sb-test-")
    proc = _Proc()
    d = _daemon(tmp, lambda _wav: proc)
    d._start_record(key="x", mode="hold")
    tmp_dir = d._tmp_dir
    d.shutdown()
    assert proc.signals, "recorder was not stopped"
    assert not d.recording and not Path(tmp_dir).exists()
    d.shutdown()  # idempotent


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
