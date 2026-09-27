"""An nginx front-door container as compose declares it, for every test that runs
one: tests/compose/test_nginx_runtime.py against a stub upstream, and
tests/integration/test_nginx_allowlist_image.py in front of the real gateway.

The service is read from the MERGED compose render — image, entrypoint, routing
argument, bind mounts, tmpfs — with the mount sources re-rooted at a per-test copy
of compose/nginx/. Skips without Docker on a laptop and FAILS on CI
(nginx_support.skip_or_fail).
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.compose.nginx_support import (
    NGINX_DIR,
    PROFILES,
    ROUTING,
    render,
    require_compose_cli,
    skip_or_fail,
)

RENDERED = "/etc/nginx/rendered"
BOOT_TIMEOUT_SECONDS = 30

HEALTH_PROBE = ("wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8090/nginx-health")


def docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


@dataclass(frozen=True)
class Spec:
    image: str
    entrypoint: tuple[str, ...]
    command: tuple[str, ...]
    binds: tuple[tuple[str, str], ...]
    tmpfs: tuple[str, ...]
    environment: Mapping[str, str]
    healthcheck: tuple[str, ...]
    targets: tuple[int, ...]


def spec_of(service: dict[str, Any], project_dir: Path) -> Spec:
    binds = []
    for volume in service["volumes"]:
        assert volume["type"] == "bind" and volume["read_only"] is True, volume
        source = Path(volume["source"]).resolve().relative_to(project_dir).as_posix()
        binds.append((source, volume["target"]))
    return Spec(
        image=service["image"],
        entrypoint=tuple(service["entrypoint"]),
        command=tuple(service["command"]),
        binds=tuple(binds),
        tmpfs=tuple(service["tmpfs"]),
        environment=dict(service["environment"]),
        healthcheck=tuple(service["healthcheck"]["test"][1:]),
        targets=tuple(port["target"] for port in service["ports"]),
    )


@dataclass(frozen=True)
class Network:
    name: str
    # The peer address nginx sees ($realip_remote_addr) for a request this
    # test process sends to a published port: the network's gateway.
    client_peer: str


def pull_if_missing(image: str) -> None:
    if docker("image", "inspect", image).returncode != 0:
        pulled = docker("pull", image, timeout=300)
        if pulled.returncode != 0:
            skip_or_fail(f"cannot pull {image}: {pulled.stderr.strip()[-300:]}")


def environment_of(spec: Spec, overrides: Mapping[str, str | None]) -> dict[str, str]:
    env = {**spec.environment, **overrides}
    return {key: value for key, value in env.items() if value is not None}


def run_args(
    spec: Spec, project_dir: Path, env: Mapping[str, str | None], routing: str | None
) -> list[str]:
    args = ["--entrypoint", spec.entrypoint[0]]
    for source, target in spec.binds:
        args += ["-v", f"{project_dir / source}:{target}:ro"]
    for path in spec.tmpfs:
        args += ["--tmpfs", path]
    for key, value in environment_of(spec, env).items():
        args += ["-e", f"{key}={value}"]
    command = spec.command if routing is None else (routing,)
    return [*args, spec.image, *spec.entrypoint[1:], *command]


@dataclass
class Running:
    name: str
    ports: dict[int, int] = field(default_factory=dict)

    def exec(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return docker("exec", self.name, *argv)

    def logs(self) -> str:
        stdout, stderr = self.log_streams()
        return stdout + stderr

    def log_streams(self) -> tuple[str, str]:
        result = docker("logs", self.name)
        return result.stdout, result.stderr

    def access_log(self, expected: int) -> list[dict[str, str]]:
        """The access-log entries for the published listeners (not the loopback
        health probes), once ``expected`` of them have been written."""
        deadline = time.monotonic() + 10
        while True:
            entries = [
                entry
                for entry in access_entries(self.log_streams()[0])
                if entry["server_port"] != "8090"
            ]
            if len(entries) >= expected or time.monotonic() > deadline:
                return entries
            time.sleep(0.2)

    def status(
        self,
        port: int,
        path: str,
        host: str,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> int:
        """The HTTP status, or 444 when nginx closes the connection with no response."""
        url = f"http://127.0.0.1:{self.ports[port]}{path}"
        with httpx.Client(trust_env=False, timeout=30) as client:
            try:
                return client.request(
                    method, url, headers={"Host": host, **(headers or {})}, content=content
                ).status_code
            except (httpx.RemoteProtocolError, httpx.ReadError):
                return 444


def corp_gate_fields() -> set[str]:
    template = (NGINX_DIR / "templates" / "00-http.conf.template").read_text()
    return set(re.findall(r'"([a-z0-9_]+)":"\$', template))


def access_entries(stdout: str) -> list[dict[str, str]]:
    """Every stdout line, each of which must be one corp_gate access-log entry."""
    fields = corp_gate_fields()
    entries = []
    for line in stdout.splitlines():
        entry = json.loads(line)
        assert set(entry) == fields, line
        entries.append(entry)
    return entries


@contextlib.contextmanager
def started(
    spec: Spec,
    project_dir: Path,
    network: Network,
    env: Mapping[str, str | None],
    *,
    trust_client: bool = True,
    aliases: Sequence[str] = (),
) -> Iterator[Running]:
    """nginx as compose would start it. In behind-proxy every peer outside
    NGINX_TRUSTED_PROXIES gets a 444, so this test process's peer address is
    added to the list unless ``trust_client`` is False. ``aliases`` are extra
    names for it on ``network``."""
    running = Running(f"corp-nginx-up-{uuid.uuid4().hex[:8]}")
    if trust_client and env.get("NGINX_TLS_MODE") == "behind-proxy":
        listed = env.get("NGINX_TRUSTED_PROXIES") or ""
        env = {**env, "NGINX_TRUSTED_PROXIES": f"{listed} {network.client_peer}".strip()}
    publish = [arg for target in spec.targets for arg in ("-p", f"127.0.0.1::{target}")]
    argv = [
        "run",
        "-d",
        "--name",
        running.name,
        "--network",
        network.name,
        *(arg for alias in aliases for arg in ("--network-alias", alias)),
        *publish,
        *run_args(spec, project_dir, env, None),
    ]
    launched = docker(*argv)
    try:
        assert launched.returncode == 0, launched.stderr
        wait_until_healthy(running)
        for target in spec.targets:
            mapped = docker("port", running.name, f"{target}/tcp").stdout.split()[0]
            running.ports[target] = int(mapped.rsplit(":", 1)[1])
        yield running
    finally:
        docker("rm", "-f", running.name)


def wait_until_healthy(running: Running) -> None:
    deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        state = docker("inspect", "-f", "{{.State.Running}}", running.name).stdout.strip()
        if state != "true":
            pytest.fail(f"nginx exited instead of starting:\n{running.logs()}")
        if running.exec(*HEALTH_PROBE).returncode == 0:
            return
        time.sleep(0.2)
    pytest.fail(f"nginx did not answer its health listener in time:\n{running.logs()}")


def nginx_specs(tmp_path_factory: pytest.TempPathFactory, *files: Path) -> dict[str, Spec]:
    """routing ("host" / "port") -> the service compose would start for it, from
    the merged render of files (the base compose file when none are named)."""
    if shutil.which("docker") is None or docker("version").returncode != 0:
        skip_or_fail("docker daemon not reachable — the entrypoint runs in the real image")
    require_compose_cli()
    result: dict[str, Spec] = {}
    for profile in PROFILES:
        rendered = render(tmp_path_factory.mktemp(profile), *files, profiles=profile)
        result[ROUTING[profile]] = spec_of(rendered.services[profile], rendered.project_dir)
    for image in {spec.image for spec in result.values()}:
        pull_if_missing(image)
    return result


@contextlib.contextmanager
def user_network(image: str) -> Iterator[Network]:
    """A user-defined network, as compose gives the service: Docker's DNS at
    127.0.0.11 (the resolver line) exists only there."""
    name = f"corp-nginx-rt-{uuid.uuid4().hex[:8]}"
    created = docker("network", "create", name)
    if created.returncode != 0:
        skip_or_fail(f"cannot create a docker network: {created.stderr.strip()}")
    try:
        # From the container's side: its default route is the address a
        # published-port connection arrives from.
        routes = docker("run", "--rm", "--network", name, "--entrypoint", "ip", image, "route")
        via = re.search(r"^default via (\S+)", routes.stdout, re.MULTILINE)
        assert via, f"no default route in the container:\n{routes.stdout}{routes.stderr}"
        yield Network(name, via.group(1))
    finally:
        docker("network", "rm", name)


def copy_nginx_dir(tmp_path: Path) -> Path:
    """A per-test copy of compose/nginx/ for the bind mounts to point at."""
    project_dir = tmp_path / "compose"
    shutil.copytree(NGINX_DIR, project_dir / "nginx")
    (project_dir / "nginx" / "certs").mkdir(exist_ok=True)
    return project_dir
