#!/usr/bin/env python3
"""shipboard platform adapters: per-OS backends behind one neutral API.

Every OS-specific call in shipboard lives here so the daemon core stays
platform-neutral. Backends are selected at call time (never import time), so
this module imports cleanly on any OS and a missing native backend fails with
a clear RuntimeError instead of a confusing ImportError.

Integration points (swap these in shipboard.py to go cross-platform):
  * get_input_backend()  -> InputListener       (Linux evdev; NotImplementedError elsewhere)
  * get_inject_backend() -> inject(combo, enter) (Linux uinput -> wtype; pynput on macOS/Windows)
  * copy_to_clipboard()  -> wl-copy / xclip / pbcopy / clip.exe, auto-detected
  * start_recording()    -> pw-record (Linux PipeWire), ffmpeg fallback
  * notify()             -> notify-send / osascript / PowerShell toast (best-effort)
  * detect_platform()    -> "linux" | "macos" | "windows" | "unknown"
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --------------------------------------------------------------------------
# Platform detection
# --------------------------------------------------------------------------

def detect_platform() -> str:
    """Return "linux", "macos", "windows", or "unknown".

    All other adapters dispatch on this value, so a new OS only needs one
    branch per adapter here.
    """
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith(("win32", "cygwin")):
        return "windows"
    return "unknown"


# --------------------------------------------------------------------------
# shipboard.toml backend overrides (lazy; default = auto-detect)
# --------------------------------------------------------------------------

_CONFIG_PATH = Path("~/.config/shipboard/shipboard.toml").expanduser()
_cfg_cache: dict | None = None


def _get_config() -> dict:
    """Load shipboard.toml once, lazily; {} if missing or unreadable.

    Reading happens at first adapter call, never at import time, so this
    module still imports cleanly on any OS.
    """
    global _cfg_cache
    if _cfg_cache is None:
        try:
            import tomllib

            with open(_CONFIG_PATH, "rb") as fh:
                _cfg_cache = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            _cfg_cache = {}
    return _cfg_cache


def _backend_choice(name: str, allowed: tuple[str, ...]) -> str | None:
    """Return the forced backend for ``name``, or None for auto-detect."""
    value = str(_get_config().get(name, "auto")).strip().lower()
    if value in ("", "auto"):
        return None
    if value not in allowed:
        raise RuntimeError(
            f"shipboard.toml: {name}={value!r} is not one of "
            f"{', '.join(allowed)}"
        )
    return value


# --------------------------------------------------------------------------
# Input listening (Linux evdev)
# --------------------------------------------------------------------------

class InputListener:
    """Abstraction over evdev key input.

    Scans /dev/input/event* for devices that report any of ``key_codes``,
    opens the matching devices, and exposes events either via iteration or a
    callback. Consumed like:

        listener = get_input_backend([key_code("KEY_PAUSE")])
        for code, value in listener:      # value: 1 = press, 0 = release
            ...

    Non-Linux platforms get a stub that raises NotImplementedError.
    """

    def __init__(self, key_codes, devices="/dev/input/event*"):
        import evdev

        self._evdev = evdev
        self._wanted = set(key_codes)
        self._devices = []
        self._callbacks = []
        for path in evdev.list_devices():
            if not Path(path).match(devices):
                continue
            try:
                dev = evdev.InputDevice(path)
                keys = dev.capabilities(verbose=False).get(evdev.ecodes.EV_KEY, [])
                if self._wanted & set(keys):
                    self._devices.append(dev)
            except OSError:
                continue

    def __iter__(self):
        import select

        while True:
            events = self.poll(timeout=0.5)
            for code, value in events:
                yield code, value

    def poll(self, timeout=0.0):
        """Read available events. Returns a list of (key_code, value) tuples
        for the watched keys only; repeats (value 2) are dropped."""
        import select

        if not self._devices:
            return []
        r, _, _ = select.select(self._devices, [], [], timeout)
        out = []
        for dev in r:
            try:
                for event in dev.read():
                    if event.type != self._evdev.ecodes.EV_KEY:
                        continue
                    if event.value == 2:  # auto-repeat
                        continue
                    if event.code not in self._wanted:
                        continue
                    out.append((event.code, event.value))
                    for cb in self._callbacks:
                        cb(event.code, event.value)
            except (OSError, ValueError):
                try:
                    dev.close()
                except OSError:
                    pass
                self._devices.remove(dev)
        return out

    def on(self, callback):
        """Register a callback invoked with (key_code, value) per key event."""
        self._callbacks.append(callback)

    def close(self):
        for dev in self._devices:
            try:
                dev.close()
            except OSError:
                pass
        self._devices = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class _InputUnavailable:
    """Non-Linux input stub: fails loudly instead of silently doing nothing."""

    def __init__(self, *_args, **_kwargs):
        raise NotImplementedError(
            "key input listening is only implemented on Linux (evdev); "
            f"current platform is {detect_platform()!r}"
        )

    def __iter__(self):  # pragma: no cover - unreachable, kept for the API shape
        return self


def key_code(name: str) -> int:
    """Resolve an evdev key name like "KEY_PAUSE" or "pause" to its code."""
    from evdev import ecodes

    if name in ecodes.ecodes:
        return ecodes.ecodes[name]
    return ecodes.ecodes["KEY_" + name.upper()]


def get_input_backend(key_codes):
    """Return an InputListener watching ``key_codes``.

    Raises RuntimeError if evdev is unavailable, NotImplementedError on any
    non-Linux platform. ``input_device_glob`` in shipboard.toml narrows the
    scanned devices (empty/missing = /dev/input/event*).
    """
    if detect_platform() != "linux":
        return _InputUnavailable
    try:
        import evdev  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "evdev is required for Linux key input (pip install python-evdev)"
        ) from exc
    glob = str(_get_config().get("input_device_glob", "")).strip()
    return InputListener(key_codes, devices=glob or "/dev/input/event*")


# --------------------------------------------------------------------------
# Key injection (Linux uinput/wtype; pynput on macOS/Windows)
# --------------------------------------------------------------------------

_COMBO_MODS = {
    "ctrl": "KEY_LEFTCTRL",
    "shift": "KEY_LEFTSHIFT",
    "alt": "KEY_LEFTALT",
    "super": "KEY_LEFTMETA",
    "meta": "KEY_LEFTMETA",
}
_COMBO_KEYS: dict[str, str] = {
    "v": "KEY_V", "insert": "KEY_INSERT", "enter": "KEY_ENTER",
}


def _full_combo_keys() -> dict[str, str]:
    """Letters a-z, digits 0-9, f1-f24 and common keys (lazy, needs evdev)."""
    if len(_COMBO_KEYS) > 4:
        return _COMBO_KEYS
    from evdev import ecodes as e

    for i in range(26):
        ch = chr(ord("a") + i)
        _COMBO_KEYS[ch] = f"KEY_{chr(ord('A') + i)}"
    for d in range(10):
        _COMBO_KEYS[str(d)] = f"KEY_{d}"
    for f in range(1, 25):
        _COMBO_KEYS[f"f{f}"] = f"KEY_F{f}"
    _COMBO_KEYS.update({
        "space": "KEY_SPACE", "tab": "KEY_TAB", "home": "KEY_HOME",
        "end": "KEY_END", "pageup": "KEY_PAGEUP", "pagedown": "KEY_PAGEDOWN",
        "delete": "KEY_DELETE", "backspace": "KEY_BACKSPACE",
    })
    return _COMBO_KEYS

_PYNPUT_MODS = {
    "ctrl": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "super": "cmd",
    "meta": "cmd",
}
_PYNPUT_KEYS = {"v": "v", "insert": "insert", "enter": "enter"}


class InjectBackend:
    """Injects a modifier combo like "ctrl+shift+v" (optionally + Enter).

    The callable returned by get_inject_backend(); dispatch on .name if you
    need to know which backend is in use.
    """

    def __init__(self, name: str, fn):
        self.name = name
        self._fn = fn

    def __call__(self, combo: str, enter: bool = False) -> None:
        self._fn(combo, enter)


def _inject_uinput(combo: str, enter: bool) -> None:
    from evdev import UInput, ecodes

    parts = combo.split("+")
    keys = _full_combo_keys()
    if len(parts) < 2 or parts[-1] not in keys:
        raise ValueError(f"unsupported combo: {combo!r}")
    codes = []
    for mod in parts[:-1]:
        name = _COMBO_MODS.get(mod)
        if name is None:
            raise ValueError(f"unknown modifier in combo: {mod!r}")
        codes.append(ecodes.ecodes[name])
    codes.append(ecodes.ecodes[keys[parts[-1]]])
    if enter:
        codes.append(ecodes.ecodes["KEY_ENTER"])

    with UInput() as ui:
        delay = 0.03
        for code in codes:
            ui.write(ecodes.EV_KEY, code, 1)
            ui.syn()
        time.sleep(delay)
        for code in reversed(codes):
            ui.write(ecodes.EV_KEY, code, 0)
            ui.syn()
        time.sleep(delay)


def _inject_wtype(combo: str, enter: bool) -> None:
    wtype = shutil.which("wtype")
    if not wtype:
        raise RuntimeError("wtype is not installed (and uinput injection failed)")
    parts = combo.split("+")
    keys = _full_combo_keys()
    if len(parts) < 2 or parts[-1] not in keys:
        raise ValueError(f"unsupported combo: {combo!r}")
    cmd = [wtype]
    for mod in parts[:-1]:
        if mod not in _COMBO_MODS:
            raise ValueError(f"unknown modifier in combo: {mod!r}")
        cmd += ["-M", mod]
    cmd.append(parts[-1])
    if enter:
        cmd += ["-m", parts[-1], "Return"]
    subprocess.run(cmd, timeout=5, capture_output=True)


def _inject_pynput(combo: str, enter: bool) -> None:
    from pynput import keyboard as _kb

    parts = combo.split("+")
    keys = _PYNPUT_KEYS | {chr(ord("a") + i): chr(ord("a") + i) for i in range(26)}
    if len(parts) < 2 or parts[-1] not in keys:
        raise ValueError(f"unsupported combo: {combo!r}")
    ctl = _kb.Controller()
    mods = []
    for mod in parts[:-1]:
        name = _PYNPUT_MODS.get(mod)
        if name is None:
            raise ValueError(f"unknown modifier in combo: {mod!r}")
        mods.append(getattr(_kb.Key, name))
    key = keys[parts[-1]]
    for m in mods:
        ctl.press(m)
    ctl.press(key)
    ctl.release(key)
    for m in reversed(mods):
        ctl.release(m)
    if enter:
        ctl.press(_kb.Key.enter)
        ctl.release(_kb.Key.enter)


def get_inject_backend() -> InjectBackend:
    """Return a callable injecting modifier combos on the current platform.

    Linux: python-evdev uinput, falling back to wtype if uinput fails at call
    time. macOS/Windows: pynput, imported lazily. Raises RuntimeError if no
    backend is importable; raising on a missing native tool happens per call.

    ``inject_backend`` in shipboard.toml (auto|uinput|wtype|pynput) forces a
    specific backend; an unavailable forced backend raises a RuntimeError
    instead of auto-falling back.
    """
    forced = _backend_choice("inject_backend", ("uinput", "wtype", "pynput"))
    if forced == "uinput":
        if detect_platform() != "linux":
            raise RuntimeError(
                "shipboard.toml sets inject_backend='uinput' but uinput "
                "injection is only available on Linux"
            )
        try:
            import evdev  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "shipboard.toml sets inject_backend='uinput' but python-evdev "
                "is not installed (pip install python-evdev)"
            ) from exc
        return InjectBackend("uinput", _inject_uinput)
    if forced == "wtype":
        if not shutil.which("wtype"):
            raise RuntimeError(
                "shipboard.toml sets inject_backend='wtype' but wtype is not "
                "installed"
            )
        return InjectBackend("wtype", _inject_wtype)
    if forced == "pynput":
        try:
            import pynput  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "shipboard.toml sets inject_backend='pynput' but pynput is "
                "not installed (pip install pynput)"
            ) from exc
        return InjectBackend("pynput", _inject_pynput)
    platform = detect_platform()
    if platform == "linux":
        try:
            import evdev  # noqa: F401

            def inject(combo, enter=False):
                try:
                    _inject_uinput(combo, enter)
                except RuntimeError:
                    raise
                except Exception:
                    _inject_wtype(combo, enter)

            return InjectBackend("uinput", inject)
        except ImportError as exc:
            wtype = shutil.which("wtype")
            if wtype:
                return InjectBackend("wtype", _inject_wtype)
            raise RuntimeError(
                "key injection needs python-evdev or wtype on Linux"
            ) from exc
    if platform in ("macos", "windows"):
        try:
            import pynput  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "pynput is required for key injection on "
                f"{platform} (pip install pynput)"
            ) from exc
        return InjectBackend("pynput", _inject_pynput)
    raise NotImplementedError(
        f"key injection is not implemented on {platform!r}"
    )


# --------------------------------------------------------------------------
# Clipboard
# --------------------------------------------------------------------------

def _linux_clip_env() -> dict:
    """Child env with the Wayland socket/XDG_RUNTIME_DIR patched for wl-copy."""
    env = os.environ.copy()
    runtime_dir = f"/run/user/{os.getuid()}"
    if not env.get("WAYLAND_DISPLAY"):
        for cand in ("wayland-1", "wayland-0"):
            if Path(runtime_dir, cand).exists():
                env["WAYLAND_DISPLAY"] = cand
                break
    if env.get("WAYLAND_DISPLAY"):
        env.setdefault("XDG_RUNTIME_DIR", runtime_dir)
    return env


def _linux_clip_cmd() -> tuple[list[str], dict]:
    """Return (command, env) for the clipboard tool on Linux.

    The daemon may run as a background process without a session env, so the
    child env is patched to reach the Wayland socket: WAYLAND_DISPLAY is
    picked from the live socket path when unset, and XDG_RUNTIME_DIR (which
    libwayland uses to resolve the socket) is set from the user's runtime dir.
    """
    env = _linux_clip_env()
    if env.get("WAYLAND_DISPLAY"):
        wl = shutil.which("wl-copy")
        if wl:
            return [wl], env
    if env.get("DISPLAY"):
        xclip = shutil.which("xclip")
        if xclip:
            return [xclip, "-selection", "clipboard"], env
    return [], env


def _forced_clip_cmd(backend: str, platform: str, env: dict) -> tuple[list[str], dict]:
    """Return (command, env) for a forced clipboard backend; RuntimeError
    when the tool is unavailable or belongs to another platform."""
    if backend == "wl-copy":
        if platform != "linux":
            raise RuntimeError(
                "shipboard.toml sets clipboard_backend='wl-copy' but the "
                f"current platform is {platform!r}"
            )
        if not shutil.which("wl-copy"):
            raise RuntimeError(
                "shipboard.toml sets clipboard_backend='wl-copy' but wl-copy "
                "is not installed"
            )
        return ["wl-copy"], _linux_clip_env()
    if backend == "xclip":
        if platform != "linux":
            raise RuntimeError(
                "shipboard.toml sets clipboard_backend='xclip' but the "
                f"current platform is {platform!r}"
            )
        if not shutil.which("xclip"):
            raise RuntimeError(
                "shipboard.toml sets clipboard_backend='xclip' but xclip is "
                "not installed"
            )
        return [shutil.which("xclip"), "-selection", "clipboard"], env
    if backend == "pbcopy":
        if platform != "macos":
            raise RuntimeError(
                "shipboard.toml sets clipboard_backend='pbcopy' but the "
                f"current platform is {platform!r}"
            )
        if not shutil.which("pbcopy"):
            raise RuntimeError(
                "shipboard.toml sets clipboard_backend='pbcopy' but pbcopy "
                "is not installed"
            )
        return [shutil.which("pbcopy")], env
    if backend == "clip":
        if platform != "windows":
            raise RuntimeError(
                "shipboard.toml sets clipboard_backend='clip' but the "
                f"current platform is {platform!r}"
            )
        if not shutil.which("clip"):
            raise RuntimeError(
                "shipboard.toml sets clipboard_backend='clip' but clip.exe "
                "is not available"
            )
        return [shutil.which("clip")], env
    raise RuntimeError(f"unsupported clipboard backend: {backend!r}")


def copy_to_clipboard(text: str) -> None:
    """Copy ``text`` to the clipboard via the platform's native tool.

    Linux auto-detects Wayland (wl-copy) vs X11 (xclip); macOS uses pbcopy,
    Windows uses clip.exe. Raises RuntimeError if no backend is available.
    ``clipboard_backend`` in shipboard.toml
    (auto|wl-copy|xclip|pbcopy|clip) forces a specific tool and raises a
    RuntimeError when it is unavailable here.

    wl-copy forks a background clipboard server by default, and that child
    inherits the parent's stdout/stderr pipe FDs. Waiting on those pipes for
    EOF therefore blocks until the server exits (clipboard ownership is
    released) — the 10s hang. So stdout goes to /dev/null and stderr to a
    regular file (kept only for the error message), never to a PIPE.
    """
    platform = detect_platform()
    forced = _backend_choice(
        "clipboard_backend", ("wl-copy", "xclip", "pbcopy", "clip")
    )
    env = os.environ.copy()
    if forced is not None:
        cmd, env = _forced_clip_cmd(forced, platform, env)
    elif platform == "linux":
        cmd, env = _linux_clip_cmd()
        if not cmd:
            raise RuntimeError(
                "clipboard copy needs wl-copy (Wayland) or xclip (X11) on Linux"
            )
    elif platform == "macos":
        cmd = [shutil.which("pbcopy") or "pbcopy"]
    elif platform == "windows":
        cmd = [shutil.which("clip") or "clip"]
    else:
        raise NotImplementedError(
            f"clipboard copy is not implemented on {platform!r}"
        )
    with tempfile.TemporaryFile() as err:
        proc = subprocess.run(
            cmd, input=text.encode("utf-8"), env=env,
            stdout=subprocess.DEVNULL, stderr=err, timeout=10,
        )
        if proc.returncode != 0:
            err.seek(0)
            detail = err.read().decode("utf-8", "replace").strip()
            raise RuntimeError(f"{cmd[0]} failed: {detail}")


# --------------------------------------------------------------------------
# Audio recording
# --------------------------------------------------------------------------

def _ffmpeg_cmd(path, rate, channels) -> list[str]:
    ff = shutil.which("ffmpeg")
    if not ff:
        return []
    platform = detect_platform()
    if platform == "linux":
        src = ["-f", "pulse", "-i", "default"]
    elif platform == "macos":
        src = ["-f", "avfoundation", "-i", ":0"]
    else:  # windows or unknown: needs an explicit dshow device name
        raise RuntimeError(
            "ffmpeg recording on this platform needs a dshow device name; "
            "install pw-record on Linux or set an audio source explicitly"
        )
    return [
        ff, "-y", *src,
        "-ar", str(rate), "-ac", str(channels),
        str(path),
    ]


def start_recording(path, rate: int = 16000, channels: int = 1,
                    target: str | None = None) -> subprocess.Popen:
    """Start recording to ``path``; returns the subprocess.Popen handle.

    Linux prefers pw-record (PipeWire); ffmpeg is the fallback everywhere.
    ``target`` is a PipeWire source name (pw-record ``--target``), ignored
    by ffmpeg. The caller stops recording with proc.send_signal(SIGINT) /
    kill, mirroring shipboard's stop_recording(). ``record_backend`` in
    shipboard.toml (auto|pw-record|ffmpeg) forces a backend and raises a
    RuntimeError when it is unavailable.
    """
    forced = _backend_choice("record_backend", ("pw-record", "ffmpeg"))
    platform = detect_platform()
    if forced == "pw-record":
        pw = shutil.which("pw-record")
        if platform != "linux":
            raise RuntimeError(
                "shipboard.toml sets record_backend='pw-record' but the "
                f"current platform is {platform!r}"
            )
        if not pw:
            raise RuntimeError(
                "shipboard.toml sets record_backend='pw-record' but pw-record "
                "is not installed"
            )
        cmd = [pw, "--rate", str(rate), "--channels", str(channels)]
        if target:
            cmd += ["--target", target]
        cmd.append(str(path))
    elif forced == "ffmpeg":
        cmd = _ffmpeg_cmd(path, rate, channels)
    elif platform == "linux":
        pw = shutil.which("pw-record")
        if pw:
            cmd = [pw, "--rate", str(rate), "--channels", str(channels)]
            if target:
                cmd += ["--target", target]
            cmd.append(str(path))
        else:
            cmd = _ffmpeg_cmd(path, rate, channels)
    else:
        cmd = _ffmpeg_cmd(path, rate, channels)
    if not cmd:
        raise RuntimeError(
            "recording needs pw-record (PipeWire) or ffmpeg; neither is installed"
        )
    return subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


# --------------------------------------------------------------------------
# Notifications (best-effort: never raise)
# --------------------------------------------------------------------------

def _notify_linux(title: str, msg: str) -> None:
    subprocess.run(
        ["notify-send", "-a", "shipboard", title, msg],
        timeout=5, capture_output=True,
    )


def _notify_macos(title: str, msg: str) -> None:
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{msg}" with title "{title}"'],
        timeout=5, capture_output=True,
    )


def _notify_windows(title: str, msg: str) -> None:
    def esc(s: str) -> str:
        return s.replace("'", "''")
    script = "; ".join([
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null",
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime] | Out-Null",
        "$t = [Windows.UI.Notifications.ToastNotificationManager]::"
        "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::"
        "ToastText02)",
        "$txt = $t.GetElementsByTagName('text')",
        "$txt.Item(0).AppendChild($t.CreateTextNode('" + esc(title) + "')) "
        "| Out-Null",
        "$txt.Item(1).AppendChild($t.CreateTextNode('" + esc(msg) + "')) "
        "| Out-Null",
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $t",
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('shipboard').Show($toast)",
    ])
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        timeout=5, capture_output=True,
    )


def notify(title: str, msg: str) -> None:
    """Show a desktop notification; best-effort, never raises.

    Uses notify-send (Linux), osascript (macOS), or a PowerShell toast
    (Windows). A missing tool is silently skipped, matching shipboard's
    existing fire-and-forget notification behavior. ``notify_backend`` in
    shipboard.toml (auto|notify-send|osascript|powershell) forces a backend;
    a forced but unavailable backend raises a RuntimeError.
    """
    platform = detect_platform()
    forced = _backend_choice(
        "notify_backend", ("notify-send", "osascript", "powershell")
    )
    if forced == "notify-send":
        if platform != "linux":
            raise RuntimeError(
                "shipboard.toml sets notify_backend='notify-send' but the "
                f"current platform is {platform!r}"
            )
        if not shutil.which("notify-send"):
            raise RuntimeError(
                "shipboard.toml sets notify_backend='notify-send' but "
                "notify-send is not installed"
            )
        _notify_linux(title, msg)
        return
    if forced == "osascript":
        if platform != "macos":
            raise RuntimeError(
                "shipboard.toml sets notify_backend='osascript' but the "
                f"current platform is {platform!r}"
            )
        if not shutil.which("osascript"):
            raise RuntimeError(
                "shipboard.toml sets notify_backend='osascript' but osascript "
                "is not installed"
            )
        _notify_macos(title, msg)
        return
    if forced == "powershell":
        if platform != "windows":
            raise RuntimeError(
                "shipboard.toml sets notify_backend='powershell' but the "
                f"current platform is {platform!r}"
            )
        if not shutil.which("powershell"):
            raise RuntimeError(
                "shipboard.toml sets notify_backend='powershell' but "
                "powershell is not available"
            )
        _notify_windows(title, msg)
        return
    try:
        if platform == "linux":
            _notify_linux(title, msg)
        elif platform == "macos":
            _notify_macos(title, msg)
        elif platform == "windows":
            _notify_windows(title, msg)
    except Exception:
        pass
