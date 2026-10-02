"""Whisper.cpp (docker) transcription: multipart POST, container wake."""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
import uuid
import wave
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

from .actions import copy_to_clipboard, normalize_text
from .config import (HEALTH_URL, IDLE_MARKER, PROMPT, WHISPER_CONTAINER,
                     WHISPER_LANGUAGE, WHISPER_URL)

# A request has to outlive the audio it carries. Measured on this box
# (large-v3-turbo + silero VAD, 6 threads): ~16x realtime, so an allowance
# of 0.75x realtime plus a minute leaves ~20x headroom while still capping a
# wedged server well below the lecture length.
REALTIME_ALLOWANCE = 0.75
TIMEOUT_FLOOR = 120.0
TIMEOUT_HEADROOM = 60.0
# whisper-idle-stop.timer SIGTERMs the container once the marker is older than
# WHISPER_IDLE_SECONDS (300). Touching it at most this often keeps a long
# request alive.
IDLE_TOUCH_INTERVAL = 60.0

def _multipart_body(fields: dict[str, str], wav_path: Path) -> tuple[bytes, str]:
    boundary = f"----shipboard-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n',
            b"Content-Type: audio/wav\r\n\r\n",
            wav_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _server_healthy() -> bool:
    try:
        with urllib_request.urlopen(HEALTH_URL, timeout=2) as resp:
            return 200 <= resp.status < 300
    except (OSError, urllib_error.URLError):
        return False


def _ensure_server(timeout: float = 60.0) -> None:
    """Touch the idle marker and wake the container if needed."""
    IDLE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    IDLE_MARKER.touch()
    if not _server_healthy():
        try:
            subprocess.run(
                ["docker", "start", WHISPER_CONTAINER],
                capture_output=True,
                timeout=30,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _server_healthy():
            return
        time.sleep(0.5)
    raise RuntimeError(f"whisper.cpp did not come up at {HEALTH_URL}")


def _audio_seconds(path: Path) -> float | None:
    """Duration of a WAV in seconds, or None when it cannot be read cheaply."""
    if path.suffix.lower() != ".wav":
        return None
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as wav:
            rate = wav.getframerate()
            return wav.getnframes() / rate if rate else None
    except (wave.Error, OSError, EOFError, ValueError):
        # wave raises EOFError (not an OSError) on a truncated header and
        # ValueError on a zero/absent framerate.
        return None


def _request_timeout(path: Path) -> float:
    """HTTP timeout for transcribing `path`: scale it to the audio length.

    The old fixed 120 s expired on any recording longer than ~30 minutes,
    which is every lecture — whisper.cpp was still working when urllib gave
    up.
    """
    seconds = _audio_seconds(path)
    if seconds is None:
        return TIMEOUT_FLOOR
    return max(TIMEOUT_FLOOR, seconds * REALTIME_ALLOWANCE + TIMEOUT_HEADROOM)


@contextlib.contextmanager
def _idle_heartbeat(interval: float = IDLE_TOUCH_INTERVAL):
    """Keep the whisper idle marker fresh for the duration of a request.

    _ensure_server touches the marker once, on entry. A transcription that
    outlasts WHISPER_IDLE_SECONDS (300) would otherwise be killed mid-flight
    by whisper-idle-stop.timer, surfacing as a dropped connection.
    """
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(interval):
            try:
                IDLE_MARKER.touch()
            except OSError:
                pass

    thread = threading.Thread(target=beat, name="shipboard-idle-beat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)


def _transcribe_payload(wav_path: Path, *, verbose: bool = False) -> tuple[object, str]:
    """POST the wav to whisper.cpp; return (decoded JSON, raw payload text).

    Single HTTP path for `transcribe` and `transcribe_segments`.

    `verbose=True` asks for `response_format=verbose_json`. That is not a
    cosmetic flag: whisper.cpp answers the default `json` format with
    `{"text": ...}` and nothing else — there are no segments in it to read.
    Only verbose_json carries `segments` (and per-segment `words`). Measured
    against this box's server, 30 s probe, 9 segments: 0.00-2.44, 2.44-6.20,
    ... 28.44-29.23 against a duration of 30.0.

    Times in that array are therefore SECONDS as floats, not milliseconds.
    """
    _ensure_server()
    fields = {"language": WHISPER_LANGUAGE}
    if PROMPT:
        fields["prompt"] = PROMPT
    if verbose:
        fields["response_format"] = "verbose_json"
    body, content_type = _multipart_body(fields, wav_path)
    req = urllib_request.Request(
        WHISPER_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    try:
        with _idle_heartbeat(), urllib_request.urlopen(
            req, timeout=_request_timeout(wav_path)
        ) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"whisper.cpp HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"whisper.cpp unavailable: {exc.reason}") from exc

    try:
        return json.loads(payload), payload
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"whisper.cpp returned non-JSON: {payload[:300]}") from exc


def transcribe(wav_path: Path) -> str:
    """Plain transcript, no timings. Request is unchanged from before."""
    result, payload = _transcribe_payload(wav_path)
    text = result.get("text") if isinstance(result, dict) else result
    if not isinstance(text, str):
        raise RuntimeError(f"whisper.cpp: no text in response: {payload[:300]}")
    return text.strip()


def transcribe_segments(wav_path: Path) -> list[dict]:
    """Segments with `start`/`end` in seconds (floats) and per-word timings.

    Raises RuntimeError if the server answered without a usable `segments`
    array — which is what a build or proxy that ignores verbose_json does.
    """
    result, payload = _transcribe_payload(wav_path, verbose=True)
    segments = result.get("segments") if isinstance(result, dict) else None
    if not isinstance(segments, list):
        raise RuntimeError(
            "whisper.cpp returned no segments (needs response_format=verbose_json): "
            f"{payload[:300]}"
        )
    return segments


def format_timestamp(seconds: float) -> str:
    """Seconds (float, as whisper.cpp reports them) -> [hh:mm:ss]."""
    total = int(seconds)
    return f"[{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}]"


def segments_to_timestamped_text(segments: list[dict], *, normalize: bool = True) -> str:
    """`[hh:mm:ss] text` per line, one line per segment.

    Each segment is normalized on its own before the prefix goes on: running
    normalize_text over the joined output would eat the bracket prefix's
    spacing and glue the timestamp to the words after it. Stripping the
    `[hh:mm:ss] ` prefixes from the result yields the normalized transcript.
    """
    lines: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = seg.get("text")
        if not isinstance(text, str):
            continue
        text = text.strip()
        if not text:
            continue
        if normalize:
            text = normalize_text(text)
        start = seg.get("start")
        stamp = format_timestamp(start) if isinstance(start, (int, float)) else "[--:--:--]"
        lines.append(f"{stamp} {text}")
    return "\n".join(lines)


def _transcribe_copy(wav: Path) -> tuple[str, str]:
    """Transcribe -> normalize -> copy to clipboard. Returns (text, preview).
    Raises RuntimeError with a user-facing message on any failure."""
    try:
        text = transcribe(wav)
    except Exception as exc:
        raise RuntimeError(f"STT error: {exc}") from exc
    text = normalize_text(text)
    if not text:
        raise RuntimeError("Nothing recognized")
    try:
        copy_to_clipboard(text)
    except Exception as exc:
        raise RuntimeError(f"Copy failed: {exc}") from exc
    preview = text if len(text) <= 100 else text[:100] + "…"
    return text, preview
