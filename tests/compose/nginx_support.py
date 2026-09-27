"""Shared plumbing for the nginx front-door tests: the merged compose render and
the skip-or-fail rule.

The render reuses the env fixture of the existing render tests
(``test_oauth_overlay.REQUIRED_ENV``) and strips from the process environment
every name any compose file interpolates, so the render is the template's and
not this laptop's.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import pytest
import yaml

from corp_llm_gateway.settings import parse_flag
from tests.compose.test_oauth_overlay import MODE_A_ONLY_KEYS, REQUIRED_ENV

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
    stripped = _interpolated_names() | set(MODE_A_ONLY_KEYS) | set(ENTRYPOINT_KEYS)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in stripped
        and not key.startswith("COMPOSE_")
        and not key.startswith("CORP_GATEWAY_ISSUE_")
    }
    argv = ["docker", "compose", "--project-name", "corp-nginx-render"]
    for path in files or (COMPOSE,):
        argv += ["-f", path.name]
    argv.append("config")
    result = subprocess.run(
        argv, cwd=project_dir, capture_output=True, text=True, env=env, check=False
    )
    if check and result.returncode != 0:
        pytest.fail(f"docker compose config failed (exit {result.returncode}):\n{result.stderr}")
    return Render(project_dir.resolve(), result.returncode, result.stdout, result.stderr)
