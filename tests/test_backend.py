#!/usr/bin/env python3
"""`shipboard backend up|status`: deploy assets must not need a checkout.

Issue #9: install required a git clone at ~/Coding/shipboard because the
units ExecStart'd repo scripts and compose bind-mounted ./scripts/*. Two
units shipped side by side, and install.sh installed the one nobody used.

Only _run() is stubbed — every ordering/branch assertion below therefore
exercises the real orchestration in src/shipboard/backend.py.
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from shipboard import backend as sbb  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not cond:
        failures.append(name)


class Fake:
    """Canned docker/systemctl replies; records every call in order."""

    def __init__(self, container="running|healthy|0", models=("ggml-large-v3-turbo.bin",),
                 unit="active", compose_rc=0, inspect_rc=0, exec_rc=0):
        self.container = container
        self.models = list(models) if models is not None else []
        self.unit = unit
        self.compose_rc = compose_rc
        self.inspect_rc = inspect_rc
        self.exec_rc = exec_rc
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []

    def __call__(self, cmd, timeout=60.0, env=None):
        self.calls.append(list(cmd))
        self.envs.append(env)
        if "info" in cmd:
            return 0, "27.5.0", ""
        if cmd[1:3] == ["compose", "version"]:
            return 0, "v2.29.0", ""
        if cmd[1:2] == ["volume"]:
            return 0, sbb.VOLUME, ""
        if "compose" in cmd and "up" in cmd:
            return self.compose_rc, "", "port is already allocated" if self.compose_rc else ""
        if "compose" in cmd and "logs" in cmd:
            return 0, "whisper-local | server log line", ""
        if "inspect" in cmd:
            return (self.inspect_rc, "No such object", "") if self.inspect_rc else (0, self.container, "")
        if "exec" in cmd:
            return (self.exec_rc, "", "exec failed") if self.exec_rc else (0, " ".join(self.models), "")
        if "is-active" in cmd:
            if self.unit == "active":
                return 0, "active", ""
            if self.unit == "not-installed":  # systemctl exit 4 = unknown unit
                return 4, "unknown", ""
            return 3, self.unit, ""
        if "is-enabled" in cmd:
            return 1, "disabled", ""
        return 0, "", ""

    def index(self, *tokens) -> int:
        """Position of the first call containing every token, else -1."""
        for position, cmd in enumerate(self.calls):
            if all(token in cmd for token in tokens):
                return position
        return -1

    def env_of(self, *tokens) -> dict | None:
        for position, cmd in enumerate(self.calls):
            if all(token in cmd for token in tokens):
                return self.envs[position]
        return None


def _ns(action: str, argv: list[str]):
    """Call backend_main exactly as cli.py does, capturing stdout+stderr."""
    argv = [action, *argv]
    saved = sys.argv
    sys.argv = ["shipboard", "backend", *argv]
    try:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = sbb.backend_main(argv)
            except SystemExit as exit_code:  # argparse --help
                code = exit_code.code
        return code, out.getvalue() + err.getvalue()
    finally:
        sys.argv = saved


# --- 1. assets are self-contained -------------------------------------------
assets = sbb._assets_dir()
check("assets ship inside the package", assets.is_dir(), str(assets))
for name in ("docker-compose.yml", "scripts/start_whisper_server.sh",
             "scripts/whisper_wake_proxy.py", "scripts/whisper_idle_stop.sh",
             "systemd/whisper-tailnet-proxy.service", "systemd/whisper-idle-stop.service",
             "systemd/whisper-idle-stop.timer", "systemd/shipboard.service"):
    check(f"asset present: {name}", (assets / name).is_file())

strays = [str(p.relative_to(assets)) for p in assets.rglob("*")
          if p.is_file() and "Coding/shipboard" in p.read_text()]
check("no asset mentions ~/Coding/shipboard", not strays, ", ".join(strays))

compose_text = (assets / "docker-compose.yml").read_text()
mounts = [line.split(":", 1)[0].lstrip("- ").strip()
          for line in compose_text.splitlines() if line.strip().startswith("- ./")]
check("compose bind mount resolves from the packaged dir",
      bool(mounts) and all((assets / m).is_file() for m in mounts), ", ".join(mounts))

units = sorted(p.name for p in (assets / "systemd").glob("*proxy*.service"))
check("exactly one proxy unit ships", units == ["whisper-tailnet-proxy.service"], str(units))

# --- 2. unit rendering -------------------------------------------------------
rendered = sbb.render_unit("whisper-tailnet-proxy.service", {
    "PROXY_HOST": "100.64.0.1", "PROXY_PORT": "10301", "BACKEND_PORT": "10302",
    "PROXY_SCRIPT": str(assets / "scripts" / "whisper_wake_proxy.py"),
})
check("no placeholder survives rendering",
      "@" not in rendered and "WHISPER_PROXY_HOST=100.64.0.1" in rendered
      and "WHISPER_PROXY_PORT=10301" in rendered and "WHISPER_BACKEND_PORT=10302" in rendered)
check("ExecStart points at the installed asset",
      f"ExecStart=/usr/bin/python3 {assets / 'scripts' / 'whisper_wake_proxy.py'}" in rendered)
check("render is deterministic",
      rendered == sbb.render_unit("whisper-tailnet-proxy.service", {
          "PROXY_HOST": "100.64.0.1", "PROXY_PORT": "10301", "BACKEND_PORT": "10302",
          "PROXY_SCRIPT": str(assets / "scripts" / "whisper_wake_proxy.py")}))
check("unit carries no hardcoded home path", "%h/Coding" not in rendered)

idle = sbb.render_unit("whisper-idle-stop.service",
                       {"IDLE_STOP_SCRIPT": str(assets / "scripts" / "whisper_idle_stop.sh")})
check("idle-stop ExecStart is absolute", idle.startswith("[Unit]")
      and f"ExecStart=/usr/bin/bash {assets / 'scripts' / 'whisper_idle_stop.sh'}" in idle)

daemon = sbb.render_unit("shipboard.service", {"SHIPBOARD_BIN": "/opt/venv/bin/shipboard"})
check("daemon unit uses the resolved script path", "ExecStart=/opt/venv/bin/shipboard" in daemon)

# --- 3. TOML upsert preserves the user's file --------------------------------
tmp = Path(tempfile.mkdtemp())
toml_path = tmp / "shipboard.toml"
toml_path.write_text(
    '# my tuning\nwhisper_url = "http://127.0.0.1:10300/inference"  # local\n'
    'whisper_language = "ru"\n\n[[key_bind]]\nkey = "rightalt"\ntoggle = "record"\n')
previous = sbb._set_toml_keys(toml_path, {
    "whisper_url": "http://100.64.0.1:10301/inference",
    "whisper_health_url": "http://100.64.0.1:10301/health",
})
body = toml_path.read_text()
check("existing key rewritten", 'whisper_url = "http://100.64.0.1:10301/inference"' in body)
check("trailing comment survives", "# local" in body)
check("unrelated key untouched", 'whisper_language = "ru"' in body)
check("key_bind table untouched", 'key = "rightalt"' in body and "toggle = \"record\"" in body)
check("header comment survives", body.startswith("# my tuning"))
check("missing key appended", 'whisper_health_url = "http://100.64.0.1:10301/health"' in body)
check("previous value reported", previous["whisper_url"] == "http://127.0.0.1:10300/inference",
      str(previous))

fresh = tmp / "new.toml"
check("creates a config that did not exist",
      sbb._set_toml_keys(fresh, {"whisper_url": "http://127.0.0.1:10301/inference"}) == {}
      and 'whisper_url = "http://127.0.0.1:10301/inference"' in fresh.read_text())

# --- 4. `up` orchestration ---------------------------------------------------
saved_env = dict(os.environ)
os.environ["XDG_CONFIG_HOME"] = str(tmp / "config")
sbb.DEFAULT_CONFIG_PATH = tmp / "config" / "shipboard.toml"

fake = Fake()
sbb._run, sbb._docker = fake, lambda: "/usr/bin/docker"
sbb._tailnet_ip = lambda: "100.64.0.1"
code, out = _ns("up", ["--timeout", "1"])
check("up succeeds on a healthy container", code == 0, out.strip().splitlines()[-1] if out else "")

volume_at = fake.index("volume", "create")
compose_at = fake.index("compose", "up", "-d")
check("volume is created before compose up", 0 <= volume_at < compose_at)
check("compose targets the packaged file",
      str(assets / "docker-compose.yml") in fake.calls[compose_at])
check("compose gets the caller's uid/gid (volume ownership)",
      (fake.env_of("compose", "up") or {}).get("USER_UID") == str(os.getuid()))
check("proxy unit enabled", fake.index("systemctl", "enable", sbb.PROXY_UNIT) > 0)
check("proxy unit restarted (new ExecStart takes effect)",
      fake.index("systemctl", "restart", sbb.PROXY_UNIT) > 0)
check("idle-stop timer started", fake.index("systemctl", "start", sbb.IDLE_STOP_TIMER) > 0)

unit_dir = tmp / "config" / "systemd" / "user"
installed = sorted(p.name for p in unit_dir.glob("*")) if unit_dir.is_dir() else []
check("four units installed, one proxy only",
      installed == ["shipboard.service", sbb.IDLE_STOP_UNIT, sbb.IDLE_STOP_TIMER, sbb.PROXY_UNIT],
      str(installed))
rendered_exec = f"ExecStart=/usr/bin/python3 {assets / 'scripts' / 'whisper_wake_proxy.py'}"
check("installed unit points at the resolved asset, not a checkout path",
      "%h/Coding" not in (unit_dir / sbb.PROXY_UNIT).read_text()
      and rendered_exec in (unit_dir / sbb.PROXY_UNIT).read_text())
check("up writes whisper_url", 'whisper_url = "http://100.64.0.1:10301/inference"'
      in sbb.DEFAULT_CONFIG_PATH.read_text())
check("up writes whisper_health_url", 'whisper_health_url = "http://100.64.0.1:10301/health"'
      in sbb.DEFAULT_CONFIG_PATH.read_text())

# re-running up on an already-correct install is a no-op for the unit files
before = (unit_dir / sbb.PROXY_UNIT).read_text()
fake2 = Fake()
sbb._run = fake2
code2, out2 = _ns("up", ["--timeout", "1"])
check("second up is idempotent", code2 == 0 and "unchanged" in out2
      and (unit_dir / sbb.PROXY_UNIT).read_text() == before)

# loopback fallback when no tailnet
sbb._tailnet_ip = lambda: None
fake3 = Fake()
sbb._run = fake3
code3, out3 = _ns("up", ["--timeout", "1", "--no-config"])
check("no tailnet falls back to loopback",
      code3 == 0 and "http://127.0.0.1:10301" in out3
      and "WHISPER_PROXY_HOST=127.0.0.1" in (unit_dir / sbb.PROXY_UNIT).read_text())
check("--no-config leaves the config alone", "config: skipped" in out3)

# legacy unit from the old install.sh is pruned
legacy = unit_dir / "whisper-wake-proxy.service"
legacy.write_text("[Unit]\n")
fake4 = Fake()
sbb._run = fake4
code4, out4 = _ns("up", ["--timeout", "1"])
check("legacy proxy unit removed", code4 == 0 and not legacy.exists()
      and "whisper-wake-proxy.service: removed" in out4)
check("prune disables it first", fake4.index("systemctl", "disable", "--now",
                                              "whisper-wake-proxy.service") >= 0)

# failing compose is fatal
fake5 = Fake(compose_rc=1)
sbb._run = fake5
code5, out5 = _ns("up", ["--timeout", "1"])
check("compose failure exits non-zero", code5 == 1 and "compose up failed" in out5)
check("compose failure prints container logs", fake5.index("compose", "logs") > 0)

# container that dies instead of becoming healthy
fake6 = Fake(container="exited|unhealthy|137")
sbb._run = fake6
code6, out6 = _ns("up", ["--timeout", "1"])
check("dead container aborts before unit install", code6 == 1
      and "did not become healthy" in out6 and fake6.index("systemctl", "enable") < 0)

# --- 5. `status` failure modes ----------------------------------------------
def _status_case(fake_run, health_url="http://127.0.0.1:10301/health", reachable=True):
    sbb._run = fake_run
    sbb.HEALTH_URL = health_url
    sbb.WHISPER_URL = health_url.replace("/health", "/inference")
    sbb._http_ok = lambda *a, **k: reachable
    return _ns("status", ["--json"])


code, out = _status_case(Fake())
payload = json.loads(out)
check("healthy backend is ready", code == 0 and payload["ready"] is True, out.strip()[:200])
check("status reports model files",
      payload["model"]["present"] and "ggml-large-v3-turbo.bin" in payload["model"]["files"])
check("status reports the proxy unit as active", payload["proxy_unit"]["state"] == "active")
check("status reports the configured url",
      payload["url"]["whisper_health_url"] == "http://127.0.0.1:10301/health" and payload["url"]["reachable"])

code, out = _status_case(Fake(inspect_rc=1))
payload = json.loads(out)
check("missing container is its own failure mode",
      code == 1 and any("does not exist" in p for p in payload["problems"])
      and payload["container"]["state"] == "absent")

code, out = _status_case(Fake(container="exited|unhealthy|137"))
payload = json.loads(out)
check("exited container is its own failure mode",
      code == 1 and any("exit 137" in p for p in payload["problems"]))

code, out = _status_case(Fake(models=[]))
payload = json.loads(out)
check("running container with no model is its own failure mode",
      code == 1 and any("model absent" in p for p in payload["problems"])
      and payload["model"]["present"] is False)

code, out = _status_case(Fake(unit="inactive"), reachable=False)
payload = json.loads(out)
check("inactive proxy is its own failure mode",
      code == 1 and any(sbb.PROXY_UNIT in p for p in payload["problems"])
      and payload["proxy_unit"]["state"] == "inactive")
check("unreachable url names the unit too",
      any("unreachable" in p and "not active" in p for p in payload["problems"]))

code, out = _status_case(Fake(unit="not-installed"), reachable=False)
payload = json.loads(out)
check("absent proxy unit is distinguishable", payload["proxy_unit"]["state"] == "not-installed")

code, out = _status_case(Fake(container="running|starting|0"), reachable=False)
payload = json.loads(out)
check("starting container reports health, not exit",
      any("health starting" in p for p in payload["problems"]))

_status_case(Fake())  # healthy defaults for the human-readable run below
code, out = _ns("status", [])  # human output
check("human status prints all four checks",
      code == 0 and all(word in out for word in ("container", "model", "proxy", "health")),
      f"code={code}")

# docker missing entirely
sbb._run = Fake()
sbb._docker = lambda: None
code, out = _ns("status", ["--json"])
check("docker missing is reported, not raised",
      code == 1 and any("docker not installed" in p for p in json.loads(out)["problems"]))

# --- 6. CLI wiring -----------------------------------------------------------
code, out = _ns("up", ["--help"])
check("backend up exposes the port overrides",
      "--backend-port" in out and "--no-config" in out and "--no-units" in out)
code, out = _ns("status", ["--help"])
check("backend status exposes --json", "--json" in out)

os.environ.clear()
os.environ.update(saved_env)

print()
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("all backend checks passed")