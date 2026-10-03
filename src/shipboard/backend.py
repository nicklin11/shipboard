"""Backend lifecycle for whisper-local: container, proxy unit, config wiring.

Every deploy asset ships as package data under `src/shipboard/assets/`, so
nothing here depends on a source checkout: the compose bind mount resolves
relative to the packaged compose file, and the systemd units are rendered
with absolute installed paths before being written to ~/.config/systemd/user.

Subcommands (`shipboard backend ...`):
  up      compose up + external volume + health wait + units + config write
  status  container / model / proxy unit / configured URL, one exit code
"""

from __future__ import annotations

import argparse
import http.client
import importlib.resources
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import DEFAULT_CONFIG_PATH, HEALTH_URL, WHISPER_CONTAINER, WHISPER_URL

CONTAINER = WHISPER_CONTAINER
VOLUME = "whisper-local-data"
PROXY_UNIT = "whisper-tailnet-proxy.service"
IDLE_STOP_UNIT = "whisper-idle-stop.service"
IDLE_STOP_TIMER = "whisper-idle-stop.timer"
DAEMON_UNIT = "shipboard.service"
# The old pair shipped side by side: install.sh installed whisper-wake-proxy
# while the working deployment used the .example twin. One canonical unit now;
# anything else left over is pruned on `up` so `list-units 'whisper*'` is honest.
LEGACY_PROXY_UNITS = ("whisper-wake-proxy.service",)
BACKEND_PORT = 10302
PROXY_PORT = 10301
HEALTH_TIMEOUT = 600.0


def _assets_dir() -> Path:
    return Path(str(importlib.resources.files("shipboard") / "assets"))


def _asset(*parts: str) -> Path:
    return _assets_dir().joinpath(*parts)


def _unit_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def _console_script() -> str:
    """The daemon unit's ExecStart.

    `sys.argv[0]` is wrong here: under `python -m shipboard` it is __main__.py,
    which is not an executable. Prefer the installed entry point, and fall back
    to an explicit module invocation (valid ExecStart syntax) for a checkout.
    """
    found = shutil.which("shipboard")
    if found:
        return str(Path(found).resolve())
    return f"{sys.executable} -m shipboard"


def _docker() -> str | None:
    return shutil.which("docker") or ("/usr/bin/docker" if Path("/usr/bin/docker").is_file() else None)


def _run(cmd: list[str], timeout: float = 60.0, env: dict | None = None) -> tuple[int, str, str]:
    """Run a command, never raise: (returncode, stdout, stderr).

    127 = binary missing, 124 = timed out. Callers branch on the code and
    print the text, so a missing docker/systemctl degrades to a message
    instead of a traceback.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _compose_cmd(docker: str) -> list[str]:
    """Compose v2 plugin, falling back to the standalone binary."""
    if _run([docker, "compose", "version"], timeout=20)[0] == 0:
        return [docker, "compose"]
    standalone = shutil.which("docker-compose")
    return [standalone] if standalone else [docker, "compose"]


def _tailnet_ip() -> str | None:
    """This machine's first tailscale IPv4, or None when not on a tailnet."""
    tailscale = shutil.which("tailscale")
    if not tailscale:
        return None
    rc, out, _err = _run([tailscale, "ip", "-4"], timeout=10)
    first = out.split()
    return first[0] if rc == 0 and first else None


def _http_ok(url: str, timeout: float = 3.0) -> bool:
    parts = urlsplit(url)
    if not parts.hostname:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    conn = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
    try:
        conn.request("GET", parts.path or "/")
        return 200 <= conn.getresponse().status < 300
    except OSError:
        return False
    finally:
        conn.close()


def render_unit(name: str, values: dict[str, str]) -> str:
    """Substitute @NAME@ placeholders in a packaged unit template."""
    text = _asset("systemd", name).read_text()
    for key, value in values.items():
        text = text.replace(f"@{key}@", value)
    return text


def _install_unit(name: str, body: str) -> str:
    """Write a rendered unit to ~/.config/systemd/user; report change or no-op."""
    target = _unit_dir() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_text() == body:
        return f"{name}: unchanged"
    existed = "replaced" if target.is_file() else "installed"
    target.write_text(body)
    return f"{name}: {existed}"


def _prune_legacy_units() -> list[str]:
    """Disable and remove proxy units this project no longer ships."""
    notes = []
    for name in LEGACY_PROXY_UNITS:
        target = _unit_dir() / name
        if not target.is_file():
            continue
        # Unconditional: a unit can be active while disabled, and leaving a
        # loaded whisper-wake-proxy around is exactly the two-unit state this
        # removes. Failure here is not fatal — the file still goes.
        _run(["systemctl", "--user", "disable", "--now", name], timeout=30)
        target.unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"], timeout=30)
        notes.append(f"{name}: removed (superseded by {PROXY_UNIT})")
    return notes


def _set_toml_keys(path: Path, updates: dict[str, str]) -> dict[str, str]:
    """Upsert top-level string keys in place, preserving comments and layout.

    Used instead of the setup editor's full rewrite: `backend up` must not
    discard the user's hand-tuned file, and must not depend on the daemon's
    module-level config having been loaded with the old values.
    """
    previous: dict[str, str] = {}
    lines = path.read_text().splitlines() if path.is_file() else []
    for key, value in updates.items():
        needle = f"{key} ="
        for index, line in enumerate(lines):
            if line != line.lstrip() or not line.startswith(needle):
                continue
            _, _, rest = line.partition("=")
            previous[key] = rest.split("#")[0].strip().strip('"').strip("'")
            # keep any trailing comment so tuning notes survive a rewrite
            comment = "  #" + rest.split(" #", 1)[1] if " #" in rest else ""
            lines[index] = f'{key} = "{value}"{comment}'
            break
        else:
            lines.append(f'{key} = "{value}"')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return previous


def _container_state(docker: str, container: str) -> tuple[str, str, int]:
    """(state, health, exit_code); state is 'absent' when docker has no such container."""
    fmt = "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.State.ExitCode}}"
    rc, out, _err = _run([docker, "inspect", "-f", fmt, container], timeout=20)
    if rc != 0:
        return "absent", "none", 0
    parts = (out.split("|") + ["none", "0"])[:3]
    try:
        return parts[0], parts[1], int(parts[2] or 0)
    except ValueError:
        return parts[0], parts[1], 0


def _model_files(docker: str, container: str) -> list[str] | None:
    """ggml-*.bin entries in the model volume, or None when undeterminable."""
    rc, out, _err = _run(
        [docker, "exec", container, "sh", "-c", "ls -1 /models 2>/dev/null || true"],
        timeout=20,
    )
    if rc != 0:
        return None
    return sorted(name for name in out.split() if name.startswith("ggml-") and name.endswith(".bin"))


def _unit_active(unit: str) -> str:
    """active | inactive | failed | not-installed"""
    rc, out, _err = _run(["systemctl", "--user", "is-active", unit], timeout=15)
    if rc == 0:
        return "active"
    if out in ("inactive", "failed", "activating", "deactivating"):
        return out
    return "not-installed" if rc == 4 else f"unknown({rc})"


def _wait_healthy(docker: str, timeout: float) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last = ""
    nudged = False
    while True:
        state, health, exit_code = _container_state(docker, CONTAINER)
        if state in ("exited", "dead") or (state == "absent" and last):
            return False, f"container {state}" + (f" (exit {exit_code})" if exit_code else "")
        if state == "absent":
            return False, "container not found"
        if health == "healthy":
            return True, "healthy"
        last = state
        if time.monotonic() >= deadline:
            return False, f"health {health} after {timeout:.0f}s"
        if health == "starting" and not nudged:
            nudged = True  # once, not once per poll
            print("  first run downloads the model into the volume — this can take a while",
                  flush=True)
        time.sleep(3.0)


def _up(args: argparse.Namespace) -> int:
    print(f"shipboard backend up — container {CONTAINER}, volume {VOLUME}")
    docker = _docker()
    if not docker:
        print("shipboard: docker not found in PATH", file=sys.stderr)
        return 1
    rc, _out, err = _run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=30)
    if rc != 0:
        print(f"shipboard: docker daemon unreachable: {err or 'docker info failed'}", file=sys.stderr)
        return 1

    rc, _out, err = _run([docker, "volume", "create", VOLUME], timeout=60)
    print(f"  volume {VOLUME}: {'present' if rc == 0 else 'FAILED ' + err}")

    compose = _compose_cmd(docker)
    compose_file = _asset("docker-compose.yml")
    env = dict(os.environ, USER_UID=str(os.getuid()), USER_GID=str(os.getgid()))
    print(f"  compose up -d: {compose_file}")
    rc, _out, err = _run(compose + ["-f", str(compose_file), "up", "-d"],
                         timeout=900, env=env)
    if rc != 0:
        print(f"shipboard: compose up failed: {err}", file=sys.stderr)
        _run(compose + ["-f", str(compose_file), "logs", "--tail", "30"], timeout=60)
        return 1

    print(f"  waiting for {CONTAINER} health (up to {args.timeout:.0f}s)…")
    ok, why = _wait_healthy(docker, args.timeout)
    if not ok:
        print(f"shipboard: container did not become healthy: {why}", file=sys.stderr)
        _run(compose + ["-f", str(compose_file), "logs", "--tail", "30"], timeout=60)
        return 1
    print("  container healthy")

    host = args.host or os.environ.get("WHISPER_PROXY_HOST") or _tailnet_ip() or "127.0.0.1"
    port = args.port
    if not args.no_units:
        print("  systemd units:")
        for note in _prune_legacy_units():
            print(f"    {note}")
        print("    " + _install_unit(PROXY_UNIT, render_unit(PROXY_UNIT, {
            "PROXY_HOST": host, "PROXY_PORT": str(port), "BACKEND_PORT": str(args.backend_port),
            "PROXY_SCRIPT": str(_asset("scripts", "whisper_wake_proxy.py")),
        })))
        print("    " + _install_unit(IDLE_STOP_UNIT, render_unit(IDLE_STOP_UNIT, {
            "IDLE_STOP_SCRIPT": str(_asset("scripts", "whisper_idle_stop.sh")),
        })))
        print("    " + _install_unit(IDLE_STOP_TIMER,
                                   _asset("systemd", IDLE_STOP_TIMER).read_text()))
        print("    " + _install_unit(DAEMON_UNIT, render_unit(DAEMON_UNIT, {
            "SHIPBOARD_BIN": _console_script(),
        })) + " (installed, not enabled)")
        _run(["systemctl", "--user", "daemon-reload"], timeout=30)
        # restart, not start: a rewritten ExecStart only takes effect on restart
        rc, _out, err = _run(["systemctl", "--user", "enable", PROXY_UNIT, IDLE_STOP_TIMER], timeout=60)
        if rc != 0:
            print(f"shipboard: systemctl enable failed: {err}", file=sys.stderr)
            return 1
        rc, _out, err = _run(["systemctl", "--user", "restart", PROXY_UNIT], timeout=60)
        if rc != 0:
            print(f"shipboard: proxy restart failed: {err}", file=sys.stderr)
            return 1
        _run(["systemctl", "--user", "start", IDLE_STOP_TIMER], timeout=60)

    url = f"http://{host}:{port}"
    print(f"  proxy: {url} (unit {PROXY_UNIT})")
    if args.no_config:
        print("  config: skipped (--no-config)")
    else:
        previous = _set_toml_keys(DEFAULT_CONFIG_PATH, {
            "whisper_url": f"{url}/inference", "whisper_health_url": f"{url}/health",
        })
        for key, value in (("whisper_url", f"{url}/inference"),
                           ("whisper_health_url", f"{url}/health")):
            old = previous.get(key)
            print(f"  {key}: {old or '(unset)'} → {value}")

    print("\nbackend ready. `shipboard backend status` re-checks it; "
          "`systemctl --user enable --now shipboard` runs the daemon.")
    return 0


def _status_json(snapshot: dict) -> int:
    print(json.dumps(snapshot, indent=2, ensure_ascii=False))
    return 0 if snapshot["ready"] else 1


def _status(args: argparse.Namespace) -> int:
    problems: list[str] = []
    docker = _docker()
    state = health = "unknown"
    exit_code = 0
    models: list[str] | None = None
    if not docker:
        problems.append("docker not installed")
    else:
        rc, _out, err = _run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=30)
        if rc != 0:
            problems.append(f"docker daemon unreachable: {err or 'docker info failed'}")
        else:
            state, health, exit_code = _container_state(docker, CONTAINER)
            if state == "absent":
                problems.append(f"container {CONTAINER} does not exist — `shipboard backend up`")
            elif state in ("exited", "dead"):
                problems.append(f"container {state} (exit {exit_code}) — `shipboard backend up`")
            elif state != "running":
                problems.append(f"container {state}")
            elif health != "healthy":
                problems.append(f"container running, health {health}")
            if state == "running":
                models = _model_files(docker, CONTAINER)
                if models == []:
                    problems.append(
                        f"model absent: {VOLUME} has no ggml-*.bin — the first "
                        "`backend up` downloads it, check `docker logs " + CONTAINER + "`")

    proxy = _unit_active(PROXY_UNIT)
    if proxy != "active":
        problems.append(f"proxy unit {PROXY_UNIT} {proxy} — `shipboard backend up`")

    health_url = HEALTH_URL
    reachable = _http_ok(health_url)
    if not reachable:
        problems.append(f"{health_url} unreachable"
                        + ("" if proxy == "active" else " (proxy unit is not active)"))

    snapshot = {
        "ready": not problems,
        "container": {"name": CONTAINER, "state": state, "health": health,
                      "exit_code": exit_code},
        "model": {"volume": VOLUME, "files": models,
                  "present": bool(models)},
        "proxy_unit": {"name": PROXY_UNIT, "state": proxy},
        "url": {"whisper_url": WHISPER_URL, "whisper_health_url": health_url,
                "reachable": reachable},
        "problems": problems,
    }
    if args.json:
        return _status_json(snapshot)

    print(f"container  {CONTAINER}: {state}"
          + (f" (health {health})" if health != "none" else "")
          + (f" exit={exit_code}" if exit_code else ""))
    if models is None:
        print(f"model      {VOLUME}: unknown (container not running)")
    else:
        print(f"model      {VOLUME}: {'present' if models else 'ABSENT'}"
              + (f" — {', '.join(models)}" if models else ""))
    print(f"proxy      {PROXY_UNIT}: {proxy}")
    print(f"url        {WHISPER_URL}")
    print(f"health     {health_url}: {'ok' if reachable else 'UNREACHABLE'}")
    if problems:
        print("\nnot ready:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nready.")
    return 0


def backend_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="shipboard backend",
        description="Bring up and inspect the local whisper-local backend.",
    )
    sub = parser.add_subparsers(dest="action", required=True)

    up = sub.add_parser("up", help="create the volume, start the container, "
                                   "install units, write whisper_url")
    up.add_argument("--host", default=None, help="proxy listen address "
                                                  "(default: tailnet IP, else 127.0.0.1)")
    up.add_argument("--port", type=int, default=PROXY_PORT, help=f"proxy port (default: {PROXY_PORT})")
    up.add_argument("--backend-port", type=int, default=BACKEND_PORT,
                    help=f"container backend port (default: {BACKEND_PORT})")
    up.add_argument("--timeout", type=float, default=HEALTH_TIMEOUT,
                    help=f"seconds to wait for health (default: {HEALTH_TIMEOUT:.0f})")
    up.add_argument("--no-units", action="store_true", help="skip systemd unit installation")
    up.add_argument("--no-config", action="store_true", help="do not write whisper_url to the config")

    status = sub.add_parser("status", help="container, model, proxy unit and configured URL")
    status.add_argument("--json", action="store_true", help="machine-readable output")

    args = parser.parse_args(argv)
    if args.action == "up":
        return _up(args)
    return _status(args)