"""Daemon: evdev device watch, record cycle, key press/release machine."""

from __future__ import annotations

import fcntl
import os
import queue
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .actions import _notify, send_keys
from .audio import start_recording, stop_recording
from .config import (BOTH_SEND_ENTER, CHANNELS, KEEP_AUDIO_DIR,
                     KEY_BINDINGS, LOCK_PATH, MAX_HOLD, MIN_RECORDING,
                     RATE, RECORD_TARGET, SEND_ENTER, TAP_START_GRACE,
                     TAP_STOP_SILENCE, WAKEWORD_ENABLED,
                     _HOLD_THRESHOLD_DEFAULT)
from .keys import _key_code, _key_label
from .logstate import _log, _write_state
from .stt import _transcribe_copy
from .wake import _WAKE_SILENCE_RMS, _WAKE_VENV, _wake_listen

def _watch_devices():
    import evdev

    wanted = {_key_code(b["key"]) for b in KEY_BINDINGS if b.get("key")}
    if not wanted:
        return []
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
            caps = dev.capabilities(verbose=False)
            keys = caps.get(evdev.ecodes.EV_KEY, [])
            if keys and wanted & set(keys):
                devices.append(dev)
        except Exception:
            continue
    return devices


def _input_access_issue() -> str:
    """Diagnose why key triggers aren't readable from /dev/input.

    Empty string = access is fine (keys simply aren't mapped on any device).
    Otherwise a short, actionable hint (e.g. user not in the `input` group),
    so the otherwise-silent "keys not found" becomes debuggable.
    """
    import evdev
    import glob
    import grp
    import os

    # Scan ALL input nodes (glob, not evdev.list_devices, which only returns
    # nodes this process can already open) so an unreadable keyboard is seen.
    paths = sorted(glob.glob("/dev/input/event*"))
    if not paths:
        return ""  # no input devices at all
    forbidden = False
    for p in paths:
        try:
            dev = evdev.InputDevice(p)
            try:
                dev.close()
            except Exception:
                pass
        except PermissionError:
            forbidden = True
            break
        except OSError:
            continue
    if not forbidden:
        return ""  # all devices openable; keys just not mapped on any
    gname = "input"
    try:
        gid = os.stat("/dev/input/event0").st_gid
        gname = grp.getgrgid(gid).gr_name
    except Exception:
        pass
    mine = {grp.getgrgid(g).gr_name for g in os.getgroups()}
    if gname in mine:
        return (f"keys off: /dev/input unreadable despite being in group "
                f"'{gname}' (session needs log out/in or a udev rule)")
    return (f"keys off: not in group '{gname}' — run "
            f"'sudo usermod -aG {gname} $USER' then log out/in")


def run_record_cycle(autosend: bool, seconds: float = 0.0) -> int:
    """Record -> transcribe -> copy; auto-send if autosend. Returns exit code."""
    # one-shot wait: first binding with hold/record
    first_hold_key = None
    for b in KEY_BINDINGS:
        if b.get("hold") in ("record","record_send"):
            first_hold_key = b["key"]
            break
    if not first_hold_key and KEY_BINDINGS:
        # fallback: any key with tap/record
        for b in KEY_BINDINGS:
            if b.get("tap") in ("record","record_send") or b.get("toggle") in ("record","record_send"):
                first_hold_key = b["key"]
                break
    tmp_dir = Path(tempfile.mkdtemp(prefix="shipboard-"))
    try:
        wav_path = tmp_dir / "rec.wav"
        mode_word = "hold"
        label = _key_label(first_hold_key) if first_hold_key else "key"
        _notify(
            "shipboard",
            f"Recording... ({mode_word} {label})"
            + (" — release: will paste and send" if autosend else ""),
        )
        proc = start_recording(wav_path)
        t0 = time.monotonic()
        if seconds > 0:
            time.sleep(seconds)
        elif first_hold_key:
            import evdev

            deadline = time.monotonic() + MAX_HOLD
            live = _watch_devices()
            held = False
            code = _key_code(first_hold_key)
            while time.monotonic() < deadline:
                if not live:
                    break
                r, _, _ = select.select(live, [], [], 0.1)
                for dev in r:
                    try:
                        for event in dev.read():
                            if (
                                event.type == evdev.ecodes.EV_KEY
                                and event.code == code
                                and event.value == 0  # release
                            ):
                                held = True
                                break
                    except (OSError, ValueError):
                        live.remove(dev)
                if held:
                    break
        stop_recording(proc)
        duration = time.monotonic() - t0
        if duration < MIN_RECORDING:
            _notify("shipboard", "Recording too short")
            return 0
        if not wav_path.is_file() or wav_path.stat().st_size == 0:
            _notify("shipboard", "Error: empty recording file")
            return 1
        try:
            text, preview = _transcribe_copy(wav_path)
        except RuntimeError as exc:
            print(f"shipboard: {exc}", file=sys.stderr)
            _notify("shipboard", str(exc))
            return 1
        if autosend:
            try:
                send_keys(enter=BOTH_SEND_ENTER)
            except Exception as exc:
                _notify("shipboard", f"Copied, but not sent: {exc}")
                return 1
            _notify("shipboard", f"Sent: {preview}")
        else:
            _notify("shipboard", f"Copied: {preview}")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# Daemon
# --------------------------------------------------------------------------
class _Daemon:
    def __init__(self) -> None:
        self.recording = False
        self.rec_proc: subprocess.Popen | None = None
        self.rec_t0 = 0.0
        self.autosend = False
        self.grace_deadline: float | None = None
        self.pause_down = False
        self.wake_rec = False
        self._inject_q: "queue.Queue[bool]" = queue.Queue()
        # key->behaviour state
        self._key_down: dict[int, float] = {}  # code -> down_time
        self._hold_fired: set[int] = set()  # codes where hold threshold already fired
        self._pending_hold: dict[int, float] = {}  # code -> deadline
        self._rec_key: str | None = None  # which key started current recording
        self._rec_mode: str | None = None  # "hold" | "toggle" | "tap"
        # quick map code -> binding
        self._code_to_bind: dict[int, dict] = {}
        for b in KEY_BINDINGS:
            try:
                c = _key_code(b["key"])
                self._code_to_bind[c] = b
            except SystemExit:
                pass

    def _binding_for(self, code: int) -> dict | None:
        return self._code_to_bind.get(code)

    def _start_record(self, key: str | None = None, mode: str | None = None, notify_label: str | None = None) -> None:
        if self.recording:
            return
        self.autosend = False
        self.grace_deadline = None
        self._cycle_lock = open(LOCK_PATH, "w")
        try:
            fcntl.flock(self._cycle_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._cycle_lock.close()
            self._cycle_lock = None
            return
        tmp_dir = Path(tempfile.mkdtemp(prefix="shipboard-"))
        wav = tmp_dir / "rec.wav"
        self.rec_proc = start_recording(wav)
        self.recording = True
        self.rec_t0 = time.monotonic()
        self._tmp_dir = tmp_dir
        self._rec_key = key
        self._rec_mode = mode
        _write_state(state="recording")
        _log(f"record start key={key!r} mode={mode} -> {wav}")
        label = notify_label or (f"{_key_label(key)}" if key else "key")
        mode_word = mode or "hold"
        _notify("shipboard", f"Recording... ({mode_word} {label})")

    def _finish_record(self, from_wake: bool = False) -> None:
        if not self.recording or self.rec_proc is None:
            return
        stop_recording(self.rec_proc)
        self.recording = False
        duration = time.monotonic() - self.rec_t0
        autosend = self.autosend
        self.autosend = False
        _log(f"finish: autosend={autosend} from_wake={from_wake} dur={duration:.1f} key={self._rec_key} mode={self._rec_mode}")
        was_key = self._rec_key
        was_mode = self._rec_mode
        self._rec_key = None
        self._rec_mode = None
        cycle_lock = getattr(self, "_cycle_lock", None)
        self._cycle_lock = None
        wav = Path(self._tmp_dir) / "rec.wav"
        try:
            if duration < MIN_RECORDING:
                _notify("shipboard", "Recording too short")
                return
            if not wav.is_file() or wav.stat().st_size == 0:
                _notify("shipboard", "Error: empty recording file")
                return
            if KEEP_AUDIO_DIR is not None:
                KEEP_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy(wav, KEEP_AUDIO_DIR / f"rec-{int(time.time())}.wav")
            _notify("shipboard", "Processing speech...")
            _write_state(state="processing")
            try:
                text, preview = _transcribe_copy(wav)
                _log(f"stt ok ({len(text)} chars) result: {preview!r}")
            except RuntimeError as exc:
                _log(f"stt error: {exc}")
                _write_state(
                    state="idle" if str(exc) == "Nothing recognized" else "error",
                    text=str(exc)[:200],
                )
                _notify("shipboard", str(exc))
                return
            if autosend:
                if from_wake:
                    self._inject_q.put(BOTH_SEND_ENTER)
                else:
                    try:
                        send_keys(enter=BOTH_SEND_ENTER)
                    except Exception as exc:
                        _write_state(state="error", text=str(exc)[:200])
                        _notify("shipboard", f"Copied, but not sent: {exc}")
                        return
                _write_state(state="sent", text=preview)
                _notify("shipboard", f"Sent: {preview}")
            else:
                _write_state(state="copied", text=preview)
                _notify("shipboard", f"Copied: {preview}")
        finally:
            try:
                shutil.rmtree(self._tmp_dir, ignore_errors=True)
            except AttributeError:
                pass
            if cycle_lock is not None:
                try:
                    cycle_lock.close()
                except OSError:
                    pass

    def _do_action(self, action: str, key: str, mode: str) -> None:
        if not action:
            return
        if action == "paste":
            try:
                send_keys(enter=SEND_ENTER)
                _log(f"paste action key={key} mode={mode}")
            except Exception as exc:
                _notify("shipboard", f"Failed to send: {exc}")
            return
        if action in ("record", "record_send"):
            # toggle/tap/hold share same start; autosend differs
            autosend = (action == "record_send")
            if self.recording:
                # if same key toggles, finish; else ignore if another recording active
                if self._rec_key == key:
                    # finishing same key — autosend already set? override if needed
                    self.autosend = autosend or self.autosend
                    self._finish_record()
                return
            self._start_record(key=key, mode=mode)
            if self.recording:
                self.autosend = autosend
                # tap has no "release to stop" (release already fired the
                # start), so a single press needs its own stop signal:
                # auto-finish after silence instead of hanging till max_hold.
                if mode == "tap" and TAP_STOP_SILENCE > 0:
                    threading.Thread(target=self._tap_silence_watch,
                                     name="tap-silence", daemon=True).start()
            return

    def _tap_silence_watch(self) -> None:
        """Auto-stop a tap-started recording once speech goes quiet.

        Mirrors the wake listener's silence logic (RMS threshold, grace)
        but with its own pw-cat meter, independent of the wake word engine.
        """
        try:
            for sp in (_WAKE_VENV / "lib").glob("python*/site-packages"):
                sys.path.insert(0, str(sp))
            import numpy as np
        except Exception as exc:
            _log(f"tap silence: numpy unavailable ({exc}) — auto-stop off")
            return
        cmd = ["pw-cat", "--record", "--rate", str(RATE), "--channels",
               str(CHANNELS), "--format", "s16", "--raw", "-"]
        if RECORD_TARGET:
            cmd += ["--target", RECORD_TARGET]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
        except OSError as exc:
            _log(f"tap silence: pw-cat failed ({exc}) — auto-stop off")
            return
        t0 = time.monotonic()
        silence_since: float | None = None
        spoke = False
        try:
            while self.recording and self._rec_mode == "tap":
                raw = proc.stdout.read(int(RATE * CHANNELS * 2 * 0.08))
                if not raw:
                    break
                audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                now = time.monotonic()
                rms = float(np.sqrt(np.mean(audio ** 2)))
                if rms >= _WAKE_SILENCE_RMS:
                    spoke = True
                    silence_since = None
                    continue
                if not spoke and now - t0 < TAP_START_GRACE:
                    continue  # give the user a beat to start talking
                if silence_since is None:
                    silence_since = now
                elif now - silence_since >= TAP_STOP_SILENCE:
                    if self.recording and self._rec_mode == "tap":
                        _log(f"tap silence: {TAP_STOP_SILENCE:.1f}s quiet — finishing")
                        self._finish_record()
                    break
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

    def _on_key_press(self, code: int) -> None:
        bind = self._binding_for(code)
        if bind is None:
            return
        key = bind["key"]
        tap = bind.get("tap", "") or ""
        hold = bind.get("hold", "") or ""
        toggle = bind.get("toggle", "") or ""
        thr = float(bind.get("hold_threshold", _HOLD_THRESHOLD_DEFAULT))
        # toggle overrides tap
        if toggle:
            tap = ""
        # toggle path: press toggles recording
        if toggle:
            # if already recording from this key in toggle mode -> stop
            if self.recording and self._rec_key == key and self._rec_mode == "toggle":
                self._finish_record()
                return
            # if not recording, decide if this is hold vs toggle based on pending hold
            # need to defer: if hold also present, don't start toggle immediately; wait for threshold
            if hold and not self.recording:
                # defer toggle/hold decision: set pending deadline
                self._key_down[code] = time.monotonic()
                self._pending_hold[code] = time.monotonic() + thr
                return
            # no hold, immediate toggle start
            if not self.recording:
                self._do_action(toggle, key, "toggle")
            return
        # no toggle: handle hold+tap
        if hold and tap:
            # defer decision until release or threshold
            self._key_down[code] = time.monotonic()
            self._pending_hold[code] = time.monotonic() + thr
            return
        if hold and not tap:
            # only hold: wait for threshold then start; short press ignored (idempotent)
            self._key_down[code] = time.monotonic()
            self._pending_hold[code] = time.monotonic() + thr
            return
        if tap and not hold:
            # only tap: defer to release (release starts or stops recording for record, or pastes for paste)
            # idempotent with hold: hold+tap handled above, this is tap-only
            self._key_down[code] = time.monotonic()
            return
        # fallback: nothing

    def _on_key_release(self, code: int) -> None:
        bind = self._binding_for(code)
        if bind is None:
            return
        key = bind["key"]
        tap = bind.get("tap", "") or ""
        hold = bind.get("hold", "") or ""
        toggle = bind.get("toggle", "") or ""
        if toggle:
            tap = ""
        was_down = self._key_down.pop(code, None)
        pending = self._pending_hold.pop(code, None)
        fired = code in self._hold_fired
        if fired:
            self._hold_fired.discard(code)
            # hold was active, release finishes recording
            if self.recording and self._rec_key == key and self._rec_mode == "hold":
                self._finish_record()
            return
        # hold not yet fired
        if toggle and hold and pending is not None:
            # short release before threshold -> toggle action
            if was_down is not None:
                self._do_action(toggle, key, "toggle")
            return
        if hold and tap and pending is not None:
            # short release -> tap, else would have fired hold
            self._do_action(tap, key, "tap")
            return
        if hold and not tap and pending is not None:
            # only hold, short press ignored (or treat as hold if you want)
            # idempotent: do nothing on short tap when only hold is set
            return
        if tap and not hold:
            # only tap: release fires tap — tap as toggle (first release starts, second release stops)
            if self.recording and self._rec_key == key and self._rec_mode == "tap":
                self._finish_record()
                return
            if not self.recording:
                self._do_action(tap, key, "tap")
            return

    # (legacy shims removed — see _on_key_press/_on_key_release)

    def run(self) -> None:
        import evdev

        _write_state(state="running", pid=os.getpid())
        stop_event = threading.Event()
        if WAKEWORD_ENABLED:
            threading.Thread(
                target=_wake_listen, args=(self, stop_event), daemon=True
            ).start()
        devices = _watch_devices()
        if not devices and KEY_BINDINGS:
            hint = _input_access_issue()
            msg = "Daemon: trigger keys not found on evdev (wake words still on)"
            if hint:
                msg += f"\n{hint}"
            _notify("shipboard", msg)
            if hint:
                print(f"shipboard: {hint}", file=sys.stderr)
        # build code map
        code_map = {}
        for b in KEY_BINDINGS:
            try:
                code_map[_key_code(b["key"])] = b
            except SystemExit:
                pass
        while True:
            now = time.monotonic()
            while True:
                try:
                    _enter = self._inject_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    send_keys(enter=_enter)
                except Exception as exc:
                    _notify("shipboard", f"Failed to send: {exc}")
            # hold threshold expiry
            for code, deadline in list(self._pending_hold.items()):
                if code not in self._key_down:
                    continue
                if now >= deadline and code not in self._hold_fired:
                    bind = self._binding_for(code)
                    if bind is None:
                        continue
                    hold = bind.get("hold", "") or ""
                    if not hold:
                        continue
                    # fire hold
                    self._hold_fired.add(code)
                    key = bind["key"]
                    # if not already recording, start hold recording
                    if not self.recording:
                        self._do_action(hold, key, "hold")
                        # _do_action may have started; ensure mode is hold
                        if self.recording:
                            self._rec_mode = "hold"
            if self.recording and now - self.rec_t0 > MAX_HOLD:
                self._finish_record()

            if not devices:
                time.sleep(0.5)
                devices = _watch_devices()
                continue
            r, _, _ = select.select(devices, [], [], 0.05)
            for dev in r:
                try:
                    for event in dev.read():
                        if event.type != evdev.ecodes.EV_KEY:
                            continue
                        if event.value == 2:  # auto-repeat
                            continue
                        code = event.code
                        if code not in code_map and code not in self._key_down and code not in self._hold_fired:
                            continue
                        if event.value == 1:
                            self._key_down.setdefault(code, now)
                            self._on_key_press(code)
                        elif event.value == 0:
                            self._on_key_release(code)
                except (OSError, ValueError):
                    devices = [d for d in devices if d != dev]
                    try:
                        dev.close()
                    except Exception:
                        pass
