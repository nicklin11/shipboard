#!/usr/bin/env python3
"""shipboard key relay: hotkeys without putting the user in the `input` group.

Being in `input` lets every program you run read everything you type. This relay
runs as a sandboxed system service instead: it reads the keyboards, and re-emits
ONLY the configured trigger keys (press/release) on a small virtual keyboard named
"shipboard-keys". A udev rule lets the logged-in user read that device and tells
libinput (your compositor) to ignore it, so the desktop gets no doubled keys. The
shipboard daemon finds the device on its own: it already scans for any readable
device that has a bound key.

No key is ever logged. Pure standard library: evdev and uinput via ioctls.

    shipboard-key-relay.py --keys rightalt[,pause,...]   (evdev names or numeric codes)
"""
import argparse
import fcntl
import glob
import os
import re
import select
import struct
import sys
import time

NAME = b"shipboard-keys"
EV_SYN, EV_KEY, SYN_REPORT = 0, 1, 0
KEY_MAX = 0x2FF
EVENT = struct.Struct("llHHi")  # struct input_event, 64-bit
RESCAN_S = 2.0
HEADER = "/usr/include/linux/input-event-codes.h"
FALLBACK = {"rightalt": 100, "rightctrl": 97, "pause": 119, "scrolllock": 70, "sysrq": 99,
            "insert": 110, "compose": 127, "menu": 139, "capslock": 58,
            **{f"f{n}": 183 + n - 13 for n in range(13, 25)}}


def _ioc(direction, kind, nr, size):
    return (direction << 30) | (size << 16) | (ord(kind) << 8) | nr


def EVIOCGBIT(ev, length):
    return _ioc(2, "E", 0x20 + ev, length)


def EVIOCGNAME(length):
    return _ioc(2, "E", 0x06, length)


UI_SET_EVBIT = _ioc(1, "U", 100, 4)
UI_SET_KEYBIT = _ioc(1, "U", 101, 4)
UI_DEV_SETUP = _ioc(1, "U", 3, 92)  # struct uinput_setup
UI_DEV_CREATE = _ioc(0, "U", 1, 0)
UI_DEV_DESTROY = _ioc(0, "U", 2, 0)


def key_codes(spec: str) -> list[int]:
    """'rightalt,pause' or '100,119' -> codes, names as shipboard writes them."""
    table = {}
    try:
        with open(HEADER) as f:
            for m in re.finditer(r"#define\s+KEY_(\w+)\s+(0x[0-9a-fA-F]+|\d+)", f.read()):
                table[m.group(1).lower()] = int(m.group(2), 0)
    except OSError:
        table = dict(FALLBACK)
    codes = []
    for item in filter(None, (s.strip().lower() for s in spec.split(","))):
        item = item[4:] if item.startswith("key_") else item
        code = int(item) if item.isdigit() else table.get(item)
        if code is None or not 0 < code <= KEY_MAX:
            raise SystemExit(f"unknown key: {item!r}")
        codes.append(code)
    if not codes:
        raise SystemExit("no keys given")
    return sorted(set(codes))


class Merger:
    """One press/release per key, however many keyboards report it.

    A key is down while any source holds it; autorepeat (value 2) and every other
    key are dropped."""

    def __init__(self, keys):
        self.held = {k: set() for k in keys}

    def feed(self, src, code, value):
        holders = self.held.get(code)
        if holders is None or value == 2:
            return []
        was_down = bool(holders)
        (holders.add if value else holders.discard)(src)
        return [(code, 1 if holders else 0)] if bool(holders) != was_down else []

    def drop(self, src):
        """A source went away: release whatever only it was holding."""
        out = []
        for code, holders in self.held.items():
            if src in holders:
                holders.discard(src)
                if not holders:
                    out.append((code, 0))
        return out


def device_name(fd):
    buf = bytearray(256)
    try:
        fcntl.ioctl(fd, EVIOCGNAME(len(buf)), buf)
    except OSError:
        return b""
    return bytes(buf).split(b"\0", 1)[0]


def has_any(fd, keys):
    bits = bytearray((KEY_MAX + 8) // 8)
    try:
        fcntl.ioctl(fd, EVIOCGBIT(EV_KEY, len(bits)), bits)
    except OSError:
        return False
    return any(bits[k // 8] & (1 << (k % 8)) for k in keys)


def make_output(keys):
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
    for k in keys:
        fcntl.ioctl(fd, UI_SET_KEYBIT, k)
    # struct uinput_setup { struct input_id {bustype, vendor, product, version}; char name[80]; u32 ff }
    fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack("HHHH80sI", 0x06, 0x5348, 0x0001, 1, NAME, 0))
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def emit(out, events):
    if events:
        os.write(out, b"".join(EVENT.pack(0, 0, EV_KEY, c, v) for c, v in events)
                 + EVENT.pack(0, 0, EV_SYN, SYN_REPORT, 0))


def run(keys):
    out = make_output(keys)
    merger = Merger(keys)
    print(f"virtual device {NAME.decode()} created for key codes {keys}", flush=True)
    sources, last_scan = {}, 0.0
    try:
        while True:
            if time.monotonic() - last_scan >= RESCAN_S:
                last_scan = time.monotonic()
                for path in sorted(glob.glob("/dev/input/event*")):
                    if path in sources:
                        continue
                    try:
                        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    except OSError:
                        continue
                    if device_name(fd) == NAME or not has_any(fd, keys):
                        os.close(fd)
                        continue
                    sources[path] = fd
                    print(f"listening: {path} ({device_name(fd).decode(errors='replace').strip()})", flush=True)
            if not sources:
                time.sleep(RESCAN_S)
                continue
            ready = select.select(list(sources.values()), [], [], RESCAN_S)[0]
            for path, fd in list(sources.items()):
                if fd not in ready:
                    continue
                try:
                    data = os.read(fd, EVENT.size * 64)
                except BlockingIOError:
                    continue
                except OSError:  # unplugged
                    os.close(fd)
                    del sources[path]
                    emit(out, merger.drop(path))
                    print(f"gone: {path}", flush=True)
                    continue
                events = []
                for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, typ, code, value = EVENT.unpack_from(data, i)
                    if typ == EV_KEY:
                        events += merger.feed(path, code, value)
                emit(out, events)
    finally:
        try:
            fcntl.ioctl(out, UI_DEV_DESTROY)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="shipboard key relay: re-emit only the trigger keys")
    ap.add_argument("--keys", default="rightalt", help="comma list of evdev key names or codes")
    ap.add_argument("--check", action="store_true", help="only resolve --keys, print the codes, exit")
    args = ap.parse_args(argv)
    keys = key_codes(args.keys)
    if args.check:
        print("key codes:", ",".join(map(str, keys)))
        return 0
    run(keys)


if __name__ == "__main__":
    sys.exit(main())
