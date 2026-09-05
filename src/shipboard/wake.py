"""Wake word listener (sherpa-onnx KWS, streaming via pw-cat)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib import request as urllib_request

from .actions import _notify
from .config import (CHANNELS, KWS_THREADS, MAX_HOLD, RATE, RECORD_TARGET,
                     SCROLL_SEND_ENTER, WAKEWORD_ACTION, WAKEWORD_COOLDOWN,
                     WAKEWORD_DEBUG, WAKEWORD_GRACE, WAKEWORD_KEYWORDS,
                     WAKEWORD_SHERPA_SCORE, WAKEWORD_SHERPA_THRESHOLD,
                     WAKEWORD_SILENCE_LEVEL, WAKEWORD_STOP_SILENCE)
from .logstate import _log

_WAKE_MODELS_DIR = Path.home() / ".local/share/shipboard/models"
_WAKE_VENV = Path.home() / ".local/share/shipboard-venv"
_WAKE_SILENCE_RMS = WAKEWORD_SILENCE_LEVEL / 32768.0
_SHERPA_MODEL_DIR = _WAKE_MODELS_DIR / \
    "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
_SHERPA_KEYWORDS_FILE = _WAKE_MODELS_DIR / "sherpa-kws-keywords.txt"
_SHERPA_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
               "kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20"
               ".tar.bz2")


def _parse_keywords(spec: str) -> list[tuple[str, str]]:
    """'alter capture:record, alter send:record_send' -> [(phrase, action)]."""
    out: list[tuple[str, str]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            phrase, action = part.rsplit(":", 1)
            out.append((phrase.strip(), action.strip()))
        else:
            out.append((part, WAKEWORD_ACTION))
    return out


def _ensure_sherpa_model() -> bool:
    needed = ["tokens.txt", "en.phone",
              "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx",
              "decoder-epoch-13-avg-2-chunk-16-left-64.onnx",
              "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx"]
    if all((_SHERPA_MODEL_DIR / f).is_file() for f in needed):
        return True
    _WAKE_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    _log("wakeword: downloading sherpa-onnx KWS model (~17MB) ...")
    import tarfile
    try:
        tmp = _WAKE_MODELS_DIR / "sherpa-kws.tar.bz2.tmp"
        urllib_request.urlretrieve(_SHERPA_URL, tmp)
        with tarfile.open(tmp, "r:bz2") as tf:
            tf.extractall(_WAKE_MODELS_DIR, filter="data")
        tmp.unlink(missing_ok=True)
        return all((_SHERPA_MODEL_DIR / f).is_file() for f in needed)
    except Exception as exc:
        _log(f"wakeword: sherpa model download failed: {exc}")
        return False


def _ensure_sherpa_keywords() -> bool:
    """Tokenize the configured phrases into the KWS keywords file.

    The generated file is cached across restarts, but regenerated whenever
    the configured phrases change (so editing wake words takes effect).
    text2token runs PER PHRASE: its multi-line mode misaligns lines when a
    phrase is missing from the en.phone lexicon, so one bad word must not
    corrupt the whole file — it is skipped and logged instead.
    """
    tokens = _SHERPA_MODEL_DIR / "tokens.txt"
    lexicon = _SHERPA_MODEL_DIR / "en.phone"
    raw = _WAKE_MODELS_DIR / "sherpa-kws-raw.txt"
    # zh-en model: phone+ppinyin keywords; the original phrase goes after
    # '@' with spaces replaced by underscores (the spotter reports that
    # form), so 'alter send' comes back as 'ALTER_SEND'.
    lines = [f"{phrase.upper()} @{phrase.upper().replace(' ', '_')}"
             for phrase, _ in _parse_keywords(WAKEWORD_KEYWORDS)]
    raw_text = "\n".join(lines) + "\n"
    if (_SHERPA_KEYWORDS_FILE.is_file() and raw.is_file()
            and raw.read_text() == raw_text):
        return True
    raw.write_text(raw_text)
    cli = _WAKE_VENV / "bin" / "sherpa-onnx-cli"
    outputs: list[str] = []
    try:
        for i, line in enumerate(lines):
            one_raw = _WAKE_MODELS_DIR / f"sherpa-kws-one-{i}.raw"
            one_out = _WAKE_MODELS_DIR / f"sherpa-kws-one-{i}.txt"
            try:
                one_raw.write_text(line + "\n")
                r = subprocess.run(
                    [str(cli), "text2token", str(one_raw),
                     "--tokens", str(tokens),
                     "--tokens-type", "phone+ppinyin",
                     "--lexicon", str(lexicon), str(one_out)],
                    capture_output=True, timeout=60,
                )
                if r.returncode == 0 and one_out.is_file():
                    tokenized = one_out.read_text().strip()
                    if tokenized:
                        outputs.append(tokenized)
                        continue
                _log(f"wakeword: keyword {line!r} not tokenized"
                     f" (missing from en.phone lexicon?) — skipped")
            finally:
                one_raw.unlink(missing_ok=True)
                one_out.unlink(missing_ok=True)
    except Exception as exc:
        _log(f"wakeword: text2token failed: {exc}")
        return False
    if not outputs:
        _log("wakeword: no keywords tokenized — listener off")
        return False
    _SHERPA_KEYWORDS_FILE.write_text("\n".join(outputs) + "\n")
    return True


class _SherpaKws:
    """sherpa-onnx open-vocabulary keyword spotter (phrase -> action)."""

    def __init__(self) -> None:
        import sherpa_onnx
        self.pairs = _parse_keywords(WAKEWORD_KEYWORDS)
        self.actions = {phrase.casefold(): action
                        for phrase, action in self.pairs}
        # provider string "cpu:<config>" lets sherpa forward onnxruntime
        # session config entries (e.g. allow_spinning=0 -> no idle CPU burn).
        _ort_cfg = Path(__file__).resolve().parent / "ort-nospin.config"
        _provider = f"cpu:{_ort_cfg}" if _ort_cfg.is_file() else "cpu"
        self.spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(_SHERPA_MODEL_DIR / "tokens.txt"),
            encoder=str(_SHERPA_MODEL_DIR /
                        "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=str(_SHERPA_MODEL_DIR /
                        "decoder-epoch-13-avg-2-chunk-16-left-64.onnx"),
            joiner=str(_SHERPA_MODEL_DIR /
                       "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=str(_SHERPA_KEYWORDS_FILE),
            num_threads=KWS_THREADS, provider=_provider,
            keywords_score=WAKEWORD_SHERPA_SCORE,
            keywords_threshold=WAKEWORD_SHERPA_THRESHOLD,
        )
        self.stream = self.spotter.create_stream()

    def feed(self, audio) -> str | None:
        """Returns the detected phrase (or None)."""
        self.stream.accept_waveform(16000, audio)
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
        result = self.spotter.get_result(self.stream)
        if result:
            self.spotter.reset_stream(self.stream)
            # zh-en model reports the '@' original: underscores instead of
            # spaces ("ALTER_SEND") — normalize back to the phrase form.
            return result.strip().replace("_", " ")
        # NOTE: no reset_stream() here! is_ready()==False just means all
        # frames were consumed; resetting would wipe the decoder's keyword
        # hypotheses every chunk (80 ms) so a phrase spanning ~1s could
        # never accumulate enough score to trigger.
        return None

    def action_for(self, phrase: str) -> str:
        return self.actions.get(phrase.casefold(), WAKEWORD_ACTION)

    def describe(self) -> str:
        return "sherpa: " + ", ".join(p for p, _ in self.pairs)


def _wake_listen(self, stop_event: threading.Event) -> None:
    """Continuous listener: pw-cat -> detector -> record until silence."""
    # onnxruntime's intra-op threads busy-spin when idle (3 sessions =
    # encoder/decoder/joiner = ~3 cores of pure spin). Make them sleep.
    os.environ.setdefault("ORT_DISABLE_SPIN_WAIT", "1")
    # sherpa-onnx lives in the shipboard venv; make it
    # importable from the system python the daemon runs under.
    try:
        for sp in (_WAKE_VENV / "lib").glob("python*/site-packages"):
            sys.path.insert(0, str(sp))
        import numpy as np
    except Exception as exc:
        _log(f"wakeword: deps unavailable ({exc}) — listener off")
        return
    try:
        if not _ensure_sherpa_model() or not _ensure_sherpa_keywords():
            _log("wakeword: sherpa model/keywords unavailable — listener off")
            return
        detector = _SherpaKws()
        _log(f"wakeword: listening ({detector.describe()})")
        _notify("shipboard", f"Wake word on: {detector.describe()}")
    except Exception as exc:
        _log(f"wakeword: engine init failed: {exc}")
        return

    cmd = ["pw-cat", "--record", "--rate", str(RATE), "--channels",
           str(CHANNELS), "--format", "s16", "--raw", "-"]
    if RECORD_TARGET:
        cmd += ["--target", RECORD_TARGET]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        _log(f"wakeword: pw-cat failed: {exc}")
        return
    import atexit
    atexit.register(proc.terminate)

    last_trigger = 0.0
    silence_since: float | None = None
    next_level_log = 0.0
    try:
        while not stop_event.is_set():
            raw = proc.stdout.read(int(RATE * CHANNELS * 2 * 0.08))  # 80 ms
            if not raw:
                break
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            now = time.monotonic()
            rms = float(np.sqrt(np.mean(audio ** 2)))

            # Debug: mic level bar every second (see it "trying to catch").
            if WAKEWORD_DEBUG and now >= next_level_log:
                next_level_log = now + 1.0
                bar = "▁▂▃▄▅▆▇█"[min(7, int(rms * 40))]
                _log(f"wakeword: lvl {rms:.3f} {bar}")

            # While a wake-triggered recording is running: stop on silence
            # or the max-record cap. Silence is ignored during the post-
            # trigger grace period (user needs a beat to start dictating).
            if self.recording and self.wake_rec:
                in_grace = now - self.rec_t0 < WAKEWORD_GRACE
                if rms < _WAKE_SILENCE_RMS and not in_grace:
                    if silence_since is None:
                        silence_since = now
                    elif now - silence_since >= WAKEWORD_STOP_SILENCE:
                        _log("wakeword: silence — finishing recording")
                        self._finish_record(from_wake=True)
                        self.wake_rec = False
                        silence_since = None
                else:
                    silence_since = None
                if now - self.rec_t0 > MAX_HOLD:
                    _log("wakeword: max record — finishing")
                    self._finish_record(from_wake=True)
                    self.wake_rec = False
                continue

            if self.recording or now - last_trigger < WAKEWORD_COOLDOWN:
                continue
            phrase = detector.feed(audio)
            if phrase:
                last_trigger = now
                action = detector.action_for(phrase)
                _log(f"wakeword: DETECTED {phrase!r} -> {action}")
                if action == "paste":
                    _notify("shipboard", f"Paste (wake word: {phrase})")
                    self._inject_q.put(SCROLL_SEND_ENTER)
                    continue
                _notify("shipboard", f"Wake word detected: {phrase}")
                self.wake_rec = True
                self._start_record()
                # autosend is set AFTER _start_record: the latter resets
                # autosend=False at the start of every recording, so setting
                # it before was silently wiped (wake record_send never sent).
                # Same order as the key path (_on_pause/_on_scrolllock).
                self.autosend = action == "record_send" and self.recording
                _log(f"wakeword: trigger autosend={self.autosend} wake_rec={self.wake_rec}")
                silence_since = None
    except (OSError, ValueError):
        pass
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
    _log("wakeword: listener stopped")
