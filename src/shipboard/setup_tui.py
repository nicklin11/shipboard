"""Interactive setup: categorized nested menu — curses TUI + numbered CLI.

Both interfaces mirror the same tree (see config._SETUP_SECTIONS):

  shipboard setup / tui
   1) Recording & keys   [[key_bind]] list (press-to-capture, add/edit/remove)
                         + max_hold, min_recording, tap_stop_silence
   2) STT / whisper      urls, container, language, prompt, normalize
   3) Wake words         enable, phrases, sherpa tuning, cooldown/grace
   4) Sending            paste combo + per-trigger Enter overrides
   5) System             mic source, rate/channels, keep-audio, dry run
   a) Advanced           collapsed: backends, device glob, kws knobs, paths
   s) save   r) restart daemon   t) test STT   q) quit
"""

from __future__ import annotations

import os
import sys
import time
from urllib import request as urllib_request

from .config import (DEFAULT_CONFIG_PATH, DEFAULT_CONFIG_TEXT, MAX_HOLD,
                     MIN_RECORDING, NORMALIZE, PASTE_COMBO, SEND_ENTER,
                     WHISPER_URL, _CFG, _SETUP_FIELDS, _SETUP_SECTIONS,
                     SECTION_ADVANCED, SECTION_RECORDING, _field_defaults,
                     _fmt_value, _parse_value, _save_config_file,
                     _setup_prefill)
from .keys import _key_code, _key_label
from .logstate import _daemon_running

# Sections shown on the home screen, in menu order; Advanced stays collapsed.
TOP_SECTIONS = tuple(s for s, _fields in _SETUP_SECTIONS
                     if s != SECTION_ADVANCED)
KEYS_SECTION = SECTION_RECORDING

_COMPOSITOR_SNIPPETS = """\
Bind the keys to a no-op in your compositor so they don't leak into apps
(kitty encodes unbound keys as CSI-u text). The daemon reads them via evdev.

niri (~/.config/niri/conf/binds.kdl):
    Pause repeat=false { spawn "true"; }
    Scroll_Lock repeat=false { spawn "true"; }
  autostart:  spawn-at-startup "shipboard"

Hyprland:
    bind = , Pause, exec, true
    bind = , Scroll_Lock, exec, true
  autostart:  exec-once = shipboard
"""


# ---------------------------------------------------------------------------
# Shared model helpers (used by both the CLI and the TUI)
# ---------------------------------------------------------------------------

def _config_main() -> int:
    if not DEFAULT_CONFIG_PATH.is_file():
        DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_CONFIG_PATH.write_text(DEFAULT_CONFIG_TEXT)
        print(f"created:   {DEFAULT_CONFIG_PATH}")
        print("edit it, then restart the daemon")
    else:
        print(f"config:    {DEFAULT_CONFIG_PATH}")
    print(f"stt:       {WHISPER_URL}")
    print(f"paste:     {PASTE_COMBO}{' + Enter' if SEND_ENTER else ''}")
    print(f"recording: max_hold {MAX_HOLD}s, min {MIN_RECORDING}s")
    print(f"normalize: {'on' if NORMALIZE else 'off'}")
    return 0


def _health_check(url: str, timeout: float = 3.0) -> str:
    try:
        with urllib_request.urlopen(url, timeout=timeout) as resp:
            return "OK" if 200 <= resp.status < 300 else f"HTTP {resp.status}"
    except Exception as exc:
        return f"FAIL: {exc}"


def _restart_msg() -> str:
    from .cli import _restart_daemon  # deferred: cli imports this module at load time (cycle)
    return _restart_daemon()


def _section_fields(section: str) -> list:
    """[(flat index into _SETUP_FIELDS, field 6-tuple), ...] for one section."""
    return [(i, f) for i, f in enumerate(_SETUP_FIELDS) if f[3] == section]


def _bind_summary(b: dict) -> str:
    """One-line bind summary: toggle/hold/tap in daemon precedence order."""
    segs = []
    if b.get("toggle"):
        segs.append(f"toggle={b['toggle']}")
    if b.get("hold"):
        segs.append(f"hold={b['hold']}@{b.get('hold_threshold', 0.25):g}s")
    if b.get("tap"):
        segs.append(f"tap={b['tap']}")
    return " · ".join(segs) if segs else "(off)"


def _binds_line(values: dict) -> str:
    binds = values.get("_key_binds") or []
    if not binds:
        return "no keys yet"
    return "  |  ".join(f"{_key_label(b['key'])}: {_bind_summary(b)}"
                        for b in binds)


# ---------------------------------------------------------------------------
# CLI: press-to-capture + action pickers + bind list operations
# (the curses twins live inside _tui_main: _capture_key / _choose_action)
# ---------------------------------------------------------------------------

def _capture_key_cli() -> str | None:
    """Press-to-capture (evdev); returns the key name or None."""
    print(" Press the key to bind (8s, Esc cancels)…")
    try:
        import evdev
        import select
        devs = []
        for path in evdev.list_devices():
            try:
                d = evdev.InputDevice(path)
                caps = d.capabilities(verbose=False)
                if caps.get(evdev.ecodes.EV_KEY):
                    devs.append(d)
            except Exception:
                continue
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            r, _, _ = select.select(devs, [], [], 0.1)
            for d in r:
                try:
                    for ev in d.read():
                        if ev.type == evdev.ecodes.EV_KEY and ev.value == 1:
                            raw = None
                            if hasattr(evdev.ecodes, "KEY"):
                                raw = evdev.ecodes.KEY.get(ev.code)
                            if raw is None:
                                for attr in dir(evdev.ecodes):
                                    if attr.startswith("KEY_") and \
                                            getattr(evdev.ecodes, attr) == ev.code:
                                        raw = attr
                                        break
                            name = raw[4:].lower() if raw and raw.startswith("KEY_") \
                                else str(ev.code)
                            print(f"  captured: {name} ({_key_label(name)})")
                            return name
                except Exception:
                    pass
    except Exception:
        pass
    return None


_ACTIONS_CLI = (("record", "copy only"), ("record_send", "copy+paste+Enter"),
                ("paste", "paste clipboard"), ("", "off"))


def _pick_action_cli(prompt: str, default: str = "") -> str:
    print(f"  {prompt}")
    for i, (v, lbl) in enumerate(_ACTIONS_CLI, start=1):
        cur = " ←" if v == default else ""
        print(f"   {i}) {v or 'off':14} {lbl}{cur}")
    raw = input("  choose 1-4 [Enter keeps]: ").strip()
    if not raw:
        return default
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(_ACTIONS_CLI):
            return _ACTIONS_CLI[idx][0]
    except ValueError:
        low = raw.lower()
        if low in ("off", "-", ""):
            return ""
        if low in ("record", "record_send", "paste"):
            return low
    return default


def _ask_threshold_cli(default: float) -> float | None:
    """Per-bind hold threshold; Enter keeps the default, bad input -> None."""
    raw = input(f"  hold threshold [{default:g}] > ").strip()
    if not raw:
        return default
    try:
        thr = float(raw)
        if 0 < thr <= 5:
            return thr
    except ValueError:
        pass
    return None


def _add_bind_cli(binds: list) -> str:
    cap = _capture_key_cli()
    k = cap or input("  key (e.g. rightalt/f13) > ").strip().lower()
    if not k:
        return "no key given"
    try:
        _key_code(k)
    except SystemExit as e:
        return str(e)
    if any(b.get("key") == k for b in binds):
        return f"duplicate key {k!r} — each key once"
    tap = _pick_action_cli("tap (short press) —", "")
    hold = _pick_action_cli("hold (long press) —", "")
    tog = _pick_action_cli("toggle (press to start/stop) —", "")
    thr = _ask_threshold_cli(0.25)
    if thr is None:
        return "bad threshold (0..5) — bind not added"
    binds.append({"key": k, "tap": tap, "hold": hold,
                  "toggle": tog, "hold_threshold": thr})
    return (f"added {k}  tap={tap or 'off'} hold={hold or 'off'} "
            f"toggle={tog or 'off'} @{thr:g}s")


def _edit_bind_cli(binds: list) -> str:
    if not binds:
        return "no binds yet — add one first ([n])"
    for i, b in enumerate(binds, start=1):
        print(f"   {i}. {_key_label(b['key']):<14} {_bind_summary(b)}")
    raw = input("  edit which > ").strip()
    try:
        b = binds[int(raw) - 1]
    except (ValueError, IndexError):
        return "invalid index"
    print(f"  editing {b['key']} — press new key or Enter keeps")
    cap = _capture_key_cli()
    nk = cap or input(f"   key [{b['key']}] > ").strip().lower() or b["key"]
    if nk != b["key"] and any(x is not b and x.get("key") == nk for x in binds):
        return f"duplicate key {nk!r}"
    try:
        _key_code(nk)
    except SystemExit as e:
        return str(e)
    b["tap"] = _pick_action_cli(f"tap (was {b.get('tap') or 'off'}) —", b.get("tap", ""))
    b["hold"] = _pick_action_cli(f"hold (was {b.get('hold') or 'off'}) —", b.get("hold", ""))
    b["toggle"] = _pick_action_cli(f"toggle (was {b.get('toggle') or 'off'}) —",
                                   b.get("toggle", ""))
    thr = _ask_threshold_cli(b.get("hold_threshold", 0.25))
    if thr is not None:
        b["hold_threshold"] = thr
    b["key"] = nk
    return f"updated {nk}"


def _remove_bind_cli(binds: list) -> str:
    if not binds:
        return "no binds"
    for i, b in enumerate(binds, start=1):
        print(f"   {i}. {_key_label(b['key']):<14} {b['key']}")
    raw = input("  remove which > ").strip()
    try:
        b = binds.pop(int(raw) - 1)
    except (ValueError, IndexError):
        return "invalid index"
    return f"removed {b['key']}"


def _cli_edit_field(values: dict, field: tuple) -> str:
    """Field edit prompt: name, one-line 'what it does', current, format."""
    key, label, conv, _section, desc, fmt = field
    cur = _fmt_value(values[key])
    print(f"\n  {label} — {desc}")
    print(f"  current: {cur}   accepted: {fmt}")
    if conv is bool:
        ans = input(f"  {label} [{cur}] (y/n, Enter=keep): ").strip().lower()
        if ans in ("y", "yes", "on", "1"):
            values[key] = True
            return f"{key} = yes"
        if ans in ("n", "no", "off", "0"):
            values[key] = False
            return f"{key} = no"
        return f"{key} unchanged"
    raw = input(f"  {label} [{cur}] > ").strip()
    if not raw:
        return f"{key} unchanged"
    try:
        values[key] = _parse_value(raw, conv)
    except ValueError:
        return "invalid value, not saved"
    return f"{key} = {_fmt_value(values[key])}"


# ---------------------------------------------------------------------------
# CLI: categorized nested menu (`shipboard setup` without a tty / --cli)
# ---------------------------------------------------------------------------

def _setup_main() -> int:
    """Numbered CLI menu mirroring the TUI tree: section number -> field number."""
    values = _field_defaults()
    values.update(_CFG)
    _setup_prefill(values)
    stt_cache: list = []  # one startup probe; [t] refreshes (short timeout: never block the first render)

    def _stt_line() -> str:
        if not stt_cache:
            stt_cache.append(_health_check(values["whisper_health_url"], timeout=1))
        return stt_cache[0]

    message = ""
    while True:
        print("\n" + "─" * 64)
        print(f" shipboard setup — {DEFAULT_CONFIG_PATH}")
        print(f" daemon: {'running' if _daemon_running() else 'off'}"
              f"   ·   stt: {_stt_line()}")
        print("─" * 64)
        for n, name in enumerate(TOP_SECTIONS, start=1):
            extra = f"   {_binds_line(values)}" if name == KEYS_SECTION else ""
            print(f" {n}) {name:<18} {len(_section_fields(name))} settings{extra}")
        n_adv = len(_section_fields(SECTION_ADVANCED))
        print(f" a) Advanced — collapsed ({n_adv} settings)   [a] to open")
        print("─" * 64)
        print(" [1-5] section · [a] advanced · [s] save · [t] test STT ·"
              " [r] restart · [q] quit")
        if message:
            print(f" {message}")
            message = ""
        try:
            choice = input(" > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ("q", "quit", "exit"):
            return 0
        if choice == "s":
            _save_config_file(values)
            message = f"saved to {DEFAULT_CONFIG_PATH} — restart the daemon to apply (r)"
        elif choice == "t":
            message = "STT health: " + _health_check(values["whisper_health_url"])
        elif choice == "r":
            message = _restart_msg()
        elif choice == "a":
            _cli_section_loop(values, SECTION_ADVANCED)
        elif choice.isdigit() and 1 <= int(choice) <= len(TOP_SECTIONS):
            _cli_section_loop(values, TOP_SECTIONS[int(choice) - 1])
        else:
            message = f"unknown command: {choice}"


def _cli_section_loop(values: dict, section: str) -> None:
    """One section page of the CLI. Numbers edit that section's fields;
    section 1 also exposes the key_bind list via n/e/d/c."""
    fields = _section_fields(section)
    is_keys = section == KEYS_SECTION
    message = ""
    while True:
        print("\n" + "─" * 64)
        print(f" == {section} ==")
        if section == SECTION_ADVANCED:
            print("  (backends & paths — keep 'auto' unless something breaks)")
        binds = values.get("_key_binds") or []
        if is_keys:
            print("  keys — one line per [[key_bind]] (press-to-capture):")
            if binds:
                for i, b in enumerate(binds, start=1):
                    print(f"   {i}. {_key_label(b['key']):<14} {_bind_summary(b)}")
            else:
                print("   (none — [n] add: press the key, then pick tap/hold/toggle)")
            print("   [n] add bind · [e] edit bind · [d] delete bind ·"
                  " [c] compositor hints")
            print("  fields:")
        for i, (_idx, f) in enumerate(fields, start=1):
            key, label, _conv, _sec, desc, fmt = f
            mark = "*" if key in _CFG else " "
            print(f"  {i:2}){mark} {label:<26} — {desc}")
            print(f"        now: {_fmt_value(values[key])}   [{fmt}]")
        print("  " + "─" * 60)
        print("  [num] edit field · [s] save · [t] test STT · [r] restart · [q] back")
        if message:
            print(f"  {message}")
            message = ""
        try:
            choice = input(" > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if choice in ("q", "b", "back", ""):
            return
        if choice == "s":
            _save_config_file(values)
            message = f"saved to {DEFAULT_CONFIG_PATH} — restart the daemon to apply (r)"
        elif choice == "t":
            message = "STT health: " + _health_check(values["whisper_health_url"])
        elif choice == "r":
            message = _restart_msg()
        elif is_keys and choice == "n":
            message = _add_bind_cli(values.setdefault("_key_binds", []))
        elif is_keys and choice == "e":
            message = _edit_bind_cli(values.setdefault("_key_binds", []))
        elif is_keys and choice == "d":
            message = _remove_bind_cli(values.setdefault("_key_binds", []))
        elif is_keys and choice == "c":
            print("\n" + _COMPOSITOR_SNIPPETS)
            input(" [press Enter to return] ")
        elif choice.isdigit():
            n = int(choice)
            if 1 <= n <= len(fields):
                message = _cli_edit_field(values, fields[n - 1][1])
            else:
                message = f"no field #{choice} — 1..{len(fields)}"
        else:
            message = f"unknown command: {choice}"


# ---------------------------------------------------------------------------
# TUI (curses): same tree as full-screen pages
# ---------------------------------------------------------------------------

def _tui_main() -> int:
    """Full-screen curses setup — home shows the 5 sections + collapsed
    Advanced; section 1 holds the [[key_bind]] list. ↑/↓ navigate, Enter
    open/edit, s save, t test STT, r restart, q/Esc back (quit from home).
    Falls back to `shipboard setup` when curses is unavailable."""
    try:
        import curses
    except ImportError:
        print("curses unavailable, use: shipboard setup", file=sys.stderr)
        return 1
    if os.environ.get("TERM") == "dumb" or not sys.stdin.isatty():
        print("curses unavailable, use: shipboard setup", file=sys.stderr)
        return 1

    values = _field_defaults()
    values.update(_CFG)
    _setup_prefill(values)

    def _prompt_line(stdscr_, prompt, default=""):
        h_, w_ = stdscr_.getmaxyx()
        try:
            stdscr_.move(h_ - 1, 0)
            stdscr_.clrtoeol()
            stdscr_.addnstr(h_ - 1, 0,
                            prompt + (f" [{default}]" if default else "") + " ", w_ - 1)
            stdscr_.noutrefresh()
            curses.doupdate()
        except curses.error:
            pass
        curses.echo()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            raw = stdscr_.getstr(h_ - 1,
                                 len(prompt) + 2 + (len(default) + 3 if default else 1), 50)
            s = raw.decode(errors="replace").strip()
        except curses.error:
            s = ""
        finally:
            curses.noecho()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        return s if s else default

    def _capture_key(stdscr_) -> str | None:
        """Press-to-capture: wait for the next evdev press; Esc cancels."""
        hint = "Press the key to bind  (Esc cancels)…"
        h_, w_ = stdscr_.getmaxyx()
        try:
            stdscr_.move(h_ - 1, 0)
            stdscr_.clrtoeol()
            stdscr_.addnstr(h_ - 1, 0, hint, w_ - 1)
            stdscr_.noutrefresh()
            curses.doupdate()
        except curses.error:
            pass
        try:
            import evdev
            import select
            devs = []
            for path in evdev.list_devices():
                try:
                    d = evdev.InputDevice(path)
                    caps = d.capabilities(verbose=False)
                    if caps.get(evdev.ecodes.EV_KEY):
                        devs.append(d)
                except Exception:
                    continue
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                r, _, _ = select.select(devs, [], [], 0.1)
                for d in r:
                    try:
                        for ev in d.read():
                            if ev.type == evdev.ecodes.EV_KEY and ev.value == 1:
                                raw = None
                                name_to_code = getattr(evdev.ecodes, "KEY", {})
                                rev = {v: k for k, v in name_to_code.items()}
                                raw = rev.get(ev.code)
                                if raw is None:
                                    for attr in dir(evdev.ecodes):
                                        if attr.startswith("KEY_") and \
                                                getattr(evdev.ecodes, attr) == ev.code:
                                            raw = attr
                                            break
                                name = raw[4:].lower() if raw and raw.startswith("KEY_") \
                                    else str(ev.code)
                                return name
                    except Exception:
                        pass
                # also poll curses for Esc
                try:
                    stdscr_.nodelay(True)
                    ch = stdscr_.getch()
                    if ch == 27:
                        return None
                finally:
                    stdscr_.nodelay(False)
        except Exception:
            pass
        try:
            stdscr_.nodelay(False)
        except curses.error:
            pass
        # evdev unavailable / timed out — caller falls back to a typed name
        return None

    def _choose_action(stdscr_, prompt, current=""):
        """Action picker: centered popup list, ↑/↓ + Enter; Esc keeps value."""
        items = [("record", "copy only"), ("record_send", "copy+paste+Enter"),
                 ("paste", "paste clipboard"), ("", "off")]
        sel = 0
        for i, (v, _) in enumerate(items):
            if v == current:
                sel = i
                break
        h_, w_ = stdscr_.getmaxyx()
        bh = min(len(items) + 6, max(5, h_ - 2))
        bw = min(max(40, len(prompt) + 6), max(20, w_ - 4))
        y0 = max(0, (h_ - bh) // 2 - 2)
        x0 = max(0, (w_ - bw) // 2)
        win = curses.newwin(bh, bw, y0, x0)
        win.keypad(True)
        while True:
            try:
                win.erase()
                win.box()
                win.addnstr(1, 2, prompt[:bw - 4], bw - 4, curses.A_BOLD)
                for i, (v, lbl) in enumerate(items):
                    mark = "▶" if i == sel else " "
                    txt = f" {mark} {(v or 'off'):<12} {lbl}"
                    attr = curses.A_REVERSE if i == sel else 0
                    win.addnstr(3 + i, 1, txt[:bw - 2], bw - 2, attr)
                win.addnstr(bh - 2, 2, "↑/↓ move · Enter choose · Esc cancel",
                            bw - 4, curses.A_DIM)
                win.refresh()
            except curses.error:
                pass
            ch = win.getch()
            if ch in (curses.KEY_UP, curses.KEY_LEFT):
                sel = (sel - 1) % len(items)
            elif ch in (curses.KEY_DOWN, curses.KEY_RIGHT, 9):  # Tab
                sel = (sel + 1) % len(items)
            elif ch in (10, 13, curses.KEY_ENTER, ord(" ")):
                return items[sel][0]
            elif ch == 27:
                return current

    def _edit_bind_page(stdscr, bind, binds) -> None:
        """Per-bind editor: one option per line. Key = press-to-capture,
        tap/hold/toggle = action pickers, hold_delay = seconds prompt."""
        rows = [
            ("Key", "key", "press key to re-bind"),
            ("Tap (short)", "tap", "record / record_send / paste / off"),
            ("Hold (long)", "hold", "record / record_send / paste / off"),
            ("Hold delay (s)", "hold_threshold", "seconds 0–5"),
            ("Toggle (start/stop)", "toggle",
             "record / record_send / paste / off (overrides tap)"),
        ]
        sel = 0
        while True:
            h_, w_ = stdscr.getmaxyx()
            try:
                stdscr.erase()
                title = f" Bind: {_key_label(bind['key'])} ({bind['key']}) "
                stdscr.addnstr(0, 0, title + "─" * max(0, w_ - len(title)),
                               w_ - 1, curses.A_REVERSE)
                for i, (label, field, field_hint) in enumerate(rows):
                    if field == "key":
                        cur = _key_label(bind["key"])
                    elif field == "hold_threshold":
                        cur = f"{bind.get('hold_threshold', 0.25):g}s"
                    else:
                        cur = bind.get(field, "") or "off"
                    line = f" {i + 1}. {label:<20} {cur:<16} [{field_hint}]"
                    attrs = curses.A_REVERSE if i == sel else 0
                    stdscr.addnstr(2 + i, 1, line[:w_ - 2], w_ - 2, attrs)
                note = ("tap = short press · hold = long press · toggle = "
                        "press to start/stop (overrides tap)")
                stdscr.addnstr(2 + len(rows) + 1, 1, note[:w_ - 2], w_ - 2, curses.A_DIM)
                stdscr.addnstr(h_ - 1, 0, " ↑/↓ move · Enter edit line · q/Esc back", w_ - 1)
                stdscr.noutrefresh()
                curses.doupdate()
            except curses.error:
                pass
            ch = stdscr.getch()
            if ch in (27, ord("q"), ord("Q")):
                return
            elif ch == curses.KEY_UP:
                sel = (sel - 1) % len(rows)
            elif ch == curses.KEY_DOWN:
                sel = (sel + 1) % len(rows)
            elif ch in (10, 13, curses.KEY_ENTER, ord(" ")):
                field = rows[sel][1]
                if field == "key":
                    rec = _capture_key(stdscr)
                    if rec is None:
                        rec = (_prompt_line(stdscr, "key name", bind["key"])
                               .strip().lower() or bind["key"])
                    if rec == bind["key"]:
                        continue
                    if any(x is not bind and x.get("key") == rec for x in binds):
                        continue  # duplicate — keep the old key
                    try:
                        _key_code(rec)
                    except SystemExit:
                        continue  # unknown key name — keep the old key
                    bind["key"] = rec
                elif field == "hold_threshold":
                    raw = _prompt_line(stdscr, "hold delay seconds (0–5)",
                                       f"{bind.get('hold_threshold', 0.25):g}").strip()
                    try:
                        v = float(raw)
                    except ValueError:
                        continue  # bad input — keep the old threshold
                    if 0 < v <= 5:
                        bind["hold_threshold"] = v
                else:  # tap / hold / toggle
                    bind[field] = _choose_action(stdscr, f"{label}:",
                                                 bind.get(field, "") or "")

    def _add_bind_flow(stdscr) -> str:
        """Capture a key, append the bind, drop into its editor."""
        binds = values.setdefault("_key_binds", [])
        if len(binds) >= 3:
            return "max 3 binds — remove one first (d)"
        name = _capture_key(stdscr)
        if not name:
            name = _prompt_line(stdscr, "key name (e.g. rightalt)", "").strip().lower()
        if not name:
            return "no key — bind not added"
        try:
            _key_code(name)
        except SystemExit as e:
            return str(e)
        if any(b.get("key") == name for b in binds):
            return f"duplicate key {name!r} — each key once"
        bind = {"key": name, "tap": "", "hold": "", "toggle": "",
                "hold_threshold": 0.25}
        binds.append(bind)
        _edit_bind_page(stdscr, bind, binds)
        return f"added {name}: {_bind_summary(bind)}"

    def _edit_field_page(stdscr, field) -> str:
        """Field editor: prompt shows name, description, current, format."""
        key, label, conv, _section, desc, fmt = field
        h, w = stdscr.getmaxyx()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        message = ""
        if conv is bool:
            prompt = (f" {label} — {desc}   now: {_fmt_value(values[key])}   "
                      f"[{fmt}]   space/y = yes · n = no · Esc = keep")
            try:
                stdscr.move(h - 1, 0)
                stdscr.clrtoeol()
                stdscr.addnstr(h - 1, 0, prompt, w - 1)
                stdscr.noutrefresh()
                curses.doupdate()
            except curses.error:
                pass
            while True:
                ch = stdscr.getch()
                if ch == 27:
                    break
                if ch == ord(" "):
                    values[key] = not values[key]
                    break
                if ch in (ord("y"), ord("Y")):
                    values[key] = True
                    break
                if ch in (ord("n"), ord("N")):
                    values[key] = False
                    break
            message = f"{key} = {_fmt_value(values[key])}"
        else:
            buf = _fmt_value(values[key])
            while True:
                prompt = f" {label} — {desc}   [{fmt}]   [{buf}]"
                try:
                    stdscr.move(h - 1, 0)
                    stdscr.clrtoeol()
                    stdscr.addnstr(h - 1, 0, prompt, w - 1)
                    stdscr.noutrefresh()
                    curses.doupdate()
                except curses.error:
                    pass
                ch = stdscr.getch()
                if ch == 27:
                    message = f"{key} unchanged"
                    break
                if ch in (10, 13, curses.KEY_ENTER):
                    try:
                        values[key] = _parse_value(buf, conv)
                        message = f"{key} = {_fmt_value(values[key])}"
                    except ValueError:
                        message = f"invalid {conv.__name__} — not saved"
                    break
                if ch in (8, 127, curses.KEY_BACKSPACE):
                    buf = buf[:-1]
                elif 32 <= ch < 127:
                    buf += chr(ch)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        return message

    def _show_binds(stdscr) -> None:
        """Compositor no-op bind snippets ([c] inside section 1)."""
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        for y, ln in enumerate(_COMPOSITOR_SNIPPETS.splitlines()):
            if y >= h - 1:
                break
            try:
                stdscr.addnstr(y, 0, ln, w - 1)
            except curses.error:
                pass
        try:
            stdscr.addnstr(h - 1, 0, " q / Esc to close ", w - 1, curses.A_REVERSE)
        except curses.error:
            pass
        stdscr.noutrefresh()
        curses.doupdate()
        while True:
            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q"), 27):
                break

    def run(stdscr) -> int:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        try:
            curses.use_default_colors()
        except curses.error:
            pass

        page = "home"      # "home" | "section"
        section = None     # section name while page == "section"
        sel = 0
        message = ""
        # short startup probe so the first frame is never delayed long;
        # [t] re-tests with the full 3s timeout
        stt_status = _health_check(values["whisper_health_url"], timeout=1)

        def _home_rows():
            rows = []
            for n, name in enumerate(TOP_SECTIONS, start=1):
                if name == KEYS_SECTION:
                    detail = _binds_line(values)
                else:
                    detail = f"{len(_section_fields(name))} settings"
                rows.append(("section", name, f"{n}) {name}", detail))
            n_adv = len(_section_fields(SECTION_ADVANCED))
            rows.append(("section", SECTION_ADVANCED, "a) Advanced",
                         f"collapsed — {n_adv} settings"))
            return rows

        def _section_rows(name):
            rows = []
            if name == KEYS_SECTION:
                for i, b in enumerate(values.get("_key_binds") or []):
                    rows.append(("bind", i, _key_label(b["key"]), _bind_summary(b)))
            for idx, f in _section_fields(name):
                key, label, _conv, _sec, _desc, _fmt = f
                mark = "*" if key in _CFG else " "
                rows.append(("field", idx, f"{mark} {label}", _fmt_value(values[key])))
            return rows

        def _detail(rows_) -> str:
            if not rows_ or sel >= len(rows_):
                return ""
            kind, payload, left, _right = rows_[sel]
            if kind == "section":
                return f"{left} — Enter to open"
            if kind == "bind":
                return (f"{left} — Enter edit · n add · d delete · "
                        "c compositor hints · toggle overrides tap")
            _key, label, _conv, _sec, desc, fmt = _SETUP_FIELDS[payload]
            return f"{label} — {desc}   [{fmt}]"

        while True:
            rows = _home_rows() if page == "home" else _section_rows(section)
            sel = max(0, min(sel, len(rows) - 1)) if rows else 0

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            try:
                _home = os.path.expanduser("~")
                _path_disp = str(DEFAULT_CONFIG_PATH).replace(_home, "~", 1)
                title = (f" shipboard setup — {_path_disp}   "
                         f"daemon: {'on' if _daemon_running() else 'off'} ")
                stdscr.addnstr(0, 0, title, w - 1, curses.A_REVERSE)
            except curses.error:
                pass

            if page == "home":
                try:
                    stdscr.addnstr(1, 0, f" stt: {stt_status}", w - 1, curses.A_DIM)
                except curses.error:
                    pass
                y = 3
                for i, (_kind, _payload, left, right) in enumerate(rows):
                    attrs = curses.A_REVERSE if i == sel else curses.A_BOLD
                    try:
                        stdscr.addnstr(y, 0, f" {left:<20} {right}", w - 1, attrs)
                    except curses.error:
                        pass
                    y += 1
                hint = (" 1-5/a section · Enter open · s save · t test STT · "
                        "r restart · q quit")
            else:
                if section == KEYS_SECTION:
                    try:
                        stdscr.addnstr(1, 0,
                                       " keys: n add (press key) · Enter edit · "
                                       "d delete · c compositor hints", w - 1, curses.A_DIM)
                    except curses.error:
                        pass
                y = 3 if section == KEYS_SECTION else 2
                for i, (_kind, _payload, left, right) in enumerate(rows):
                    attrs = curses.A_REVERSE if i == sel else 0
                    try:
                        stdscr.addnstr(y, 0, f" {left:<24} {right}", w - 1, attrs)
                    except curses.error:
                        pass
                    y += 1
                hint = (" ↑/↓ move · Enter edit · s save · t test STT · "
                        "r restart · q/Esc back")

            try:
                stdscr.addnstr(h - 2, 0, f" {message or _detail(rows)}", w - 1,
                               curses.A_DIM)
            except curses.error:
                pass
            try:
                stdscr.addnstr(h - 1, 0, hint, w - 1)
            except curses.error:
                pass
            stdscr.noutrefresh()
            curses.doupdate()

            ch = stdscr.getch()
            if ch == curses.KEY_UP:
                sel -= 1
            elif ch == curses.KEY_DOWN:
                sel += 1
            elif ch == curses.KEY_RESIZE:
                continue
            elif page == "home":
                if ch in (10, 13, curses.KEY_ENTER) and rows:
                    section = rows[sel][1]
                    page = "section"
                    sel = 0
                    message = ""
                elif ord("1") <= ch <= ord("0") + len(TOP_SECTIONS):
                    section = TOP_SECTIONS[ch - ord("1")]
                    page = "section"
                    sel = 0
                    message = ""
                elif ch in (ord("a"), ord("A")):
                    section = SECTION_ADVANCED
                    page = "section"
                    sel = 0
                    message = ""
                elif ch == ord("s"):
                    _save_config_file(values)
                    message = "saved — restart the daemon to apply (r)"
                elif ch == ord("t"):
                    stt_status = _health_check(values["whisper_health_url"])
                    message = "STT health: " + stt_status
                elif ch == ord("r"):
                    message = _restart_msg()
                elif ch in (ord("q"), ord("Q"), 27):
                    return 0
            else:  # section page
                if ch in (10, 13, curses.KEY_ENTER) and rows:
                    kind, payload = rows[sel][0], rows[sel][1]
                    if kind == "bind":
                        bind = (values.get("_key_binds") or [])[payload]
                        _edit_bind_page(stdscr, bind,
                                        values.setdefault("_key_binds", []))
                        message = "keys: " + _binds_line(values)
                    else:
                        message = _edit_field_page(stdscr, _SETUP_FIELDS[payload])
                elif ch in (ord("n"), ord("N")) and section == KEYS_SECTION:
                    message = _add_bind_flow(stdscr)
                elif ch in (ord("d"), ord("x")) and section == KEYS_SECTION and rows \
                        and rows[sel][0] == "bind":
                    removed = (values.get("_key_binds") or []).pop(rows[sel][1])
                    message = f"removed {_key_label(removed['key'])}"
                elif ch in (ord("c"), ord("C")) and section == KEYS_SECTION:
                    _show_binds(stdscr)
                elif ch == ord("s"):
                    _save_config_file(values)
                    message = "saved — restart the daemon to apply (r)"
                elif ch == ord("t"):
                    stt_status = _health_check(values["whisper_health_url"])
                    message = "STT health: " + stt_status
                elif ch == ord("r"):
                    message = _restart_msg()
                elif ch in (ord("q"), ord("Q"), 27):
                    page = "home"
                    section = None
                    sel = 0
                    message = ""
        return 0

    try:
        return curses.wrapper(run)
    except KeyboardInterrupt:
        return 0
