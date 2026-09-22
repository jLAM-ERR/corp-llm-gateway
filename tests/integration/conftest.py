"""Docker plumbing shared by the container tests.

The route-gate suite (``test_route_gate_container.py``) runs the REAL gateway
image on a network with **no route off it** (``docker network create
--internal``), with a stub provider as the only reachable upstream. A request the
gate should have refused therefore cannot quietly reach a real provider: it
either lands in the stub's capture log or it fails.

Docker will not publish a port from a container whose only network is internal,
so one relay container sits on both the default bridge and the internal network
and forwards TCP to the gateway (``route_gate_relay.py``). The gateway itself
never gets a route out.

Every environment dependency (docker daemon, buildable image) skips on a laptop
that lacks it and FAILS where it must run: CI sets ``CORP_REQUIRE_PROXY_CAPTURE=1``
— the same switch ``test_anthropic_oauth_outbound.py`` uses — and
``skip_or_fail`` is the only place either is raised.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest

from corp_llm_gateway.settings import parse_flag

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

REQUIRE_ENV_VAR = "CORP_REQUIRE_PROXY_CAPTURE"
IMAGE_ENV_VAR = "CORP_GATEWAY_IMAGE"

# Built from Dockerfile.gateway when CORP_GATEWAY_IMAGE is unset.
DEFAULT_IMAGE_TAG = "corp-llm-gateway:route-gate-test"
LITELLM_VERSION = "v1.101.0"

GATEWAY_PORT = 4000
STUB_PORT = 8000

STUB_SCRIPT = HERE / "route_gate_stub_provider.py"
RELAY_SCRIPT = HERE / "route_gate_relay.py"

CAPTURE_PREFIX = "@@CAPTURE@@"

BOOT_TIMEOUT_SECONDS = 300


def capture_is_required() -> bool:
    return parse_flag(os.environ.get(REQUIRE_ENV_VAR))


def skip_or_fail(reason: str) -> NoReturn:
    """Skip on a machine that cannot run this, fail where it must run."""
    if capture_is_required():
        pytest.fail(f"{REQUIRE_ENV_VAR} is set but this harness cannot run: {reason}")
    pytest.skip(reason)


def docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def docker_daemon_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return docker("version", "--format", "{{.Server.Version}}", timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.fixture(scope="session")
def gateway_image() -> str:
    """The built gateway image: ``CORP_GATEWAY_IMAGE``, or a local build."""
    if not docker_daemon_ready():
        skip_or_fail("docker daemon not reachable — the route gate is tested on the real image")
    preset = os.environ.get(IMAGE_ENV_VAR)
    if preset:
        if docker("image", "inspect", preset, timeout=60).returncode != 0:
            skip_or_fail(f"{IMAGE_ENV_VAR}={preset} is not present locally")
        return preset
    if docker("image", "inspect", DEFAULT_IMAGE_TAG, timeout=60).returncode == 0:
        return DEFAULT_IMAGE_TAG
    built = docker(
        "build",
        "-f",
        str(ROOT / "Dockerfile.gateway"),
        "--build-arg",
        f"LITELLM_VERSION={LITELLM_VERSION}",
        "--build-arg",
        "NER_PROFILE=base",
        "-t",
        DEFAULT_IMAGE_TAG,
        str(ROOT),
        timeout=3600,
    )
    if built.returncode != 0:
        skip_or_fail(f"cannot build {DEFAULT_IMAGE_TAG}: {built.stderr.strip()[-400:]}")
    return DEFAULT_IMAGE_TAG


@dataclass
class Stack:
    """One running gateway: its containers, and how to read what they saw."""

    base_url: str
    gateway: str
    stub: str
    relay: str
    network: str
    seen: int = field(default=0)

    def gateway_logs(self) -> str:
        result = docker("logs", self.gateway)
        return result.stdout + result.stderr

    def stub_captures(self) -> list[dict[str, Any]]:
        result = docker("logs", self.stub)
        return [
            json.loads(line[len(CAPTURE_PREFIX) :])
            for line in (result.stdout + result.stderr).splitlines()
            if line.startswith(CAPTURE_PREFIX)
        ]

    def mark(self) -> None:
        """Remember how much the stub had seen, for ``new_captures()``."""
        self.seen = len(self.stub_captures())

    def new_captures(self) -> list[dict[str, Any]]:
        return self.stub_captures()[self.seen :]

    def audit_records(self) -> list[dict[str, Any]]:
        records = []
        for line in self.gateway_logs().splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            with contextlib.suppress(ValueError):
                record = json.loads(line)
                if isinstance(record, dict) and "request_id" in record:
                    records.append(record)
        return records

    def metrics(self) -> str:
        return httpx.get(f"{self.base_url}/metrics", timeout=30).text

    def block_count(self, block_reason: str) -> float:
        needle = f'corp_llm_gateway_blocked_requests_total{{block_reason="{block_reason}"}}'
        for line in self.metrics().splitlines():
            if line.startswith(needle):
                return float(line.rsplit(" ", 1)[1])
        return 0.0


def _run_detached(name: str, args: Sequence[str]) -> None:
    result = docker("run", "-d", "--name", name, *args, timeout=300)
    if result.returncode != 0:
        pytest.fail(f"docker run {name} failed:\n{result.stderr}")


def _published_port(name: str, container_port: int) -> int:
    lines = docker("port", name, str(container_port)).stdout.strip().splitlines()
    if not lines:
        pytest.fail(f"no published port for {name}:\n{docker('logs', name).stderr}")
    return int(lines[0].rsplit(":", 1)[1])


def _wait_for_health(stack: Stack) -> None:
    deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        running = docker("inspect", "-f", "{{.State.Running}}", stack.gateway).stdout.strip()
        if running != "true":
            logs = stack.gateway_logs()[-4000:]
            pytest.fail(f"the gateway container exited during boot:\n{logs}")
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(f"{stack.base_url}/healthz/live", timeout=5).status_code == 200:
                return
        time.sleep(1)
    pytest.fail(f"the gateway never became ready:\n{stack.gateway_logs()[-4000:]}")


POSTGRES_IMAGE = "postgres:16-alpine"
POSTGRES_DSN = "postgresql://gw:gw@pg:5432/litellm"


def _start_postgres(name: str, network: str) -> None:
    if docker("image", "inspect", POSTGRES_IMAGE, timeout=60).returncode != 0:
        pulled = docker("pull", POSTGRES_IMAGE, timeout=900)
        if pulled.returncode != 0:
            skip_or_fail(f"cannot obtain {POSTGRES_IMAGE}: {pulled.stderr.strip()[-300:]}")
    _run_detached(
        name,
        [
            "--network",
            network,
            "--network-alias",
            "pg",
            "-e",
            "POSTGRES_USER=gw",
            "-e",
            "POSTGRES_PASSWORD=gw",
            "-e",
            "POSTGRES_DB=litellm",
            POSTGRES_IMAGE,
        ],
    )
    # The entrypoint runs the Prisma schema sequence at import, so the database
    # has to answer before the gateway starts or the boot warns and continues
    # with no schema.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if docker("exec", name, "pg_isready", "-U", "gw", timeout=30).returncode == 0:
            return
        time.sleep(1)
    pytest.fail(f"postgres never became ready:\n{docker('logs', name).stderr[-2000:]}")


@contextlib.contextmanager
def running_stack(
    image: str,
    config_path: Path,
    env: Mapping[str, str],
    *,
    with_postgres: bool = False,
) -> Iterator[Stack]:
    """Stub + gateway on an egress-blocked network, reachable through a relay."""
    suffix = uuid.uuid4().hex[:8]
    network = f"corp-rg-net-{suffix}"
    stub = f"corp-rg-stub-{suffix}"
    gateway = f"corp-rg-gw-{suffix}"
    relay = f"corp-rg-relay-{suffix}"
    postgres = f"corp-rg-pg-{suffix}"
    extra: list[str] = []

    created = docker("network", "create", "--internal", network, timeout=60)
    if created.returncode != 0:
        pytest.fail(f"cannot create the egress-blocked network:\n{created.stderr}")
    try:
        _run_detached(
            stub,
            [
                "--network",
                network,
                "--network-alias",
                "stub",
                "-v",
                f"{STUB_SCRIPT}:/mnt/stub.py:ro",
                "--entrypoint",
                "/app/.venv/bin/python",
                image,
                "/mnt/stub.py",
            ],
        )
        if with_postgres:
            _start_postgres(postgres, network)
            extra = ["-e", f"DATABASE_URL={POSTGRES_DSN}"]
        _run_detached(
            gateway,
            [
                "--network",
                network,
                "--network-alias",
                "gateway",
                "-v",
                f"{config_path}:/etc/litellm/config.yaml:ro",
                *_env_args(env),
                *extra,
                image,
            ],
        )
        _run_detached(
            relay,
            [
                "-p",
                f"127.0.0.1:0:{GATEWAY_PORT}",
                "-v",
                f"{RELAY_SCRIPT}:/mnt/relay.py:ro",
                "-e",
                "RELAY_TARGET_HOST=gateway",
                "-e",
                f"RELAY_TARGET_PORT={GATEWAY_PORT}",
                "--entrypoint",
                "/app/.venv/bin/python",
                image,
                "/mnt/relay.py",
            ],
        )
        connected = docker("network", "connect", network, relay, timeout=60)
        if connected.returncode != 0:
            pytest.fail(f"cannot attach the relay to {network}:\n{connected.stderr}")
        stack = Stack(
            base_url=f"http://127.0.0.1:{_published_port(relay, GATEWAY_PORT)}",
            gateway=gateway,
            stub=stub,
            relay=relay,
            network=network,
        )
        _wait_for_health(stack)
        yield stack
    finally:
        docker("rm", "-f", stub, gateway, relay, postgres, timeout=240)
        docker("network", "rm", network, timeout=60)


@contextlib.contextmanager
def booting_gateway(image: str, config_path: Path | str, env: Mapping[str, str]) -> Iterator[str]:
    """A gateway container started on its own — for the startup negatives."""
    suffix = uuid.uuid4().hex[:8]
    network = f"corp-rg-net-{suffix}"
    name = f"corp-rg-boot-{suffix}"
    docker("network", "create", "--internal", network, timeout=60)
    try:
        args = ["--network", network, *_env_args(env)]
        if Path(config_path).is_file():
            args += ["-v", f"{config_path}:/etc/litellm/config.yaml:ro"]
        _run_detached(name, [*args, image])
        yield name
    finally:
        docker("rm", "-f", name, timeout=120)
        docker("network", "rm", network, timeout=60)


def wait_for_exit(name: str, timeout: float = 120) -> int:
    """The container's exit code, or a failure naming what it printed."""
    result = docker("wait", name, timeout=timeout)
    if result.returncode != 0:
        logs = docker("logs", name)
        pytest.fail(f"container {name} never exited:\n{logs.stdout}{logs.stderr}")
    return int(result.stdout.strip())


def _env_args(env: Mapping[str, str]) -> list[str]:
    args: list[str] = []
    for name, value in env.items():
        args += ["-e", f"{name}={value}"]
    return args
