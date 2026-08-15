#!/usr/bin/env python3
"""Transcribe Hermes audio through the local whisper.cpp HTTP server."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit, urlunsplit


def _load_local_env() -> None:
    """Load host-specific STT settings without requiring Hermes to be edited."""
    env_path = Path(
        os.environ.get(
            "WHISPER_CPP_ENV_FILE", str(Path.home() / ".hermes/stt/whisper.env")
        )
    )
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_local_env()


FFMPEG = "/usr/bin/ffmpeg"
DOCKER = "/usr/bin/docker"
SERVER_CONTAINER = os.environ.get("WHISPER_CONTAINER", "whisper-local")
IDLE_MARKER = Path(
    os.environ.get("WHISPER_IDLE_MARKER", "/tmp/whisper-local-last-use")
)
SERVER_URL = os.environ.get(
    "WHISPER_CPP_URL", "http://127.0.0.1:10300/inference"
)
_server_parts = urlsplit(SERVER_URL)
HEALTH_URL = os.environ.get(
    "WHISPER_CPP_HEALTH_URL",
    urlunsplit((_server_parts.scheme, _server_parts.netloc, "/health", "", "")),
)
AUTOSTART = os.environ.get("WHISPER_AUTOSTART", "auto").lower()
DEFAULT_PROMPT = (
    "Русская и English речь с переключением языка. "
    "Technical terms and names: Docker, Docker Compose, GitHub, Linux, "
    "Python, JavaScript, TypeScript, API, UI, STT, config, .config, "
    "Hermes, QuickShell, ExistingLoner, Noctalia Shell, Wayland, Niri, "
    "Waybar, Fuzzel, Firefox, FunPay, Reanimal."
)
PROMPT = os.environ.get("WHISPER_CPP_PROMPT", DEFAULT_PROMPT)
KNOWN_TERM_REPLACEMENTS = (
    (
        re.compile(r"\bExisting\s+(?:Loner|Loaner|Lanner)(?:\.de)?\b", re.I),
        "ExistingLoner",
    ),
    (re.compile(r"\bExistingLoner\.de\b", re.I), "ExistingLoner"),
    (re.compile(r"\bQuick\s+Shell\b", re.I), "QuickShell"),
    (re.compile(r"\bFun\s+Pay\b", re.I), "FunPay"),
    (re.compile(r"\bFire\s+Fox\b", re.I), "Firefox"),
    (re.compile(r"\bRe\s+animal\b", re.I), "Reanimal"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe an audio file through the local whisper.cpp server."
    )
    parser.add_argument("--input", required=True, type=Path, help="Source audio file")
    parser.add_argument(
        "--output", required=True, type=Path, help="Destination transcript file"
    )
    parser.add_argument(
        "--language",
        default="auto",
        help="Whisper language code, or 'auto' for bilingual speech",
    )
    parser.add_argument(
        "--timeout", type=float, default=300, help="End-to-end timeout in seconds"
    )
    return parser.parse_args()


def _multipart_body(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = f"----hermes-whisper-{uuid.uuid4().hex}"
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
            file_path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _touch_idle_marker() -> None:
    IDLE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    IDLE_MARKER.touch()


def _server_is_healthy() -> bool:
    try:
        with urllib_request.urlopen(HEALTH_URL, timeout=2) as response:
            return 200 <= response.status < 300
    except (OSError, urllib_error.URLError):
        return False


def _should_autostart() -> bool:
    if AUTOSTART in {"1", "true", "yes", "on"}:
        return True
    if AUTOSTART in {"0", "false", "no", "off"}:
        return False
    return _server_parts.hostname in {"127.0.0.1", "localhost", "::1"}


def _ensure_server(timeout: float) -> None:
    """Start the stopped container and wait for the model to be ready."""
    _touch_idle_marker()
    startup_timeout = min(
        timeout, float(os.environ.get("WHISPER_START_TIMEOUT", "60"))
    )
    if not _server_is_healthy() and _should_autostart():
        try:
            subprocess.run(
                [DOCKER, "start", SERVER_CONTAINER],
                check=False,
                capture_output=True,
                text=True,
                timeout=min(startup_timeout, 30),
            )
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f"could not start {SERVER_CONTAINER}: {exc}") from exc

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if _server_is_healthy():
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"whisper.cpp did not become ready at {HEALTH_URL} within {startup_timeout:.0f}s"
    )


class _IdleHeartbeat:
    """Keep the idle reaper from stopping a container during an active request."""

    def __init__(self, interval: float = 30) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                _touch_idle_marker()
            except OSError:
                return

    def __enter__(self) -> "_IdleHeartbeat":
        _touch_idle_marker()
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        _touch_idle_marker()


def _transcribe(wav_path: Path, language: str, timeout: float) -> str:
    fields = {
        "response_format": "json",
        "language": language or "auto",
        "temperature": "0.0",
        "temperature_inc": "0.2",
        "beam_size": "5",
        "best_of": "5",
        "prompt": PROMPT,
        "carry_initial_prompt": "true",
        "no_timestamps": "true",
    }
    body, content_type = _multipart_body(fields, wav_path)
    request = urllib_request.Request(
        SERVER_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        },
        method="POST",
    )

    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except urllib_error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"whisper.cpp HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(
            f"whisper.cpp is unavailable at {SERVER_URL}: {exc.reason}"
        ) from exc

    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"whisper.cpp returned invalid JSON: {payload[:300]}") from exc

    if isinstance(result, dict):
        text = result.get("text")
    else:
        text = result
    if not isinstance(text, str):
        raise RuntimeError(f"whisper.cpp response has no text: {payload[:300]}")
    return _normalize_terms(text)


def _normalize_terms(text: str) -> str:
    """Restore project/app names that Whisper commonly splits phonetically."""
    for pattern, replacement in KNOWN_TERM_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text.strip()


def _transcribe_local_hermes(input_path: Path) -> str:
    """Fallback to Hermes' built-in local faster-whisper backend."""
    agent_root = Path(
        os.environ.get("HERMES_AGENT_ROOT", str(Path.home() / ".hermes/hermes-agent"))
    )
    if str(agent_root) not in sys.path:
        sys.path.insert(0, str(agent_root))
    try:
        from tools.transcription_tools import _transcribe_local
    except Exception as exc:
        raise RuntimeError(f"cannot load Hermes local STT: {exc}") from exc
    model = os.environ.get("WHISPER_FALLBACK_MODEL", "base")
    result = _transcribe_local(str(input_path), model)
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError(str((result or {}).get("error", "local Hermes STT failed")))
    return _normalize_terms(str(result.get("transcript", "")))


def run(args: argparse.Namespace) -> str:
    input_path = args.input.expanduser().resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {input_path}")

    with _IdleHeartbeat():
        _ensure_server(args.timeout)
        with tempfile.TemporaryDirectory(prefix="hermes-whisper-cpp-") as temp_dir:
            wav_path = Path(temp_dir) / "audio.wav"
            subprocess.run(
                [
                    FFMPEG,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-i",
                    str(input_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "wav",
                    "-y",
                    str(wav_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
            return _transcribe(wav_path, args.language, args.timeout)


def main() -> int:
    args = parse_args()
    try:
        transcript = run(args)
    except (FileNotFoundError, subprocess.SubprocessError, RuntimeError) as exc:
        if os.environ.get("WHISPER_FALLBACK_LOCAL", "").lower() in {"1", "true", "yes", "on"}:
            try:
                print(f"whisper-cpp-stt: remote failed, using local fallback: {exc}", file=sys.stderr)
                transcript = _transcribe_local_hermes(args.input.expanduser().resolve())
            except Exception as fallback_exc:
                print(f"whisper-cpp-stt: local fallback failed: {fallback_exc}", file=sys.stderr)
                return 1
        else:
            print(f"whisper-cpp-stt: {exc}", file=sys.stderr)
            return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(transcript, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
