#!/usr/bin/env python3
"""normalize_text keeps prose intact and still builds command symbols (#15)."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from shipboard.actions import normalize_text as n  # noqa: E402

failures = []


def check(text, want):
    got = n(text)
    ok = got == want
    print(("PASS  " if ok else "FAIL  ") + repr(text) + ("" if ok else f" -> {got!r}, want {want!r}"))
    if not ok:
        failures.append(text)


# prose must come through untouched (the reported corruptions)
check("Meet me at five, the dot product is zero.", "Meet me at five, the dot product is zero.")
check("Note: this is a test - really.", "Note: this is a test - really.")
check("Какой у тебя вопрос? Моя точка зрения такая, плюс минус пять.",
      "Какой у тебя вопрос? Моя точка зрения такая, плюс минус пять.")
check("1 + 1 = 2 and star wars", "1 + 1 = 2 and star wars")
check("Привет. Это тест", "Привет. Это тест")
check("Done. and then more", "Done. and then more")
check("use the --verbose flag", "use the --verbose flag")

# command dictation still works
check("тильда слэш точка config слэш shipboard", "~/.config/shipboard")
check("open slash etc slash hosts", "open/etc/hosts")
check("дэш дэш config равно слэш tmp слэш x", "--config=/tmp/x")
check("configdefizfile", "config-file")
check("a запятая b", "a, b")
check("a\nслэш\nb", "a/b")
check("сл эш tmp", "/tmp")

print()
if failures:
    print(f"{len(failures)} FAILED")
    raise SystemExit(1)
print("all normalizer checks passed")
