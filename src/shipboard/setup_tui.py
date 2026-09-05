"""Interactive setup: curses TUI + numbered CLI dialog."""

from __future__ import annotations

import os
import sys
import time
from urllib import request as urllib_request

from .config import (DEFAULT_CONFIG_PATH, DEFAULT_CONFIG_TEXT, MAX_HOLD,
                     MIN_RECORDING, NORMALIZE, PASTE_COMBO, SEND_ENTER,
                     WHISPER_URL, _CFG, _SETUP_FIELDS, _field_defaults,
                     _fmt_value, _parse_value, _save_config_file,
                     _setup_prefill)
from .keys import _key_code, _key_label
from .logstate import _daemon_running

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


def _health_check(url: str) -> str:
    try:
        with urllib_request.urlopen(url, timeout=3) as resp:
            return "OK" if 200 <= resp.status < 300 else f"HTTP {resp.status}"
    except Exception as exc:
        return f"FAIL: {exc}"


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


def _setup_main() -> int:
    """Interactive CLI menu (no curses — inherits the terminal theme)."""
    from .cli import _restart_daemon  # deferred: cli imports this module at load time (cycle)
    values = _field_defaults()
    values.update(_CFG)
    _setup_prefill(values)
    def _capture_key_cli() -> str | None:
        hint = "Press the key to bind (8s, Esc cancels)…"
        print(f" {hint}")
        try:
            import evdev, select
            devs = []
            for path in evdev.list_devices():
                try:
                    d = evdev.InputDevice(path)
                    caps = d.capabilities(verbose=False)
                    if caps.get(evdev.ecodes.EV_KEY):
                        devs.append(d)
                except Exception:
                    continue
            import time as _time
            deadline = _time.monotonic() + 8
            while _time.monotonic() < deadline:
                r,_,_ = select.select(devs, [], [], 0.1)
                for d in r:
                    try:
                        for ev in d.read():
                            if ev.type == evdev.ecodes.EV_KEY and ev.value == 1:
                                raw = None
                                if hasattr(evdev.ecodes, "KEY"):
                                    raw = evdev.ecodes.KEY.get(ev.code)
                                if raw is None:
                                    for attr in dir(evdev.ecodes):
                                        if attr.startswith("KEY_") and getattr(evdev.ecodes, attr) == ev.code:
                                            raw = attr; break
                                name = raw[4:].lower() if raw and raw.startswith("KEY_") else str(ev.code)
                                print(f"  captured: {name} ({_key_label(name)})")
                                return name
                    except Exception:
                        pass
        except Exception:
            pass
        return None

    def _pick_action_cli(prompt, default=""):
        opts = [("record","copy only"), ("record_send","copy+paste+Enter"), ("paste","paste clipboard"), ("","off")]
        # show numbered picker
        print(f"  {prompt}")
        for i,(v,lbl) in enumerate(opts, start=1):
            cur = " ←" if v==default else ""
            print(f"   {i}) {v or 'off':14} {lbl}{cur}")
        raw = input("  choose 1-4 [Enter keeps]: ").strip()
        if not raw:
            return default
        try:
            idx = int(raw)-1
            if 0 <= idx < len(opts):
                return opts[idx][0]
        except ValueError:
            low = raw.lower()
            if low in ("off","-",""): return ""
            if low in ("record","record_send","paste"): return low
        return default

    show_advanced = False
    message = ""
    while True:
        print("\n" + "─" * 60)
        print(f" shipboard setup — {DEFAULT_CONFIG_PATH}   (daemon: {'on' if _daemon_running() else 'off'})")
        print("─" * 60)
        # ── Keys quick-card (always on top — this is what people tweak most) ──
        binds = values.get("_key_binds") or []
        if binds:
            print("  Keys  (tap = short press, hold = long press, toggle = press to start/stop)")
            for idx, b in enumerate(binds, start=1):
                tap = b.get("tap",""); hold = b.get("hold",""); tog = b.get("toggle","")
                thr = b.get("hold_threshold", 0.25)
                parts = []
                if tog: parts.append(f"toggle={tog}")
                if hold: parts.append(f"hold {hold} @ {thr:g}s")
                if tap: parts.append(f"tap {tap}")
                hint = "  (toggle overrides tap)" if tog and (tap or hold) else ""
                print(f"   • {_key_label(b.get('key','')):<12}  {'  ·  '.join(parts) if parts else '(off)'}{hint}")
        else:
            print("  Keys  (none yet — add your first binding)")

        print("     [k] keys: add / edit / remove")
        # ── Essentials + Recording + collapsed sections ──
        last_section = None
        for i, (key, label, conv, section) in enumerate(_SETUP_FIELDS, start=1):
            is_adv = (section == "Advanced")
            if is_adv:
                if not show_advanced and section != last_section:
                    adv_count = sum(1 for _,_,_,s in _SETUP_FIELDS if s == "Advanced")
                    print(f"\n  … Advanced  ({adv_count} settings)  [a] show / hide")
                    last_section = section
                    continue
                elif not show_advanced:
                    last_section = section
                    continue
            if section != last_section:
                print(f"\n  == {section} ==")
                last_section = section
            mark = "*" if key in _CFG else " "
            print(f" {i:2}) {mark} {label:<40} {_fmt_value(values[key])}")
        print("─" * 60)
        print(" [num] edit setting  ·  [k] keys  ·  [a] advanced  ·  [s] save  ·  [t] test STT  ·  [p] compositor"
              "  ·  [r] restart  ·  [q] quit")
        if message:
            print(f" {message}")
        try:
            choice = input(" > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        message = ""
        if choice in ("q", "quit", "exit"):
            return 0
        if choice == "a":
            show_advanced = not show_advanced
            message = "advanced shown" if show_advanced else "advanced hidden"
        elif choice == "k":
            binds = values.setdefault("_key_binds", [])
            while True:
                print("\n" + "─"*50)
                print(" Keys  (SEPARATE PAGE — press to capture, then pick tap/hold/toggle one by one)")
                if not binds:
                    print("  (no keys — press a to add)")
                else:
                    for i, b in enumerate(binds, start=1):
                        tap = b.get("tap","") or "off"; hold = b.get("hold","") or "off"; tog = b.get("toggle","") or "off"; thr = b.get("hold_threshold",0.25)
                        print(f"  {i}. {_key_label(b['key']):12}  tap={tap:12}  hold={hold:12}@{thr:g}s  toggle={tog:12}")
                    print("  toggle overrides tap; hold+tap idempotent")
                print("  [a] add (press key)  [e] edit  [d] delete  [q] back")
                sub = input(" keys> ").strip().lower()
                if sub in ("q", "back", "b", ""):
                    break
                if sub in ("a", "1"):
                    cap = _capture_key_cli()
                    k = (cap or input(" key (e.g. rightalt/f13) > ").strip().lower())
                    if not k:
                        message = "no key given"
                    else:
                        try:
                            _key_code(k)
                        except SystemExit as e:
                            message = str(e)
                        else:
                            if any(b.get("key")==k for b in binds):
                                message = f"duplicate key {k!r} — each key once"
                            else:
                                tap = _pick_action_cli("tap (short press) —", "")
                                hold = _pick_action_cli("hold (long press) —", "")
                                tog = _pick_action_cli("toggle (press to start/stop) —", "")
                                thr_raw = input("  hold threshold [0.25] > ").strip() or "0.25"
                                try:
                                    thr = float(thr_raw)
                                    assert 0 < thr <= 5
                                except Exception:
                                    message = "bad threshold (0..5)"
                                else:
                                    binds.append({"key":k,"tap":tap,"hold":hold,"toggle":tog,"hold_threshold":thr})
                                    message = f"added {k}  tap={tap or 'off'} hold={hold or 'off'} toggle={tog or 'off'}"
                elif sub in ("e", "2"):
                    if not binds:
                        message = "no binds yet — add one first"
                    else:
                        for i,b in enumerate(binds, start=1):
                            print(f"  {i}) {_key_label(b['key']):12}  tap={b.get('tap','') or 'off'}  hold={b.get('hold','') or 'off'}  toggle={b.get('toggle','') or 'off'}  @{b.get('hold_threshold',0.25):g}s")
                        raw = input(" edit which > ").strip()
                        try:
                            idx = int(raw)-1
                            b = binds[idx]
                        except Exception:
                            message = "invalid index"
                        else:
                            print(f" editing {b['key']} — press new key or Enter keeps")
                            cap = _capture_key_cli()
                            nk = (cap or input(f"  key [{b['key']}] > ").strip().lower() or b['key'])
                            if nk != b['key'] and any(x.get("key")==nk for x in binds):
                                message = f"duplicate key {nk!r}"
                            else:
                                try:
                                    _key_code(nk)
                                except SystemExit as e:
                                    message = str(e)
                                else:
                                    b["tap"] = _pick_action_cli(f"tap (was {b.get('tap','') or 'off'}) —", b.get('tap',''))
                                    b["hold"] = _pick_action_cli(f"hold (was {b.get('hold','') or 'off'}) —", b.get('hold',''))
                                    b["toggle"] = _pick_action_cli(f"toggle (was {b.get('toggle','') or 'off'}) —", b.get('toggle',''))
                                    thr_raw = input(f"  threshold [{b.get('hold_threshold',0.25):g}] > ").strip()
                                    if thr_raw:
                                        try:
                                            b["hold_threshold"] = float(thr_raw)
                                        except Exception:
                                            message = "bad threshold, kept old"
                                    b["key"] = nk
                                    if "bad threshold" not in (message or ""):
                                        message = f"updated {nk}"
                elif sub in ("d", "3"):
                    if not binds:
                        message = "no binds"
                    else:
                        for i,b in enumerate(binds, start=1):
                            print(f"  {i}) {_key_label(b['key']):12} {b['key']}")
                        raw = input(" remove which > ").strip()
                        try:
                            idx = int(raw)-1
                            b = binds.pop(idx)
                            message = f"removed {b['key']}"
                        except Exception:
                            message = "invalid index"
                else:
                    if sub:
                        print(f"  unknown: {sub}")
        elif choice == "s":
            _save_config_file(values)
            message = f"saved to {DEFAULT_CONFIG_PATH} — restart the daemon to apply (r)"
        elif choice == "t":
            message = "STT health: " + _health_check(values["whisper_health_url"])
        elif choice == "p":
            print("\n" + _COMPOSITOR_SNIPPETS)
            input(" [press Enter to return] ")
        elif choice == "r":
            message = _restart_daemon()
        elif choice.isdigit():
            i = int(choice) - 1
            if 0 <= i < len(_SETUP_FIELDS):
                key, label, conv, _section = _SETUP_FIELDS[i]
                cur = _fmt_value(values[key])
                if conv is bool:
                    ans = input(f" {label} [{cur}] (y/n, Enter=keep): ").strip().lower()
                    if ans in ("y", "yes", "on", "1"):
                        values[key] = True
                        message = f"{key} = yes"
                    elif ans in ("n", "no", "off", "0"):
                        values[key] = False
                        message = f"{key} = no"
                    else:
                        message = f"{key} unchanged"
                else:
                    raw = input(f" {label} [{cur}] > ").strip()
                    if not raw:
                        message = f"{key} unchanged"
                    else:
                        try:
                            values[key] = _parse_value(raw, conv)
                            message = f"{key} = {_fmt_value(values[key])}"
                        except ValueError:
                            message = "invalid value, not saved"
            else:
                message = "no such field"
        else:
            message = f"unknown command: {choice}"


def _tui_main() -> int:
    """Full-screen curses editor for _SETUP_FIELDS (alternative to `setup`).

    ↑/↓ navigate · Enter edit · Space/y/n toggle bools · s save ·
    t test STT · p compositor binds · r restart daemon · q quit.
    Falls back to `shipboard setup` when curses is unavailable.
    """
    from .cli import _restart_daemon  # deferred: cli imports this module at load time (cycle)
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
    show_advanced = False  # TUI also collapses Advanced by default

    def _build_rows():
        rows_ = []
        # Keys card as a navigable row (not a SETUP_FIELD)
        rows_.append(("keys", None))
        last_sec = None
        for i, (key, label, conv, section) in enumerate(_SETUP_FIELDS):
            if section == "Advanced" and not show_advanced:
                if last_sec != "Advanced":
                    rows_.append(("section", "… Advanced  [a] show/hide"))
                    last_sec = "Advanced"
                continue
            if section != last_sec:
                rows_.append(("section", section))
                last_sec = section
            rows_.append(("field", i))
        return rows_

    rows = _build_rows()

    def _next_field(sel: int, step: int) -> int:
        i = sel + step
        while 0 <= i < len(rows) and rows[i][0] not in ("field", "keys"):
            i += step
        return i if 0 <= i < len(rows) else sel

    def _keys_summary() -> str:
        binds = values.get("_key_binds") or []
        if not binds:
            return "Keys: (none yet — press k to add)"
        parts = []
        for b in binds:
            tap = b.get("tap",""); hold = b.get("hold",""); tog = b.get("toggle",""); thr = b.get("hold_threshold",0.25)
            segs = []
            if tog: segs.append(f"toggle={tog}")
            if hold: segs.append(f"hold {hold}@{thr:g}s")
            if tap: segs.append(f"tap {tap}")
            label = _key_label(b.get("key",""))
            parts.append(f"{label}: {' · '.join(segs) if segs else '(off)'}")
        return "Keys: " + "  |  ".join(parts)

    def _prompt_line(stdscr_, prompt, default=""):
        h_, w_ = stdscr_.getmaxyx()
        try:
            stdscr_.move(h_-1, 0); stdscr_.clrtoeol()
            stdscr_.addnstr(h_-1, 0, prompt + (f" [{default}]" if default else "") + " ", w_-1)
            stdscr_.noutrefresh(); curses.doupdate()
        except curses.error:
            pass
        curses.echo()
        try: curses.curs_set(1)
        except curses.error: pass
        try:
            raw = stdscr_.getstr(h_-1, len(prompt)+2 + (len(default)+3 if default else 1), 50)
            s = raw.decode(errors="replace").strip()
        except curses.error:
            s = ""
        finally:
            curses.noecho()
            try: curses.curs_set(0)
            except curses.error: pass
        return s if s else default

    def _capture_key(stdscr_) -> str | None:
        # Wait for next evdev press and return its key name; fallback to curses if no device
        hint = "Press the key to bind  (Esc cancels)…"
        h_, w_ = stdscr_.getmaxyx()
        try:
            stdscr_.move(h_-1, 0); stdscr_.clrtoeol()
            stdscr_.addnstr(h_-1, 0, hint, w_-1)
            stdscr_.noutrefresh(); curses.doupdate()
        except curses.error:
            pass
        try:
            import evdev
            import select
            wanted = set()
            devs = []
            for path in evdev.list_devices():
                try:
                    d = evdev.InputDevice(path)
                    caps = d.capabilities(verbose=False)
                    keys = caps.get(evdev.ecodes.EV_KEY, [])
                    if keys:
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
                                    # fallback: scan KEY_* attributes
                                    for attr in dir(evdev.ecodes):
                                        if attr.startswith("KEY_") and getattr(evdev.ecodes, attr) == ev.code:
                                            raw = attr
                                            break
                                name = raw[4:].lower() if raw and raw.startswith("KEY_") else str(ev.code)
                                return name
                    except Exception:
                        pass
                # also poll curses for Esc
                try:
                    stdscr_.nodelay(True)
                    ch = stdscr_.getch()
                    stdscr_.nodelay(False)
                    if ch == 27:
                        return None
                    if ch != -1:
                        stdscr_.nodelay(False)
                        # ignore
                        pass
                except curses.error:
                    pass
                try:
                    stdscr_.nodelay(False)
                except curses.error:
                    pass
            stdscr_.nodelay(False)
            return None
        except Exception:
            try:
                stdscr_.nodelay(False)
            except curses.error:
                pass
            # fallback: ask via curses key name
            try:
                stdscr_.move(h_-1, 0); stdscr_.clrtoeol()
                stdscr_.addnstr(h_-1, 0, "evdev unavailable — type key name: ", w_-1)
                stdscr_.noutrefresh(); curses.doupdate()
            except curses.error:
                pass
            return None

    def _choose_action(stdscr_, prompt, current=""):
        items = [("record", "copy only"), ("record_send", "copy+paste+Enter"), ("paste", "paste clipboard"), ("", "off")]
        sel = 0
        for i,(v,_) in enumerate(items):
            if v == current:
                sel = i; break
        while True:
            h_, w_ = stdscr_.getmaxyx()
            try:
                stdscr_.move(h_-1, 0); stdscr_.clrtoeol()
                line = prompt + "  "
                for i,(v,lbl) in enumerate(items):
                    tag = f"[{v or 'off'}:{lbl}]"
                    if i == sel:
                        tag = f"[{v or 'off'}]"
                    line += ("▶" if i==sel else " ") + tag + " "
                stdscr_.addnstr(h_-1, 0, line[:w-1], w_-1)
                stdscr_.addnstr(h_-1, max(0,w_-28), " ↑↓ choose · Enter ok · Esc cancel", 28)
                stdscr_.noutrefresh(); curses.doupdate()
            except curses.error:
                pass
            ch = stdscr_.getch()
            if ch in (curses.KEY_UP, curses.KEY_LEFT):
                sel = (sel-1) % len(items)
            elif ch in (curses.KEY_DOWN, curses.KEY_RIGHT, 9):  # Tab
                sel = (sel+1) % len(items)
            elif ch in (10,13,curses.KEY_ENTER, ord(" ")):
                return items[sel][0]
            elif ch == 27:
                return current

    def _edit_keys_flow(stdscr_) -> None:
        """Keys page: list of bindings; Enter opens a per-bind editor where each
        option (key / tap / hold / hold-delay / toggle) is its own line."""
        binds = values.setdefault("_key_binds", [])

        def _edit_bind(stdscr__, bind: dict) -> None:
            """Per-bind editor — one option per line, each edited separately."""
            rows = [
                ("Key",             "key"),
                ("Tap (short)",     "tap"),
                ("Hold (long)",     "hold"),
                ("Hold delay (s)",  "hold_threshold"),
                ("Toggle",          "toggle"),
            ]
            sel = 0
            while True:
                h___, w___ = stdscr__.getmaxyx()
                try:
                    stdscr__.erase()
                    title = f" Bind: {_key_label(bind['key'])} "
                    stdscr__.addnstr(0, 0, title + "─" * max(0, w___ - len(title)),
                                     w___ - 1, curses.A_REVERSE)
                    for i, (label, field) in enumerate(rows):
                        if field == "key":
                            cur = _key_label(bind["key"])
                            hint = "press key to re-bind"
                        elif field == "hold_threshold":
                            cur = f"{bind.get('hold_threshold', 0.25):g}s"
                            hint = "seconds 0–5"
                        else:
                            cur = bind.get(field, "") or "off"
                            hint = "record / record_send / paste / off"
                        line = f" {i+1}. {label:<18} {cur:<14}  [{hint}]"
                        attrs = curses.A_REVERSE if i == sel else 0
                        stdscr__.addnstr(2 + i, 1, line[:w___ - 2], w___ - 2, attrs)
                    note = "tap = short press  ·  hold = long press  ·  toggle = press to start/stop (overrides tap)"
                    try:
                        stdscr__.addnstr(2 + len(rows) + 1, 1, note, w___ - 2, curses.A_DIM)
                    except curses.error:
                        pass
                    hint = " ↑/↓ move · Enter edit line · Esc back"
                    stdscr__.addnstr(h___ - 1, 0, hint, w___ - 1)
                    stdscr__.noutrefresh()
                    curses.doupdate()
                except curses.error:
                    pass
                ch = stdscr__.getch()
                if ch in (27, ord("q"), ord("Q")):
                    return
                elif ch == curses.KEY_UP:
                    sel = (sel - 1) % len(rows)
                elif ch == curses.KEY_DOWN:
                    sel = (sel + 1) % len(rows)
                elif ch in (10, 13, curses.KEY_ENTER, ord(" ")):
                    field = rows[sel][1]
                    if field == "key":
                        rec = _capture_key(stdscr__)
                        if rec:
                            if any(x is not bind and x.get("key") == rec for x in binds):
                                continue
                            try:
                                _key_code(rec)
                                bind["key"] = rec
                            except SystemExit:
                                pass
                        else:
                            nk = _prompt_line(stdscr__, "key name", bind["key"]).strip().lower() or bind["key"]
                            if nk != bind["key"] and any(x is not bind and x.get("key") == nk for x in binds):
                                continue
                            try:
                                _key_code(nk)
                                bind["key"] = nk
                            except SystemExit:
                                pass
                    elif field == "hold_threshold":
                        raw = _prompt_line(stdscr__, "hold delay seconds (0–5)",
                                           f"{bind.get('hold_threshold', 0.25):g}").strip()
                        try:
                            v = float(raw)
                            assert 0 < v <= 5
                            bind["hold_threshold"] = v
                        except Exception:
                            pass
                    else:  # tap / hold / toggle
                        bind[field] = _choose_action(stdscr__, f"{rows[sel][1]}:",
                                                     bind.get(field, "") or "")

        def _render_keys_page(sel=0):
            stdscr_.erase()
            h__, w__ = stdscr_.getmaxyx()
            try:
                stdscr_.addnstr(0, 0, " Keys " + "─" * max(0, w__ - 6), w__ - 1,
                                curses.A_REVERSE)
            except curses.error:
                pass
            if not binds:
                try:
                    stdscr_.addnstr(2, 2, "(no bindings yet — press a to add, then PRESS THE KEY)", w__ - 4)
                except curses.error:
                    pass
                y0 = 4
            else:
                y0 = 2
                for i, b in enumerate(binds):
                    tap = b.get("tap", "") or "off"
                    hold = b.get("hold", "") or "off"
                    tog = b.get("toggle", "") or "off"
                    thr = b.get("hold_threshold", 0.25)
                    segs = []
                    if tog:
                        segs.append(f"toggle={tog}")
                    if hold:
                        segs.append(f"hold={hold}@{thr:g}s")
                    if tap:
                        segs.append(f"tap={tap}")
                    if not segs:
                        segs.append("(all off)")
                    line = f" {i+1}. {_key_label(b['key']):16}  {' · '.join(segs)}"
                    attrs = curses.A_REVERSE if i == sel else 0
                    try:
                        stdscr_.addnstr(y0 + i, 1, line[:w__ - 2], w__ - 2, attrs)
                    except curses.error:
                        pass
                try:
                    stdscr_.addnstr(y0 + len(binds) + 1, 2,
                                    "toggle overrides tap; hold+tap / hold+toggle are idempotent — safe to combine",
                                    w__ - 4, curses.A_DIM)
                except curses.error:
                    pass
            hint = " a add (press key) · Enter edit · d delete · Esc/q back"
            try:
                stdscr_.addnstr(h__ - 1, 0, hint, w__ - 1)
            except curses.error:
                pass
            stdscr_.noutrefresh()
            curses.doupdate()

        sel = 0
        _render_keys_page(sel)
        while True:
            ch = stdscr_.getch()
            if ch in (27, ord("q"), ord("Q")):
                return
            elif ch == curses.KEY_UP:
                if binds:
                    sel = (sel - 1) % len(binds)
                    _render_keys_page(sel)
            elif ch == curses.KEY_DOWN:
                if binds:
                    sel = (sel + 1) % len(binds)
                    _render_keys_page(sel)
            elif ch in (ord("a"), ord("A")):
                if len(binds) >= 3:
                    continue
                name = _capture_key(stdscr_)
                if not name:
                    name = _prompt_line(stdscr_, "key name (e.g. rightalt)", "").strip().lower()
                    if not name:
                        _render_keys_page(sel)
                        continue
                try:
                    _key_code(name)
                except SystemExit:
                    _render_keys_page(sel)
                    continue
                if any(b.get("key") == name for b in binds):
                    _render_keys_page(sel)
                    continue
                bind = {"key": name, "tap": "", "hold": "", "toggle": "",
                        "hold_threshold": 0.25}
                binds.append(bind)
                sel = len(binds) - 1
                _edit_bind(stdscr_, bind)  # straight into the per-option editor
                _render_keys_page(sel)
            elif ch in (10, 13, curses.KEY_ENTER, ord(" "), ord("e"), ord("E")) and binds:
                _edit_bind(stdscr_, binds[sel])
                _render_keys_page(sel)
            elif ch in (ord("d"), ord("x"), curses.KEY_DC, 127) and binds:
                binds.pop(sel)
                if sel >= len(binds) and sel > 0:
                    sel -= 1
                _render_keys_page(sel)

    def _show_binds(stdscr) -> None:
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

    def _edit_field(stdscr, sel: int, message: str) -> str:
        field = rows[sel][1]
        key, label, conv, _section = _SETUP_FIELDS[field]
        h, w = stdscr.getmaxyx()
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        if conv is bool:
            prompt = f" {label} [{_fmt_value(values[key])}]   space/y = yes · n = no · Esc = keep"
            try:
                stdscr.move(h - 1, 0)
                stdscr.clrtoeol()  # clear the navigation hint line first
                stdscr.addnstr(h - 1, 0, prompt, w - 1)
                stdscr.noutrefresh()
                curses.doupdate()
            except curses.error:
                pass
            while True:
                ch = stdscr.getch()
                if ch in (27,):
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
                prompt = f" {label} [{buf}]"
                try:
                    stdscr.move(h - 1, 0)
                    stdscr.clrtoeol()  # clear the navigation hint line first
                    stdscr.addnstr(h - 1, 0, prompt, w - 1)
                    stdscr.noutrefresh()
                    curses.doupdate()
                except curses.error:
                    pass
                ch = stdscr.getch()
                if ch in (27,):
                    break
                if ch in (10, 13, curses.KEY_ENTER):
                    try:
                        values[key] = _parse_value(buf, conv)
                        message = f"{key} = {_fmt_value(values[key])}"
                    except ValueError:
                        message = "invalid value, not saved"
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

    def run(stdscr) -> int:
        nonlocal rows, show_advanced
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        sel, scroll = 0, 0
        while sel < len(rows) and rows[sel][0] not in ("field", "keys"):
            sel += 1
        message = "↑/↓ move · Enter/k edit keys · a advanced · s save · t test · p compositor · r restart · q quit"
        while True:
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            positions = []
            y = 1
            for kind, payload in rows:
                positions.append((kind, payload, y))
                y += 1
            sel_y = positions[sel][2]
            if sel_y - scroll < 1:
                scroll = sel_y - 1
            elif sel_y - scroll > h - 3:
                scroll = sel_y - (h - 3)
            title = f" shipboard setup — {DEFAULT_CONFIG_PATH}  ({'on' if _daemon_running() else 'off'}) "
            try:
                stdscr.addnstr(0, 0, title, w - 1, curses.A_REVERSE)
            except curses.error:
                pass
            for kind, payload, py in positions:
                yy = py - scroll
                if yy < 1 or yy >= h - 1:
                    continue
                try:
                    if kind == "section":
                        stdscr.addnstr(yy, 0, f"  == {payload} ==", w - 1,
                                       curses.A_BOLD)
                    elif kind == "keys":
                        selected = rows[sel][0] == "keys"
                        attrs = curses.A_REVERSE if selected else curses.A_BOLD
                        stdscr.addnstr(yy, 0, f"  {_keys_summary()}", w - 1, attrs)
                    else:
                        key, label, conv, _section = _SETUP_FIELDS[payload]
                        mark = "*" if key in _CFG else " "
                        selected = kind == "field" and payload == rows[sel][1]
                        attrs = curses.A_REVERSE if selected else 0
                        stdscr.addnstr(yy, 0,
                                       f" {mark} {label:<46} {_fmt_value(values[key])}",
                                       w - 1, attrs)
                except curses.error:
                    pass
            try:
                stdscr.addnstr(h - 1, 0, message, w - 1)
            except curses.error:
                pass
            stdscr.noutrefresh()
            curses.doupdate()
            ch = stdscr.getch()
            if ch in (curses.KEY_UP,):
                sel = _next_field(sel, -1)
            elif ch in (curses.KEY_DOWN,):
                sel = _next_field(sel, 1)
            elif ch in (curses.KEY_RESIZE,):
                continue
            elif ch in (10, 13, curses.KEY_ENTER):
                if rows[sel][0] == "keys":
                    _edit_keys_flow(stdscr)
                    message = "keys: " + _keys_summary()
                else:
                    message = _edit_field(stdscr, sel, message)
            elif ch == ord("k"):
                _edit_keys_flow(stdscr)
                message = "keys: " + _keys_summary()
            elif ch == ord("a"):
                show_advanced = not show_advanced
                rows = _build_rows()
                sel = 0
                while sel < len(rows) and rows[sel][0] not in ("field", "keys"):
                    sel += 1
                message = "advanced shown" if show_advanced else "advanced hidden"
            elif ch == ord("s"):
                _save_config_file(values)
                message = "saved to ~/.config/shipboard/shipboard.toml - restart daemon to apply (r)"
            elif ch == ord("t"):
                message = "STT health: " + _health_check(values["whisper_health_url"])
            elif ch == ord("p"):
                _show_binds(stdscr)
            elif ch == ord("r"):
                message = _restart_daemon()
            elif ch in (ord("q"), ord("Q")):
                return 0
        return 0

    try:
        return curses.wrapper(run)
    except KeyboardInterrupt:
        return 0
