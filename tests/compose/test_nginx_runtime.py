"""The nginx front door in a real ``nginx:1.27-alpine`` container.

The container is started from the MERGED compose render — its image, entrypoint,
routing argument, bind mounts and tmpfs — with the mount sources re-rooted at a
per-test copy of ``compose/nginx/``. So a wrong mount path in compose fails here,
not just a wrong template: a static test reads host files and cannot see what the
container renders.

Skips without Docker on a laptop and FAILS on CI (``nginx_support.skip_or_fail``).
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping
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
EXIT_TIMEOUT_SECONDS = 60

DOMAIN = "example.test"
GATEWAY_HOST = f"gateway.{DOMAIN}"
LANGFUSE_HOST = f"langfuse.{DOMAIN}"

# A config every row below differs from in exactly one respect.
VALID_BEHIND_PROXY: dict[str, str | None] = {
    "NGINX_TLS_MODE": "behind-proxy",
    "GATEWAY_DOMAIN": DOMAIN,
    "NGINX_TRUSTED_PROXIES": "10.0.0.0/8",
    "LANGFUSE_PUBLIC_URL": f"https://{LANGFUSE_HOST}",
}
TERMINATE: dict[str, str | None] = {
    **VALID_BEHIND_PROXY,
    "NGINX_TLS_MODE": "terminate",
    "NGINX_TLS_CERT": "gateway.crt",
    "NGINX_TLS_KEY": "gateway.key",
}
BOTH_CERTS = {"gateway.crt": b"cert", "gateway.key": b"key"}

UNSET = None

HEALTH_PROBE = ("wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8090/nginx-health")


def docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


# --------------------------------------------------------------------------- #
# the service as compose declares it
# --------------------------------------------------------------------------- #


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


def _spec(service: dict[str, Any], project_dir: Path) -> Spec:
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


@pytest.fixture(scope="module")
def specs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Spec]:
    """routing ("host" / "port") -> the service compose would start for it."""
    if shutil.which("docker") is None or docker("version").returncode != 0:
        skip_or_fail("docker daemon not reachable — the entrypoint runs in the real image")
    require_compose_cli()
    result: dict[str, Spec] = {}
    for profile in PROFILES:
        rendered = render(tmp_path_factory.mktemp(profile), profiles=profile)
        result[ROUTING[profile]] = _spec(rendered.services[profile], rendered.project_dir)
    images = {spec.image for spec in result.values()}
    for image in images:
        if docker("image", "inspect", image).returncode != 0:
            pulled = docker("pull", image, timeout=300)
            if pulled.returncode != 0:
                skip_or_fail(f"cannot pull {image}: {pulled.stderr.strip()[-300:]}")
    return result


@pytest.fixture(scope="module")
def network(specs: dict[str, Spec]) -> Iterator[str]:
    # A user-defined network, as compose gives the service: Docker's DNS at
    # 127.0.0.11 (the resolver line) exists only there.
    name = f"corp-nginx-rt-{uuid.uuid4().hex[:8]}"
    created = docker("network", "create", name)
    if created.returncode != 0:
        skip_or_fail(f"cannot create a docker network: {created.stderr.strip()}")
    yield name
    docker("network", "rm", name)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A per-test copy of compose/nginx/ for the bind mounts to point at."""
    project_dir = tmp_path / "compose"
    shutil.copytree(NGINX_DIR, project_dir / "nginx")
    (project_dir / "nginx" / "certs").mkdir(exist_ok=True)
    return project_dir


def _environment(spec: Spec, overrides: Mapping[str, str | None]) -> dict[str, str]:
    env = {**spec.environment, **overrides}
    return {key: value for key, value in env.items() if value is not None}


def _run_args(
    spec: Spec, project_dir: Path, env: Mapping[str, str | None], routing: str | None
) -> list[str]:
    args = ["--entrypoint", spec.entrypoint[0]]
    for source, target in spec.binds:
        args += ["-v", f"{project_dir / source}:{target}:ro"]
    for path in spec.tmpfs:
        args += ["--tmpfs", path]
    for key, value in _environment(spec, env).items():
        args += ["-e", f"{key}={value}"]
    command = spec.command if routing is None else (routing,)
    return [*args, spec.image, *spec.entrypoint[1:], *command]


def run_to_exit(
    spec: Spec,
    project_dir: Path,
    env: Mapping[str, str | None],
    routing: str | None = None,
) -> subprocess.CompletedProcess[str]:
    name = f"corp-nginx-exit-{uuid.uuid4().hex[:8]}"
    argv = ["run", "--rm", "--name", name, *_run_args(spec, project_dir, env, routing)]
    try:
        return docker(*argv, timeout=EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        docker("rm", "-f", name)
        pytest.fail("the entrypoint did not exit: nginx started on a config it should refuse")


@dataclass
class Running:
    name: str
    ports: dict[int, int] = field(default_factory=dict)

    def exec(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return docker("exec", self.name, *argv)

    def logs(self) -> str:
        result = docker("logs", self.name)
        return result.stdout + result.stderr

    def status(self, port: int, path: str, host: str, method: str = "GET") -> int:
        """The HTTP status, or 444 when nginx closes the connection with no response."""
        url = f"http://127.0.0.1:{self.ports[port]}{path}"
        with httpx.Client(trust_env=False, timeout=10) as client:
            try:
                return client.request(method, url, headers={"Host": host}).status_code
            except (httpx.RemoteProtocolError, httpx.ReadError):
                return 444


@contextlib.contextmanager
def started(
    spec: Spec,
    project_dir: Path,
    network: str,
    env: Mapping[str, str | None],
) -> Iterator[Running]:
    running = Running(f"corp-nginx-up-{uuid.uuid4().hex[:8]}")
    publish = [arg for target in spec.targets for arg in ("-p", f"127.0.0.1::{target}")]
    argv = [
        "run",
        "-d",
        "--name",
        running.name,
        "--network",
        network,
        *publish,
        *_run_args(spec, project_dir, env, None),
    ]
    launched = docker(*argv)
    try:
        assert launched.returncode == 0, launched.stderr
        _wait_until_healthy(running)
        for target in spec.targets:
            mapped = docker("port", running.name, f"{target}/tcp").stdout.split()[0]
            running.ports[target] = int(mapped.rsplit(":", 1)[1])
        yield running
    finally:
        docker("rm", "-f", running.name)


def _wait_until_healthy(running: Running) -> None:
    deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        state = docker("inspect", "-f", "{{.State.Running}}", running.name).stdout.strip()
        if state != "true":
            pytest.fail(f"nginx exited instead of starting:\n{running.logs()}")
        if running.exec(*HEALTH_PROBE).returncode == 0:
            return
        time.sleep(0.2)
    pytest.fail(f"nginx did not answer its health listener in time:\n{running.logs()}")


# --------------------------------------------------------------------------- #
# the entrypoint refuses, with the documented exit code and a named key
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Refusal:
    env: Mapping[str, str | None]
    code: int
    names: tuple[str, ...]
    routing: str = "host"
    certs: Mapping[str, bytes] = field(default_factory=dict)


def _tls_mode(value: str | None) -> Refusal:
    return Refusal(
        {**VALID_BEHIND_PROXY, "NGINX_TLS_MODE": value},
        64,
        ("NGINX_TLS_MODE", "'terminate'", "'behind-proxy'"),
    )


def _terminate(certs: Mapping[str, bytes], key: str, **env: str | None) -> Refusal:
    return Refusal({**TERMINATE, **env}, 65, (key,), certs=certs)


def _trusted(value: str | None, **env: str | None) -> Refusal:
    return Refusal(
        {**VALID_BEHIND_PROXY, "NGINX_TRUSTED_PROXIES": value, **env},
        66,
        ("NGINX_TRUSTED_PROXIES",),
    )


def _bind(value: str) -> Refusal:
    return Refusal({**VALID_BEHIND_PROXY, "NGINX_BIND_ADDR": value}, 68, ("NGINX_BIND_ADDR",))


def _domain(value: str | None) -> Refusal:
    return Refusal({**VALID_BEHIND_PROXY, "GATEWAY_DOMAIN": value}, 64, ("GATEWAY_DOMAIN",))


def _langfuse(value: str | None, routing: str = "host") -> Refusal:
    return Refusal(
        {**VALID_BEHIND_PROXY, "LANGFUSE_PUBLIC_URL": value},
        69,
        ("LANGFUSE_PUBLIC_URL",),
        routing=routing,
    )


REFUSALS = [
    pytest.param(_tls_mode(UNSET), id="tls-mode-unset"),
    pytest.param(_tls_mode(""), id="tls-mode-empty"),
    pytest.param(_tls_mode("Terminate"), id="tls-mode-Terminate"),
    pytest.param(_tls_mode("both"), id="tls-mode-both"),
    pytest.param(_tls_mode("behind-proxy "), id="tls-mode-trailing-space"),
    pytest.param(
        _terminate({"gateway.key": b"key"}, "NGINX_TLS_CERT"), id="terminate-cert-missing"
    ),
    pytest.param(_terminate({"gateway.crt": b"cert"}, "NGINX_TLS_KEY"), id="terminate-key-missing"),
    pytest.param(
        _terminate({"gateway.crt": b"", "gateway.key": b"key"}, "NGINX_TLS_CERT"),
        id="terminate-cert-empty",
    ),
    pytest.param(
        _terminate(BOTH_CERTS, "NGINX_TLS_CERT", NGINX_TLS_CERT="../x.pem"),
        id="terminate-cert-path",
    ),
    pytest.param(
        _terminate(BOTH_CERTS, "NGINX_TLS_CERT", NGINX_TLS_CERT=UNSET),
        id="terminate-cert-unset",
    ),
    pytest.param(
        _terminate(BOTH_CERTS, "NGINX_TLS_KEY", NGINX_TLS_KEY=".."), id="terminate-key-dotdot"
    ),
    pytest.param(
        _terminate(BOTH_CERTS, "NGINX_TLS_CERT", NGINX_TLS_CERT="gateway.crt;"),
        id="terminate-cert-syntax",
    ),
    pytest.param(_trusted(UNSET), id="trusted-unset"),
    pytest.param(_trusted(""), id="trusted-empty"),
    pytest.param(_trusted("   "), id="trusted-blank"),
    pytest.param(_trusted("10.0.0.0/8 banana"), id="trusted-banana"),
    pytest.param(_trusted("0.0.0.0/0"), id="trusted-v4-slash-0"),
    pytest.param(_trusted("::/0"), id="trusted-v6-slash-0"),
    pytest.param(_trusted("0.0.0.0/00"), id="trusted-slash-00"),
    pytest.param(_trusted("0.0.0.0/1 128.0.0.0/1"), id="trusted-two-halves"),
    pytest.param(_trusted("10.0.0.0/8;"), id="trusted-semicolon"),
    pytest.param(_trusted("10.0.0.0/7"), id="trusted-v4-below-floor"),
    pytest.param(_trusted("fd00::/15"), id="trusted-v6-below-floor"),
    pytest.param(_trusted("10.0.0.0/33"), id="trusted-v4-above-32"),
    pytest.param(_trusted("10.0.0.256"), id="trusted-bad-octet"),
    pytest.param(_trusted("10.0.0"), id="trusted-three-octets"),
    pytest.param(_trusted("10.0.0.0/8/8"), id="trusted-two-slashes"),
    pytest.param(_trusted("10.0.0.0/"), id="trusted-empty-prefix"),
    pytest.param(
        Refusal(
            {**TERMINATE, "NGINX_TRUSTED_PROXIES": "0.0.0.0/0"},
            66,
            ("NGINX_TRUSTED_PROXIES",),
            certs=BOTH_CERTS,
        ),
        id="trusted-checked-in-terminate-too",
    ),
    *(
        pytest.param(_bind(value), id=f"bind-{value or 'empty'}")
        for value in (
            "0.0.0.0",
            "0",
            "::",
            "[::]",
            "::0",
            "[::0]",
            "0:0:0:0:0:0:0:0",
            "::ffff:0.0.0.0",
            "[::FFFF:0.0.0.0]",
            "::ffff:0:0",
            "[::ffff:0:0]",
            "0:0:0:0:0:ffff:0:0",
            "::ffff:0000:0000",
            "[::FFFF:0:0]",
            "",
        )
    ),
    pytest.param(_domain("example.test; include /etc/passwd"), id="domain-injection"),
    pytest.param(_domain(UNSET), id="domain-unset"),
    pytest.param(_domain("Example.test"), id="domain-uppercase"),
    pytest.param(_domain("localhost"), id="domain-one-label"),
    pytest.param(_domain("-bad.example.test"), id="domain-leading-hyphen"),
    pytest.param(_domain("a.example.test\ninclude x"), id="domain-newline"),
    pytest.param(_langfuse(f"http://{LANGFUSE_HOST}"), id="langfuse-http"),
    pytest.param(_langfuse(UNSET), id="langfuse-unset"),
    pytest.param(_langfuse("https://"), id="langfuse-no-host"),
    pytest.param(_langfuse("https://langfuse.other.test"), id="langfuse-other-domain"),
    pytest.param(_langfuse(f"https://user@{LANGFUSE_HOST}"), id="langfuse-userinfo"),
    pytest.param(_langfuse(f"https://{LANGFUSE_HOST}.evil.test"), id="langfuse-suffix"),
    pytest.param(_langfuse("http://10.1.2.3:8443", routing="port"), id="langfuse-http-port"),
]


def _assert_one_line_naming(result: subprocess.CompletedProcess[str], names: tuple[str, ...]):
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1, result.stderr
    assert lines[0].startswith("corp-nginx: "), lines[0]
    for name in names:
        assert name in lines[0], (name, lines[0])


@pytest.mark.parametrize("refusal", REFUSALS)
def test_the_entrypoint_refuses(specs: dict[str, Spec], project: Path, refusal: Refusal) -> None:
    for name, content in refusal.certs.items():
        (project / "nginx" / "certs" / name).write_bytes(content)

    result = run_to_exit(specs[refusal.routing], project, refusal.env)

    assert result.returncode == refusal.code, result.stderr
    _assert_one_line_naming(result, refusal.names)


LEAK_CANARY = "LEAK-CANARY-9c1e"


@pytest.mark.parametrize(
    "value",
    [
        f"https://u:{LEAK_CANARY}@{LANGFUSE_HOST}",
        f"http://u:{LEAK_CANARY}@{LANGFUSE_HOST}",
        f"https://langfuse.other.test/cb?token={LEAK_CANARY}",
        f"https://u:{LEAK_CANARY}/x@{LANGFUSE_HOST}",
    ],
    ids=["https-userinfo", "http-userinfo", "query-string", "slash-in-userinfo"],
)
def test_the_langfuse_refusal_does_not_echo_a_credential(
    specs: dict[str, Spec], project: Path, value: str
) -> None:
    result = run_to_exit(
        specs["host"], project, {**VALID_BEHIND_PROXY, "LANGFUSE_PUBLIC_URL": value}
    )

    assert result.returncode == 69, result.stderr
    _assert_one_line_naming(result, ("LANGFUSE_PUBLIC_URL",))
    assert LEAK_CANARY not in result.stdout + result.stderr


@pytest.mark.parametrize("routing", ["hosts", "", "both"])
def test_the_entrypoint_refuses_an_unknown_routing(
    specs: dict[str, Spec], project: Path, routing: str
) -> None:
    result = run_to_exit(specs["host"], project, VALID_BEHIND_PROXY, routing=routing)

    assert result.returncode == 64, result.stderr
    _assert_one_line_naming(result, ("routing", "'host'", "'port'"))


def test_a_placeholder_outside_the_envsubst_list_is_refused(
    specs: dict[str, Spec], project: Path
) -> None:
    template = project / "nginx" / "templates" / "00-http.conf.template"
    template.write_text(
        template.read_text() + 'map $host $corp_probe { default "${NOT_IN_THE_LIST}"; }\n'
    )

    result = run_to_exit(specs["host"], project, VALID_BEHIND_PROXY)

    assert result.returncode == 67, result.stderr
    _assert_one_line_naming(result, ("${NOT_IN_THE_LIST}",))


@pytest.mark.parametrize("routing", ["host", "port"])
def test_a_missing_template_is_refused(specs: dict[str, Spec], project: Path, routing: str) -> None:
    (project / "nginx" / "templates" / "snippets" / "langfuse-locations.inc.template").unlink()

    result = run_to_exit(specs[routing], project, VALID_BEHIND_PROXY)

    assert result.returncode == 67, result.stderr
    _assert_one_line_naming(result, ("langfuse-locations.inc.template",))


# --------------------------------------------------------------------------- #
# valid configs start
# --------------------------------------------------------------------------- #

STARTS = [
    pytest.param("host", {"NGINX_BIND_ADDR": "10.1.2.3"}, id="bind-v4-nic"),
    pytest.param("host", {"NGINX_BIND_ADDR": "[fd00::1]"}, id="bind-v6-nic"),
    pytest.param("host", {"NGINX_BIND_ADDR": "[::ffff:1:0]"}, id="bind-v4-mapped-nonzero"),
    pytest.param(
        "host", {"NGINX_TRUSTED_PROXIES": "10.0.0.0/8 192.168.0.0/16"}, id="trusted-two-cidrs"
    ),
    pytest.param(
        "host", {"NGINX_TRUSTED_PROXIES": "fd00::/16 10.20.0.5 172.16.0.0/12"}, id="trusted-mixed"
    ),
    pytest.param(
        "host",
        {"LANGFUSE_PUBLIC_URL": f"https://{LANGFUSE_HOST.upper()}:8443/"},
        id="langfuse-port-path-case",
    ),
    pytest.param(
        "port",
        {
            "GATEWAY_DOMAIN": "not a domain; include x",
            "LANGFUSE_PUBLIC_URL": "https://10.1.2.3:8443",
        },
        id="port-routing-ignores-the-domain",
    ),
]


@pytest.mark.parametrize(("routing", "env"), STARTS)
def test_a_valid_config_starts(
    specs: dict[str, Spec],
    project: Path,
    network: str,
    routing: str,
    env: dict[str, str],
) -> None:
    with started(specs[routing], project, network, {**VALID_BEHIND_PROXY, **env}) as nginx:
        rendered = nginx.exec("sh", "-c", f"cat {RENDERED}/*.conf {RENDERED}/snippets/*.inc")

        assert rendered.returncode == 0, rendered.stderr
        # A value the routing does not use is never rendered.
        assert "not a domain" not in rendered.stdout


def _runtime_variables(text: str) -> set[str]:
    return set(re.findall(r"\$(?!\{)[a-z_][a-z0-9_]*", text))


RENDERED_FILES = {
    "host": {
        "00-http.conf": "00-http.conf.template",
        "10-behind-proxy.host.conf": "listeners/behind-proxy.host.conf.template",
        "snippets/gateway-locations.inc": "snippets/gateway-locations.inc.template",
        "snippets/langfuse-locations.inc": "snippets/langfuse-locations.inc.template",
    },
    "port": {
        "00-http.conf": "00-http.conf.template",
        "10-behind-proxy.port.conf": "listeners/behind-proxy.port.conf.template",
        "snippets/gateway-locations.inc": "snippets/gateway-locations.inc.template",
        "snippets/langfuse-locations.inc": "snippets/langfuse-locations.inc.template",
    },
}


@pytest.mark.parametrize("routing", ["host", "port"])
def test_a_valid_behind_proxy_config_renders_only_the_design(
    specs: dict[str, Spec], project: Path, network: str, routing: str
) -> None:
    spec = specs[routing]
    with started(spec, project, network, VALID_BEHIND_PROXY) as nginx:
        assert nginx.exec("nginx", "-t").returncode == 0
        # The compose healthcheck itself, as declared.
        assert nginx.exec(*spec.healthcheck).returncode == 0

        listing = nginx.exec("find", RENDERED, "-type", "f").stdout.split()
        assert {path.removeprefix(f"{RENDERED}/") for path in listing} == set(
            RENDERED_FILES[routing]
        )

        leftover = nginx.exec("grep", "-R", "-n", "[$][{]", RENDERED)
        assert leftover.returncode == 1, leftover.stdout

        for rendered_name, template_name in RENDERED_FILES[routing].items():
            template = (NGINX_DIR / "templates" / template_name).read_text()
            rendered = nginx.exec("cat", f"{RENDERED}/{rendered_name}").stdout
            assert _runtime_variables(template) <= _runtime_variables(rendered), rendered_name

        dump = nginx.exec("nginx", "-T")
        assert dump.returncode == 0, dump.stderr
        files = re.findall(r"^# configuration file (\S+):$", dump.stdout, re.MULTILINE)
        assert files[0] == "/etc/nginx/nginx.conf"
        assert all(f == "/etc/nginx/nginx.conf" or f.startswith(f"{RENDERED}/") for f in files)
        # The stock default.conf server: its docroot and its server_name.
        assert "/usr/share/nginx/html" not in dump.stdout
        assert not re.search(r"server_name\s+localhost", dump.stdout)
        # Without one, nginx's compiled-in `combined` log goes to the image's
        # /var/log/nginx/access.log -> /dev/stdout, request line and all.
        directives = re.sub(r"#[^\n]*", "", dump.stdout)
        assert re.findall(r"^\s*access_log\s+([^;]+);", directives, re.MULTILINE) == ["off"]


def test_envsubst_substitutes_only_the_listed_names(
    specs: dict[str, Spec], project: Path, network: str
) -> None:
    # A bare `envsubst` (no list) would blank every nginx runtime variable.
    probe = 'map $host $corp_render_probe { default "$remote_addr ${GATEWAY_DOMAIN}"; }'
    template = project / "nginx" / "templates" / "00-http.conf.template"
    template.write_text(template.read_text() + probe + "\n")

    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        rendered = nginx.exec("cat", f"{RENDERED}/00-http.conf").stdout

    assert 'map $host $corp_render_probe { default "$remote_addr example.test"; }' in rendered


# --------------------------------------------------------------------------- #
# at this stage the front door denies everything
# --------------------------------------------------------------------------- #

PATHS = (
    "/",
    "/v1/messages",
    "/v1/messages?beta=true",
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/models",
    "/healthz/live",
    "/internal/issue-token",
    "/nginx-health",
    "/ui",
    "/key/generate",
    "/v1/some/future/router",
)
UNMATCHED_HOSTS = ("other.example.test", "127.0.0.1", f"{GATEWAY_HOST}.evil.test", DOMAIN)


def test_host_routing_answers_404_for_its_names_and_444_for_any_other(
    specs: dict[str, Spec], project: Path, network: str
) -> None:
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        for host in (GATEWAY_HOST, LANGFUSE_HOST):
            for path in PATHS:
                for method in ("GET", "POST"):
                    assert nginx.status(8080, path, host, method) == 404, (host, method, path)
        for host in UNMATCHED_HOSTS:
            for path in ("/", "/v1/messages"):
                assert nginx.status(8080, path, host, "POST") == 444, (host, path)


def test_a_denied_request_writes_nothing_of_itself_to_the_container_log(
    specs: dict[str, Spec], project: Path, network: str
) -> None:
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        url = f"http://127.0.0.1:{nginx.ports[8080]}/v1/messages?code={LEAK_CANARY}"
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.get(
                url, headers={"Host": GATEWAY_HOST}, auth=("leak-user", LEAK_CANARY)
            )
        assert response.status_code == 404
        logs = nginx.logs()

    assert LEAK_CANARY not in logs
    assert "leak-user" not in logs
    assert "/v1/messages" not in logs


def test_port_routing_answers_404_on_both_ports_whatever_the_host(
    specs: dict[str, Spec], project: Path, network: str
) -> None:
    with started(specs["port"], project, network, VALID_BEHIND_PROXY) as nginx:
        for port in (8080, 8081):
            for host in (GATEWAY_HOST, "other.example.test", "10.1.2.3"):
                for path in PATHS:
                    for method in ("GET", "POST"):
                        assert nginx.status(port, path, host, method) == 404, (
                            port,
                            host,
                            method,
                            path,
                        )
