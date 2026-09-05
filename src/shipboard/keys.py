"""evdev key-name helpers: key code resolution + human-readable labels."""

from __future__ import annotations

_KEY_CODE_CACHE: dict[str, int] = {}


def _key_code(name: str) -> int:
    """evdev key name ('pause', 'scrolllock', 'f13', ...) -> input code."""
    code = _KEY_CODE_CACHE.get(name)
    if code is not None:
        return code
    import evdev

    try:
        code = int(getattr(evdev.ecodes, f"KEY_{name.upper()}"))
    except AttributeError as exc:
        raise SystemExit(f"shipboard: unknown key name {name!r}") from exc
    _KEY_CODE_CACHE[name] = code
    return code


_KEY_LABELS = {
    "pause": "Pause", "scrolllock": "Scroll Lock", "insert": "Insert",
    "home": "Home", "end": "End", "pageup": "Page Up", "pagedown": "Page Down",
    "delete": "Delete", "f13": "F13", "f14": "F14", "f15": "F15",
    "f16": "F16", "f17": "F17", "f18": "F18", "f19": "F19", "f20": "F20",
}


def _key_label(name: str) -> str:
    if not name:
        return "off"
    return _KEY_LABELS.get(name, name.replace("_", " ").title())
