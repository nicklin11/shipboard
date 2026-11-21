"""Entry points: subcommand dispatch, daemon lifecycle, one-shot helpers."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .actions import _notify, copy_to_clipboard, normalize_text, send_keys
from .backend import backend_main
from .config import (DAEMON_LOCK_PATH, LOCK_PATH, SCROLL_SEND_ENTER,
                     SHOW_TRANSCRIPT)
from .daemon import _Daemon, run_record_cycle
from .failed import list_failed
from .locks import open_lock
from .logstate import _daemon_running, _status_main
from .setup_tui import _config_main, _setup_main, _tui_main
from .stt import (segments_to_timestamped_text, transcribe, transcribe_segments)

def _daemon_pids() -> list[int]:
    """PIDs of the running daemon.

    The daemon holds an exclusive flock on DAEMON_LOCK_PATH and records its pid
    there, so the lock — not a guess from process argv — says whether one is
    running and which process it is. (Matching argv shapes found the whisper
    proxy instead of the daemon and missed a pipx-launched daemon.)
    """
    if not _daemon_running():
        return []
    try:
        pid = int(Path(DAEMON_LOCK_PATH).read_text().strip())
    except (OSError, ValueError):
        return []
    if pid == os.getpid() or not Path(f"/proc/{pid}").exists():
        return []
    return [pid]


def _restart_daemon() -> str:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "shipboard"],
            capture_output=True, timeout=15,
        )
        if r.returncode == 0:
            return "restarted via systemd"
    except Exception:
        pass
    for pid in _daemon_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.4)  # let the old daemon release its flock
    subprocess.Popen(
        [sys.executable, str(Path(sys.argv[0]).resolve())],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return "restarted (detached)"


def _daemon_main() -> int:
    lock_fh = open_lock(DAEMON_LOCK_PATH)
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("shipboard: daemon already running", file=sys.stderr)
        return 0
    # Record our pid for stop/restart (we hold the flock, so this is ours).
    lock_fh.seek(0)
    lock_fh.truncate()
    lock_fh.write(f"{os.getpid()}\n")
    lock_fh.flush()
    daemon = _Daemon()

    def _terminate(signum, _frame):
        # Unwind through run() so the finally below reaps the recorder and the
        # interpreter exits normally (atexit then terminates the wake pw-cat).
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    try:
        daemon.run()
    except KeyboardInterrupt:
        pass
    finally:
        # Ignore further signals so a second SIGTERM can't abort the cleanup.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        daemon.shutdown()
        lock_fh.close()
    return 0


def _start_daemon() -> int:
    """Start the daemon detached; alias for the `daemon`/`start` subcommands."""
    if _daemon_pids():
        print("shipboard: daemon already running", file=sys.stderr)
        return 0
    subprocess.Popen(
        [sys.executable, str(Path(sys.argv[0]).resolve())],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("shipboard: daemon started (detached)")
    return 0


def _stop_daemon() -> int:
    """SIGTERM every daemon process (the `stop` subcommand)."""
    pids = _daemon_pids()
    if not pids:
        print("shipboard: no daemon running", file=sys.stderr)
        return 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    print(f"shipboard: stopped daemon ({len(pids)} process(es))")
    return 0


def _restart_main() -> int:
    """Restart the daemon (the `restart` subcommand)."""
    print(f"shipboard: {_restart_daemon()}")
    return 0


def _send_main() -> int:
    """One-shot paste + Enter (manual/backup trigger, e.g. a WM bind)."""
    # Refuse while a recording is in flight so stale content never pastes.
    try:
        lock_fh = open_lock(LOCK_PATH)
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fh.close()
    except OSError:
        _notify("shipboard", "Recording in progress — skipping paste")
        return 0
    try:
        send_keys(enter=SCROLL_SEND_ENTER)
    except Exception as exc:
        _notify("shipboard", f"Failed to send: {exc}")
        return 1
    return 0


def _process_main(argv: list[str]) -> int:
    """Transcribe an already-recorded audio file (the `process` subcommand).

    Prints the transcript to stdout so it composes with shell redirection;
    `--copy` keeps the daemon's clipboard behaviour. Unlike `--file`, a
    failure is always reported: non-zero exit and a message on stderr.
    """
    parser = argparse.ArgumentParser(
        prog="shipboard process",
        description="Transcribe an already-recorded audio file.",
    )
    parser.add_argument("path", type=Path, help="audio file to transcribe")
    parser.add_argument(
        "--copy", action="store_true",
        help="copy the transcript to the clipboard instead of printing it",
    )
    parser.add_argument(
        "--timestamps", nargs="?", const="text", choices=("text", "json"),
        default=None,
        help="emit per-segment timings instead of a plain transcript: "
             "bare flag -> '[hh:mm:ss] text' lines, 'json' -> the raw segments "
             "array. Output always goes to stdout; --copy is not used",
    )
    args = parser.parse_args(argv)

    wav = args.path.expanduser().resolve()
    if not wav.is_file():
        print(f"shipboard: file not found: {wav}", file=sys.stderr)
        return 1

    # Same lock as the record path: never transcribe while a recording is in
    # flight, or the two would race for the clipboard.
    lock_fh = open_lock(LOCK_PATH)
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fh.close()
        print("shipboard: recording in progress — retry when it finishes", file=sys.stderr)
        return 1
    try:
        _notify("shipboard", "Processing speech...")
        try:
            if args.timestamps:
                segments = transcribe_segments(wav)
                payload = (
                    json.dumps(segments, ensure_ascii=False, indent=2)
                    if args.timestamps == "json"
                    else segments_to_timestamped_text(segments)
                )
            else:
                text = normalize_text(transcribe(wav))
        except Exception as exc:
            _notify("shipboard", f"Recognition error: {exc}")
            print(f"shipboard: {exc}", file=sys.stderr)
            return 1
        if args.timestamps:
            if not payload.strip():
                _notify("shipboard", "Nothing recognized")
                print("shipboard: nothing recognized", file=sys.stderr)
                return 1
            print(payload)
            return 0
        if not text:
            _notify("shipboard", "Nothing recognized")
            print("shipboard: nothing recognized", file=sys.stderr)
            return 1
        if args.copy:
            try:
                copy_to_clipboard(text)
            except Exception as exc:
                print(f"shipboard: copy failed: {exc}", file=sys.stderr)
                return 1
            _notify("shipboard", "Copied: " + (
                text[:100] if SHOW_TRANSCRIPT else f"{len(text)} chars"))
        else:
            print(text)
        return 0
    finally:
        lock_fh.close()


def _retry_main(argv: list[str]) -> int:
    """Re-run transcription on a recording kept after an STT failure."""
    parser = argparse.ArgumentParser(
        prog="shipboard retry",
        description="Retry the newest recording kept after a failed "
                    "transcription (copies the result to the clipboard).",
    )
    parser.add_argument("--list", action="store_true",
                        help="list kept recordings, newest first, and exit")
    args = parser.parse_args(argv)
    kept = list_failed()
    if args.list:
        for path in reversed(kept):
            print(path)
        return 0
    if not kept:
        print("shipboard: no failed recordings to retry", file=sys.stderr)
        return 1
    newest = kept[-1]
    rc = _process_main([str(newest), "--copy"])
    if rc == 0:
        newest.unlink(missing_ok=True)
    return rc


def main() -> int:
    if sys.argv[1:2] == ["status"]:
        return _status_main()
    if sys.argv[1:2] == ["config"]:
        return _config_main()
    if sys.argv[1:2] == ["setup"]:
        # Default setup is the full-screen TUI when run interactively.
        # `shipboard setup --cli` (or a non-tty stdin) falls back to the
        # numbered dialog.
        if "--cli" in sys.argv[2:] or not sys.stdin.isatty() or os.environ.get("TERM") == "dumb":
            return _setup_main()
        return _tui_main()
    if sys.argv[1:2] in (["tui"], ["setup-tui"]):
        return _tui_main()
    if sys.argv[1:2] in (["daemon"], ["start"]):
        return _start_daemon()
    if sys.argv[1:2] == ["stop"]:
        return _stop_daemon()
    if sys.argv[1:2] == ["restart"]:
        return _restart_main()
    if sys.argv[1:2] == ["process"]:
        return _process_main(sys.argv[2:])
    if sys.argv[1:2] == ["retry"]:
        return _retry_main(sys.argv[2:])
    if sys.argv[1:2] == ["backend"]:
        return backend_main(sys.argv[2:])

    parser = argparse.ArgumentParser(
        description=(
            "shipboard: voice daemon (Pause/Scroll Lock) + helpers.\n"
            "CLI subcommands: daemon/start (run detached), stop (SIGTERM), "
            "restart (systemd or respawn), status (state), "
            "process PATH (transcribe a recorded file to stdout), "
            "retry (re-transcribe the last recording kept after an STT failure), "
            "backend up|status (whisper-local container + proxy unit), "
            "config (TOML editor), --send (paste+Enter).\n"
            "Interactive: setup (full-screen TUI, default when a terminal; "
            "--cli forces the numbered dialog), tui/setup-tui (curses setup)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--send", action="store_true",
        help="one-shot: paste clipboard + Enter (skips daemon)",
    )
    parser.add_argument(
        "--seconds", type=float, default=0.0,
        help="record for a fixed duration (one-shot, no daemon)",
    )
    parser.add_argument(
        "--file", type=Path, default=None,
        help="transcribe an existing audio file and copy it (one-shot)",
    )
    parser.add_argument(
        "--no-copy", action="store_true",
        help="print the transcript instead of copying",
    )
    parser.add_argument(
        "--timestamps", nargs="?", const="text", choices=("text", "json"),
        default=None,
        help="with --file, emit per-segment timings: bare flag -> "
             "'[hh:mm:ss] text' lines, 'json' -> the raw segments array. "
             "Implies printing to stdout instead of copying",
    )
    args = parser.parse_args()

    if args.send:
        return _send_main()

    if args.file is not None or args.seconds > 0 or args.no_copy:
        # Legacy one-shot record flow (no daemon).
        lock_fh = open_lock(LOCK_PATH)
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        try:
            if args.file is not None:
                wav = args.file.expanduser().resolve()
                if not wav.is_file():
                    print(f"File not found: {wav}", file=sys.stderr)
                    return 1
                _notify("shipboard", "Processing speech...")
                try:
                    if args.timestamps:
                        segments = transcribe_segments(wav)
                        payload = (
                            json.dumps(segments, ensure_ascii=False, indent=2)
                            if args.timestamps == "json"
                            else segments_to_timestamped_text(segments)
                        )
                    else:
                        text = normalize_text(transcribe(wav))
                except Exception as exc:
                    _notify("shipboard", f"Recognition error: {exc}")
                    return 1
                if args.timestamps:
                    if payload.strip():
                        print(payload)
                    return 0
                if args.no_copy:
                    print(text)
                else:
                    copy_to_clipboard(text)
                    _notify("shipboard", f"Copied: {text[:100]}")
                return 0
            return run_record_cycle(autosend=False, seconds=args.seconds)
        finally:
            lock_fh.close()

    return _daemon_main()


if __name__ == "__main__":
    raise SystemExit(main())
