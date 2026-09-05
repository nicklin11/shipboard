"""Entry points: subcommand dispatch, daemon lifecycle, one-shot helpers."""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .actions import _notify, copy_to_clipboard, normalize_text, send_keys
from .config import DAEMON_LOCK_PATH, LOCK_PATH, SCROLL_SEND_ENTER
from .daemon import _Daemon, run_record_cycle
from .logstate import _status_main
from .setup_tui import _config_main, _setup_main, _tui_main
from .stt import transcribe

def _daemon_pids() -> list[int]:
    # Match only the daemon python process (not bash wrappers / other CLIs).
    # The daemon is always launched as `python <path>` with NO subcommand;
    # `shipboard setup` / `status` / `--send` etc. carry extra argv entries
    # and must never be killed by stop/restart.
    out = subprocess.run(["pgrep", "-f", "python3 .*shipboard"],
                         capture_output=True, text=True)
    pids = []
    for pid in out.stdout.split():
        try:
            pid = int(pid)
        except ValueError:
            continue
        if pid == os.getpid():
            continue  # never kill our own CLI process
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            argv = [a.decode(errors="replace") for a in argv if a]
        except OSError:
            continue  # process exited between pgrep and read
        if len(argv) != 2:
            continue  # has a subcommand/flag -> not the daemon
        pids.append(pid)
    return pids


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
    lock_fh = open(DAEMON_LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("shipboard: daemon already running", file=sys.stderr)
        return 0
    try:
        _Daemon().run()
    except KeyboardInterrupt:
        pass
    finally:
        lock_fh.close()
        try:
            Path(DAEMON_LOCK_PATH).unlink(missing_ok=True)
        except OSError:
            pass
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
        lock_fh = open(LOCK_PATH, "w")
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

    parser = argparse.ArgumentParser(
        description=(
            "shipboard: voice daemon (Pause/Scroll Lock) + helpers.\n"
            "CLI subcommands: daemon/start (run detached), stop (SIGTERM), "
            "restart (systemd or respawn), status (state), "
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
    args = parser.parse_args()

    if args.send:
        return _send_main()

    if args.file is not None or args.seconds > 0 or args.no_copy:
        # Legacy one-shot record flow (no daemon).
        lock_fh = open(LOCK_PATH, "w")
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
                    text = transcribe(wav)
                except Exception as exc:
                    _notify("shipboard", f"Recognition error: {exc}")
                    return 1
                text = normalize_text(text)
                if args.no_copy:
                    print(text)
                else:
                    copy_to_clipboard(text)
                    _notify("shipboard", f"Copied: {text[:100]}")
                return 0
            return run_record_cycle(autosend=False, seconds=args.seconds)
        finally:
            lock_fh.close()
            try:
                Path(LOCK_PATH).unlink(missing_ok=True)
            except OSError:
                pass

    return _daemon_main()


if __name__ == "__main__":
    raise SystemExit(main())
