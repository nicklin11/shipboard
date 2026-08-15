#!/usr/bin/env python3
"""Wake the GPU Whisper container on demand and proxy its HTTP API."""

from __future__ import annotations

import http.client
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


DOCKER = "/usr/bin/docker"
CONTAINER = os.environ.get("WHISPER_CONTAINER", "whisper-local")
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = int(os.environ.get("WHISPER_BACKEND_PORT", "10302"))
LISTEN_HOST = os.environ.get("WHISPER_PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("WHISPER_PROXY_PORT", "10300"))
START_TIMEOUT = float(os.environ.get("WHISPER_START_TIMEOUT", "60"))
IDLE_MARKER = Path(
    os.environ.get("WHISPER_IDLE_MARKER", "/tmp/whisper-local-last-use")
)
_start_lock = threading.Lock()


def _touch_marker() -> None:
    IDLE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    IDLE_MARKER.touch()


def _backend_healthy() -> bool:
    connection = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=2)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        response.read()
        return 200 <= response.status < 300
    except OSError:
        return False
    finally:
        connection.close()


def _ensure_backend(mark_activity: bool = False) -> None:
    if mark_activity:
        _touch_marker()
    if _backend_healthy():
        return

    with _start_lock:
        if _backend_healthy():
            return
        # A wake-only health check gets a short grace period, but does not
        # count as user activity once the backend is already running.
        _touch_marker()
        try:
            subprocess.run(
                [DOCKER, "start", CONTAINER],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f"could not start {CONTAINER}: {exc}") from exc

        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if _backend_healthy():
                return
            time.sleep(0.5)

    raise RuntimeError(
        f"{CONTAINER} did not become ready within {START_TIMEOUT:.0f} seconds"
    )


class _Heartbeat:
    def __init__(self, interval: float = 30) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(interval,), daemon=True)

    def _run(self, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                _touch_marker()
            except OSError:
                return

    def __enter__(self) -> "_Heartbeat":
        _touch_marker()
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        _touch_marker()


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._proxy_request(b"")

    def do_HEAD(self) -> None:  # noqa: N802
        self._proxy_request(b"")

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_error(400, "invalid Content-Length")
            return
        self._proxy_request(self.rfile.read(length))

    def _proxy_request(self, body: bytes) -> None:
        is_activity = (
            self.command == "POST" and urlsplit(self.path).path == "/inference"
        )
        try:
            if is_activity:
                with _Heartbeat():
                    _ensure_backend(mark_activity=True)
                    self._forward(body)
            else:
                _ensure_backend(mark_activity=False)
                self._forward(body)
        except (OSError, RuntimeError) as exc:
            self._send_error(503, str(exc))

    def _forward(self, body: bytes) -> None:
        target = urlsplit(self.path)
        path = target.path or "/"
        if target.query:
            path += f"?{target.query}"

        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "connection", "content-length"}
        }
        headers["Host"] = f"{BACKEND_HOST}:{BACKEND_PORT}"
        if body:
            headers["Content-Length"] = str(len(body))

        connection = http.client.HTTPConnection(
            BACKEND_HOST, BACKEND_PORT, timeout=300
        )
        try:
            connection.request(self.command, path, body=body or None, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
        finally:
            connection.close()

        self.send_response(response.status, response.reason)
        for key, value in response.getheaders():
            if key.lower() not in {"connection", "transfer-encoding", "content-length"}:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response_body)

    def _send_error(self, status: int, message: str) -> None:
        payload = f"{{\"error\":{message!r}}}".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        print(f"whisper-wake-proxy: {format % args}", flush=True)


class ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    server = ProxyServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    print(
        f"whisper-wake-proxy listening on {LISTEN_HOST}:{LISTEN_PORT}, "
        f"backend {BACKEND_HOST}:{BACKEND_PORT}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
