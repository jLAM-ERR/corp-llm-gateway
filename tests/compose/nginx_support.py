"""Shared plumbing for the nginx front-door tests: the merged compose render and
the skip-or-fail rule.

The render writes ``REQUIRED_ENV`` into the project ``.env`` and strips from the
process environment every name any compose file interpolates
(``bare_compose_env``), so the render is the template's and not this laptop's.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import pytest
import yaml

from corp_llm_gateway.settings import parse_flag

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_DIR = ROOT / "compose"
NGINX_DIR = COMPOSE_DIR / "nginx"
COMPOSE = COMPOSE_DIR / "docker-compose.yml"
OAUTH = COMPOSE_DIR / "docker-compose.oauth.yml"
ISSUANCE = COMPOSE_DIR / "docker-compose.issuance.yml"
CONFIG_EXAMPLE = COMPOSE_DIR / "gateway" / "config.toml.example"

PROFILES = ("nginx", "nginx-ports")
ROUTING = {"nginx": "host", "nginx-ports": "port"}

# Every key the "Rendering design" reference list marks as read by the entrypoint.
ENTRYPOINT_KEYS = (
    "NGINX_TLS_MODE",
    "GATEWAY_DOMAIN",
    "NGINX_TLS_CERT",
    "NGINX_TLS_KEY",
    "NGINX_TRUSTED_PROXIES",
    "NGINX_BIND_ADDR",
    "LANGFUSE_PUBLIC_URL",
    "NGINX_TOKEN_RATE",
    "NGINX_TOKEN_BURST",
    "NGINX_TOKEN_CONN",
    "NGINX_ISSUE_RATE",
)
COMPOSE_ONLY_KEYS = ("NGINX_PORT", "NGINX_LANGFUSE_PORT")

MODE_A_ONLY_KEYS = ("LITELLM_MASTER_KEY", "UI_USERNAME", "UI_PASSWORD")
# Every `${X:?...}` in the base file, minus the Mode A keys the subscription mode
# omits. Values are obvious non-credentials; only their presence in the render matters.
REQUIRED_ENV = (
    "POSTGRES_PASSWORD",
    "GATEWAY_IMAGE_TAG",
    "CORP_LANGFUSE_PUBLIC_KEY",
    "CORP_LANGFUSE_SECRET_KEY",
    "LANGFUSE_CLICKHOUSE_PASSWORD",
    "LANGFUSE_ENCRYPTION_KEY",
    "LANGFUSE_NEXTAUTH_SECRET",
    "LANGFUSE_POSTGRES_PASSWORD",
    "LANGFUSE_SALT",
    "MINIO_ROOT_PASSWORD",
)

REQUIRE_ENV_VARS = ("CI", "CORP_REQUIRE_PROXY_CAPTURE")


def must_run() -> bool:
    return any(parse_flag(os.environ.get(name)) for name in REQUIRE_ENV_VARS)


def skip_or_fail(reason: str) -> NoReturn:
    """Skip on a laptop that cannot run this, fail on CI where it must run."""
    if must_run():
        pytest.fail(f"CI is set but the nginx front-door tests cannot run: {reason}")
    pytest.skip(reason)


def compose_cli_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    return probe.returncode == 0


def require_compose_cli() -> None:
    if not compose_cli_available():
        skip_or_fail("docker compose CLI not on PATH")


def _interpolated_names() -> set[str]:
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
    names: set[str] = set()
    for path in (COMPOSE, OAUTH, ISSUANCE):
        names |= set(pattern.findall(path.read_text()))
    return names


def bare_compose_env() -> dict[str, str]:
    """The process environment minus everything a compose file could read from it."""
    stripped = _interpolated_names() | set(MODE_A_ONLY_KEYS) | set(ENTRYPOINT_KEYS)
    return {
        key: value
        for key, value in os.environ.items()
        if key not in stripped
        and not key.startswith("COMPOSE_")
        and not key.startswith("CORP_GATEWAY_ISSUE_")
    }


@dataclass
class Render:
    project_dir: Path
    returncode: int
    stdout: str
    stderr: str

    @property
    def doc(self) -> dict[str, Any]:
        return yaml.safe_load(self.stdout)

    @property
    def services(self) -> dict[str, Any]:
        return self.doc["services"]


def render(
    tmp_path: Path,
    *files: Path,
    profiles: str | None = None,
    env_extra: str = "",
    check: bool = True,
) -> Render:
    """``docker compose config`` on a copy of ``compose/``; COMPOSE_PROFILES comes from
    the project ``.env``, which is how the server selects the front door."""
    project_dir = tmp_path / "compose"
    shutil.copytree(COMPOSE_DIR, project_dir)
    if ISSUANCE in files:
        shutil.copy(CONFIG_EXAMPLE, project_dir / "gateway" / "config.toml")
    env_text = "".join(f"{key}=render-fixture\n" for key in REQUIRED_ENV)
    if profiles is not None:
        env_text += f"COMPOSE_PROFILES={profiles}\n"
    (project_dir / ".env").write_text(env_text + env_extra)
    argv = ["docker", "compose", "--project-name", "corp-nginx-render"]
    for path in files or (COMPOSE,):
        argv += ["-f", path.name]
    argv.append("config")
    result = subprocess.run(
        argv, cwd=project_dir, capture_output=True, text=True, env=bare_compose_env(), check=False
    )
    if check and result.returncode != 0:
        pytest.fail(f"docker compose config failed (exit {result.returncode}):\n{result.stderr}")
    return Render(project_dir.resolve(), result.returncode, result.stdout, result.stderr)


# `corp-llm-gateway status` as a developer runs it, one process per probe, with
# one name resolved to loopback in place of DNS.
STATUS_CLI = """
import socket
import sys

real_getaddrinfo = socket.getaddrinfo


def resolving(host, *args, **kwargs):
    return real_getaddrinfo("127.0.0.1" if host == sys.argv[1] else host, *args, **kwargs)


socket.getaddrinfo = resolving
from corp_llm_gateway.cli.status import main

sys.exit(main(sys.argv[2:]))
"""


def run_status_cli(
    gateway_url: str, home: Path, *, resolve: str, ca: Path | None = None
) -> tuple[int, dict[str, Any]]:
    """The CLI's exit code and ``--json`` report for the install under ``home``.
    ``ca`` is trusted through ``SSL_CERT_FILE``; no proxy is used."""
    install_dir = home / ".corp-llm-gateway"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "PYTHONPATH": str(ROOT / "src"),
        "NO_PROXY": "*",
        "no_proxy": "*",
    }
    if ca is not None:
        env["SSL_CERT_FILE"] = str(ca)
    argv = [
        "--gateway-url",
        gateway_url,
        "--token-file",
        str(install_dir / "token"),
        "--version-file",
        str(install_dir / "VERSION"),
        "--json",
    ]
    ran = subprocess.run(
        [sys.executable, "-c", STATUS_CLI, resolve, *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert ran.stdout, ran.stderr
    return ran.returncode, json.loads(ran.stdout)
