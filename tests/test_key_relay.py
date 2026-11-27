#!/usr/bin/env python3
"""contrib/key-relay: only the trigger keys come out, once, and never stuck."""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "key_relay", REPO / "contrib/key-relay/shipboard-key-relay.py")
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


RALT, PAUSE, A = 100, 119, 30

# key names resolve like shipboard writes them (with or without KEY_, any case, or numbers)
check("names and codes resolve", relay.key_codes("rightalt,KEY_PAUSE,30") == [A, RALT, PAUSE])
relay.HEADER = "/nonexistent/input-event-codes.h"  # no kernel headers installed
check("fallback table without headers", relay.key_codes("rightalt,f13") == [RALT, 183])
for bad in ("nosuchkey", "", "0", "99999"):
    try:
        relay.key_codes(bad)
        check(f"rejects {bad!r}", False)
    except SystemExit:
        check(f"rejects {bad!r}", True)

m = relay.Merger([RALT])
check("other keys are dropped", m.feed("kbd", A, 1) == [] and m.feed("kbd", A, 0) == [])
check("press passes", m.feed("kbd", RALT, 1) == [(RALT, 1)])
check("autorepeat is dropped", m.feed("kbd", RALT, 2) == [])
check("release passes", m.feed("kbd", RALT, 0) == [(RALT, 0)])
check("stray release is dropped", m.feed("kbd", RALT, 0) == [])

# the same key held on two keyboards (e.g. a real one and keyd's virtual one) is one press
m = relay.Merger([RALT])
out = m.feed("kbd1", RALT, 1) + m.feed("kbd2", RALT, 1) + m.feed("kbd1", RALT, 0)
check("overlapping holds give one press, no early release", out == [(RALT, 1)])
check("last release comes out", m.feed("kbd2", RALT, 0) == [(RALT, 0)])

# a keyboard unplugged mid-hold must not leave the key stuck down
m = relay.Merger([RALT, PAUSE])
m.feed("kbd", RALT, 1)
m.feed("kbd", PAUSE, 1)
check("unplug releases held keys", sorted(m.drop("kbd")) == [(RALT, 0), (PAUSE, 0)])
check("unplug of an idle keyboard emits nothing", m.drop("other") == [])

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all key relay checks passed")
