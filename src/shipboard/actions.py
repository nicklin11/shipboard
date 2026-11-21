"""Platform-facing glue: notify, key injection, clipboard, text
normalization (all delegating to platform_adapters where applicable)."""

from __future__ import annotations

import re

from . import platform_adapters as _plat
from .config import DRY_RUN, NORMALIZE, PASTE_COMBO, SEND_ENTER

def _notify(title: str, msg: str) -> None:
    _plat.notify(title, msg)


_SYMBOL_MAP = {
    # Russian
    "слэш": "/", "слеш": "/", "точка": ".", "тильда": "~",
    "дефис": "-", "минус": "-", "запятая": ",", "точка с запятой": ";",
    "двоеточие": ":", "равно": "=", "амперсанд": "&", "процент": "%",
    "собака": "@", "нижнее подчёркивание": "_", "подчёркивание": "_",
    "звёздочка": "*", "решётка": "#", "решетка": "#", "плюс": "+",
    "вопрос": "?", "восклицание": "!", "кавычка": '"',
    "открывающая скобка": "(", "закрывающая скобка": ")",
    "апостроф": "'", "пробел": " ",
    # dash variants: whisper often transliterates these ("desh"/"defiz"/"tire")
    "дэш": "-", "деш": "-", "desh": "-", "defiz": "-",
    "тире": "-", "tire": "-", "tireh": "-",
    "слэж": "/",
    # English
    "slash": "/", "dot": ".", "tilde": "~", "tilda": "~", "dash": "-", "hyphen": "-",
    "comma": ",", "colon": ":", "semicolon": ";", "equals": "=",
    "equal": "=", "ampersand": "&", "percent": "%", "at": "@",
    "underscore": "_", "asterisk": "*", "star": "*", "hash": "#",
    "question mark": "?", "exclamation mark": "!", "exclamation": "!",
    "plus": "+", "minus": "-", "quote": '"', "apostrophe": "'",
    "open paren": "(", "close paren": ")", "space": " ",
}
_TOKEN_RE = re.compile(
    r"\b(" + "|".join(
        re.escape(tok) for tok in sorted(_SYMBOL_MAP, key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
)
# whisper sometimes glues the spoken word to its neighbor ("deshtag",
# "configdefizfile") — replace the token even as a word prefix then.
_GLUE_DASH_RE = re.compile(
    r"(?i)(?:дэш|деш|desh|defiz|тире|tire|tireh)(?=[a-zа-яё0-9])"
)


# Words that are ordinary vocabulary as often as they are dictated symbols
# ("meet me at five", "the dot product", "Моя точка зрения", "плюс минус пять").
# They become symbols only when touching an unambiguous symbol word or another
# accepted symbol ("тильда слэш точка config" -> "~/.config"); on their own in
# a sentence they are left as words.
_AMBIGUOUS = {
    "at", "dot", "star", "space", "quote", "plus", "minus", "equal", "equals",
    "hash", "percent", "точка", "вопрос", "плюс", "минус", "равно", "собака",
    "пробел", "процент", "кавычка", "решётка", "решетка",
}

# Substituted symbols are wrapped in this private-use sentinel so the spacing
# rules below act only on symbols that came from a spoken name, never on
# punctuation the model already wrote ("Note: this is a test - really.").
_S = "\ue000"
_GLUE_SYMBOLS = "~/_@#*+=:"


def _substitute(text: str) -> str:
    matches = list(_TOKEN_RE.finditer(text))
    accepted = [m.group(1).lower() not in _AMBIGUOUS for m in matches]
    changed = True
    while changed:
        changed = False
        for i, m in enumerate(matches):
            if accepted[i]:
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < len(matches) and accepted[j]:
                    first, second = (matches[j], m) if j < i else (m, matches[j])
                    if not text[first.end():second.start()].strip():
                        accepted[i] = changed = True
                        break
    out, pos = [], 0
    for m, ok in zip(matches, accepted):
        out.append(text[pos:m.start()])
        out.append(
            f"{_S}{_SYMBOL_MAP[m.group(1).lower()]}{_S}" if ok else m.group(0)
        )
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


_DASH_CLUSTER_RE = re.compile(rf"\s*(?:-\s*)*(?:{_S}-{_S}(?:\s*-)*\s*)+")


def normalize_text(text: str) -> str:
    """Spoken punctuation names -> symbols; command symbols glue, sentence
    punctuation keeps normal spacing (\"Привет. Это\" vs \"~/.config\").

    Only symbols produced from spoken names are re-spaced; punctuation already
    present in the transcript is left alone, and ambiguous words ("at", "dot",
    "точка", ...) are substituted only next to another symbol word."""
    if not NORMALIZE or not text:
        return text
    # whisper returns segments joined with newlines (VAD cuts the speech);
    # the result must be a single line
    text = re.sub(r"\s+", " ", text)
    # whisper sometimes splits a spoken word ("сл эш" for "слэш") —
    # stitch the known splits back together
    text = text.replace("сл эш", "слэш")
    text = _substitute(text)
    text = _GLUE_DASH_RE.sub(f"{_S}-{_S}", text)
    # the model often writes hyphens around the spoken word; fold them in and
    # collapse runs of 3+ (keep "--" — legitimate flag prefix)
    text = _DASH_CLUSTER_RE.sub(
        lambda m: "-" * min(m.group(0).replace(_S, "").count("-"), 2), text
    )
    # command symbols (paths, flags, URLs): never surrounded by spaces
    text = re.sub(rf"\s*{_S}([{re.escape(_GLUE_SYMBOLS)}]){_S}\s*", r"\1", text)
    # dot: glued before a lowercase letter/digit ("файл.пи", "~/.config"),
    # sentence spacing otherwise ("Привет. Это")
    text = re.sub(rf"\s*{_S}\.{_S}\s*(?=[a-zа-яё0-9])", ".", text)
    text = re.sub(rf"\s*{_S}\.{_S}", ".", text)
    # sentence punctuation: no space before, single space after
    text = re.sub(rf"\s*{_S}([,;!?]){_S}\s*", r"\1 ", text)
    text = text.replace(_S, "")
    return text.strip()


def copy_to_clipboard(text: str) -> None:
    _plat.copy_to_clipboard(text)


# --------------------------------------------------------------------------
# Key injection (platform adapter: uinput -> wtype, pynput elsewhere)
# --------------------------------------------------------------------------
def send_keys(combo: str = PASTE_COMBO, enter: bool = SEND_ENTER) -> None:
    if DRY_RUN:
        print(f"[dry-run] send combo={combo!r} enter={enter}")
        return
    try:
        _plat.get_inject_backend()(combo, enter)
    except Exception as exc:
        raise RuntimeError(f"failed to inject keys: {exc}") from exc
