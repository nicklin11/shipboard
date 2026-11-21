#!/usr/bin/env python3
"""Keyboards that appear after startup are picked up; probes don't leak (#25)."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import evdev  # noqa: E402

from shipboard import daemon as sbd  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


EV_KEY = evdev.ecodes.EV_KEY
sbd.KEY_BINDINGS = [{"key": "pause", "tap": "record"}]  # hermetic: no user config
WANTED = sbd._key_code("pause")
present = {}  # path -> list of key codes; mutated to simulate plug/unplug
opened = []


class FakeDev:
    def __init__(self, path):
        if path not in present:
            raise OSError("gone")
        self.path, self.name, self.closed = path, f"dev{path[-1]}", False
        opened.append(self)

    def capabilities(self, verbose=False):
        return {EV_KEY: present[self.path]}

    def close(self):
        self.closed = True


evdev.list_devices = lambda: sorted(present)
evdev.InputDevice = FakeDev

present["/dev/input/event1"] = [WANTED]
present["/dev/input/event2"] = [30]  # a mouse-ish device without the bound key
first = sbd._watch_devices()
check("initial scan opens only the matching device",
      [d.path for d in first] == ["/dev/input/event1"])
check("non-matching probe was closed", [d.closed for d in opened if d.path.endswith("2")] == [True])

known = {d.path for d in first}
check("rescan with nothing new returns nothing and opens nothing",
      sbd._watch_devices(exclude=known) == [] and len([d for d in opened if d.path.endswith("1")]) == 1)

present["/dev/input/event3"] = [WANTED]  # second keyboard hot-plugged
fresh = sbd._watch_devices(exclude=known)
check("hot-plugged keyboard is found", [d.path for d in fresh] == ["/dev/input/event3"])

del present["/dev/input/event1"]  # unplug + replug under a new node
present["/dev/input/event4"] = [WANTED]
fresh = sbd._watch_devices(exclude={"/dev/input/event3"})
check("re-enumerated keyboard is found while another stays open",
      [d.path for d in fresh] == ["/dev/input/event4"])

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all hotplug checks passed")
