"""State file, log line helper, daemon-liveness lock, `status` output."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

from .config import (DAEMON_LOCK_PATH, DEFAULT_CONFIG_PATH, KEY_BINDINGS,
                     MAX_HOLD, MIN_RECORDING, NORMALIZE, PASTE_COMBO,
                     RECORD_TARGET, SEND_ENTER, WAKEWORD_ENABLED,
                     WAKEWORD_KEYWORDS, WHISPER_URL,
                     _HOLD_THRESHOLD_DEFAULT)
from .keys import _key_label

STATE_PATH = Path(
    os.environ.get(
        "SHIPBOARD_STATE", "~/.local/state/shipboard/state.json"
    )
).expanduser()


def _log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except Exception:
        pass


def _write_state(**fields: object) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {"ts": int(time.time())}
        data.update(fields)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(STATE_PATH)
    except Exception:
        pass


def _daemon_running() -> bool:
    try:
        fh = open(DAEMON_LOCK_PATH, "w")
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        fh.close()


def _status_main() -> int:
    print(f"daemon:    {'running' if _daemon_running() else 'stopped'}")
    print(f"config:    {DEFAULT_CONFIG_PATH}")
    print(f"stt:       {WHISPER_URL}")
    if KEY_BINDINGS:
        hdr = "keys:"
        for b in KEY_BINDINGS:
            k = _key_label(b["key"])
            thr = b.get("hold_threshold", _HOLD_THRESHOLD_DEFAULT)
            parts = []
            if b.get("toggle"): parts.append(f"toggle={b['toggle']}")
            if b.get("hold"): parts.append(f"hold={b['hold']}@{thr:g}s")
            if b.get("tap"): parts.append(f"tap={b['tap']}")
            print(f"{hdr:<11} {k:<14} {' | '.join(parts) if parts else '(off)'}")
            hdr = "           "
    else:
        print("keys:      (none — wake words only)")
    print(f"paste:     {PASTE_COMBO} | Enter: {'yes' if SEND_ENTER else 'no'}"
          f" | max_hold {MAX_HOLD}s | min_rec {MIN_RECORDING}s")
    print(f"normalize: {'on' if NORMALIZE else 'off'}")
    print(f"wakeword:  {'on (sherpa)' if WAKEWORD_ENABLED else 'off'}")
    if WAKEWORD_ENABLED:
        print(f"           {WAKEWORD_KEYWORDS}")
    if RECORD_TARGET:
        src = RECORD_TARGET
    else:
        try:
            out = subprocess.run(["pactl", "get-default-source"],
                                 capture_output=True, text=True, timeout=3)
            src = f"default ({out.stdout.strip()})"
        except Exception:
            src = "default (system)"
    print(f"source:    {src}")
    if STATE_PATH.is_file():
        try:
            st = json.loads(STATE_PATH.read_text())
            when = time.strftime("%H:%M:%S", time.localtime(st.get("ts", 0)))
            last_state = st.get("state", "?")
            note = ""
            if last_state == "running" and not _daemon_running():
                note = " (daemon stopped)"
            print(f"last:      {last_state} @ {when}{note}")
            if st.get("text"):
                print(f"           {st['text']}")
        except Exception:
            pass
    return 0
