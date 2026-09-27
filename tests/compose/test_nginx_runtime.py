"""The nginx front door in a real ``nginx:1.27-alpine`` container.

The container is started from the MERGED compose render — its image, entrypoint,
routing argument, bind mounts and tmpfs — with the mount sources re-rooted at a
per-test copy of ``compose/nginx/`` (``nginx_container.py``). So a wrong mount path
in compose fails here, not just a wrong template: a static test reads host files
and cannot see what the container renders.

The tracked gateway snippet proxies to ``http://litellm:4000``; here the stub
upstream answers as ``litellm`` on the per-test network.

Skips without Docker on a laptop and FAILS on CI (``nginx_support.skip_or_fail``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.compose.nginx_allowlist import declared_pairs, gateway_snippet, parse_snippet
from tests.compose.nginx_container import (
    HEALTH_PROBE,
    RENDERED,
    Network,
    Running,
    Spec,
    access_entries,
    copy_nginx_dir,
    docker,
    nginx_specs,
    pull_if_missing,
    run_args,
    started,
    user_network,
)
from tests.compose.nginx_support import COMPOSE, NGINX_DIR, OAUTH

FIXTURES = Path(__file__).resolve().parent / "nginx_fixtures"
TEST_ONLY_PROXY_SNIPPET = FIXTURES / "test-only-proxy-locations.inc.template"
STUB_UPSTREAM_SCRIPT = FIXTURES / "stub_upstream.py"
STUB_IMAGE = "python:3.12-slim"
STUB_ALIAS = "stub-upstream"
# What the tracked gateway snippet proxies to: http://litellm:4000.
GATEWAY_ALIAS = "litellm"

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


# --------------------------------------------------------------------------- #
# the service as compose declares it
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def specs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Spec]:
    """routing ("host" / "port") -> the service compose would start for it."""
    return nginx_specs(tmp_path_factory)


@pytest.fixture(scope="module")
def oauth_specs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Spec]:
    """The same, from the render with the OAuth overlay — the production mode."""
    return nginx_specs(tmp_path_factory, COMPOSE, OAUTH)


@pytest.fixture(scope="module")
def network(specs: dict[str, Spec]) -> Iterator[Network]:
    with user_network(specs["host"].image) as created:
        yield created


@dataclass(frozen=True)
class Stub:
    name: str

    def hits(self) -> int:
        result = docker("logs", self.name)
        return (result.stdout + result.stderr).count("stub-hit")

    def requests(self) -> list[dict[str, Any]]:
        """What reached the ``litellm`` stand-in (:4000), oldest first."""
        result = docker("logs", self.name)
        return [
            json.loads(line.removeprefix("stub-hit "))
            for line in result.stdout.splitlines()
            if line.startswith("stub-hit {")
        ]

    def requests_since(self, seen: int) -> list[dict[str, Any]]:
        return self.requests()[seen:]


@contextlib.contextmanager
def launched_stub(network: Network) -> Iterator[Stub]:
    """200 on :8000, refused on :8001, never answers on :8002 — reachable as
    ``stub-upstream`` from nginx on the same network — and the ``litellm``
    stand-in on :4000, where the tracked gateway snippet proxies."""
    pull_if_missing(STUB_IMAGE)
    stub = Stub(f"corp-nginx-stub-{uuid.uuid4().hex[:8]}")
    launched = docker(
        "run",
        "-d",
        "--name",
        stub.name,
        "--network",
        network.name,
        "--network-alias",
        STUB_ALIAS,
        "--network-alias",
        GATEWAY_ALIAS,
        "-v",
        f"{STUB_UPSTREAM_SCRIPT}:/stub/stub_upstream.py:ro",
        STUB_IMAGE,
        "python",
        "-u",
        "/stub/stub_upstream.py",
    )
    try:
        assert launched.returncode == 0, launched.stderr
        deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
        while "stub-ready" not in docker("logs", stub.name).stdout:
            assert time.monotonic() < deadline, docker("logs", stub.name).stderr
            time.sleep(0.2)
        yield stub
    finally:
        docker("rm", "-f", stub.name)


@pytest.fixture(scope="module")
def stub_upstream(network: Network) -> Iterator[Stub]:
    with launched_stub(network) as stub:
        yield stub


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A per-test copy of compose/nginx/ for the bind mounts to point at."""
    return copy_nginx_dir(tmp_path)


def inject_test_only_proxy(project_dir: Path) -> None:
    """Replace the gateway snippet in the per-test copy — never the tracked one —
    with locations that proxy to the stub upstream."""
    target = project_dir / "nginx" / "templates" / "snippets" / "gateway-locations.inc.template"
    shutil.copy(TEST_ONLY_PROXY_SNIPPET, target)


def run_to_exit(
    spec: Spec,
    project_dir: Path,
    env: Mapping[str, str | None],
    routing: str | None = None,
) -> subprocess.CompletedProcess[str]:
    name = f"corp-nginx-exit-{uuid.uuid4().hex[:8]}"
    argv = ["run", "--rm", "--name", name, *run_args(spec, project_dir, env, routing)]
    try:
        return docker(*argv, timeout=EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        docker("rm", "-f", name)
        pytest.fail("the entrypoint did not exit: nginx started on a config it should refuse")


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
    # The listeners are IPv4: an IPv6-only list would 444 every peer.
    pytest.param(
        Refusal(
            {**VALID_BEHIND_PROXY, "NGINX_TRUSTED_PROXIES": "fd00::1"},
            66,
            ("NGINX_TRUSTED_PROXIES", "IPv4"),
        ),
        id="trusted-v6-only",
    ),
    pytest.param(
        Refusal(
            {**VALID_BEHIND_PROXY, "NGINX_TRUSTED_PROXIES": "fd00::1 fd00::/16"},
            66,
            ("NGINX_TRUSTED_PROXIES", "IPv4"),
        ),
        id="trusted-v6-only-two",
    ),
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
        f"https://langfuse.other.test/cb?email=alice@{LEAK_CANARY}.example",
        f"https://langfuse.other.test#x@{LEAK_CANARY}",
    ],
    ids=[
        "https-userinfo",
        "http-userinfo",
        "query-string",
        "slash-in-userinfo",
        "at-in-query",
        "at-in-fragment",
    ],
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
    network: Network,
    routing: str,
    env: dict[str, str],
) -> None:
    with started(specs[routing], project, network, {**VALID_BEHIND_PROXY, **env}) as nginx:
        rendered = nginx.exec("sh", "-c", f"cat {RENDERED}/*.conf {RENDERED}/snippets/*.inc")

        assert rendered.returncode == 0, rendered.stderr
        # A value the routing does not use is never rendered.
        assert "not a domain" not in rendered.stdout


@pytest.mark.parametrize(
    "trusted",
    ["fd00::1 10.0.0.0/8", "::ffff:10.1.2.3", "::FFFF:10.1.2.3"],
    ids=["v6-beside-v4", "v4-mapped", "v4-mapped-upper"],
)
def test_a_trusted_list_with_an_ipv4_entry_starts(
    specs: dict[str, Spec], project: Path, network: Network, trusted: str
) -> None:
    # trust_client=False: the test client's own IPv4 address must not be what
    # satisfies the at-least-one-IPv4 rule.
    env = {**VALID_BEHIND_PROXY, "NGINX_TRUSTED_PROXIES": trusted}
    with started(specs["host"], project, network, env, trust_client=False) as nginx:
        dump = nginx.exec("nginx", "-T")

    assert dump.returncode == 0, dump.stderr
    directives = re.sub(r"#[^\n]*", "", dump.stdout)
    assert _block(directives, "geo $realip_remote_addr $from_trusted_proxy") == [
        "default 0;",
        *(f"{entry} 1;" for entry in trusted.split()),
    ]


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
    specs: dict[str, Spec], project: Path, network: Network, routing: str
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
        # Exactly one, and ours: any other would replace it for its scope, and with
        # none nginx logs its compiled-in `combined`, request line and all.
        directives = re.sub(r"#[^\n]*", "", dump.stdout)
        assert re.findall(r"^\s*access_log\s+([^;]+);", directives, re.MULTILINE) == [
            "/dev/stdout corp_gate if=$corp_loggable"
        ]
        assert re.findall(r"^\s*error_log\s+([^;]+);", directives, re.MULTILINE) == [
            "/dev/stderr crit"
        ]


def test_envsubst_substitutes_only_the_listed_names(
    specs: dict[str, Spec], project: Path, network: Network
) -> None:
    # A bare `envsubst` (no list) would blank every nginx runtime variable.
    probe = 'map $host $corp_render_probe { default "$remote_addr ${GATEWAY_DOMAIN}"; }'
    template = project / "nginx" / "templates" / "00-http.conf.template"
    template.write_text(template.read_text() + probe + "\n")

    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        rendered = nginx.exec("cat", f"{RENDERED}/00-http.conf").stdout

    assert 'map $host $corp_render_probe { default "$remote_addr example.test"; }' in rendered


# --------------------------------------------------------------------------- #
# routing: the gateway name serves the allow-list, Langfuse still 404s
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

DECLARED = declared_pairs(gateway_snippet())
ADMITTED_PATHS = {path for _, path in DECLARED}


def _gateway_status(method: str, path: str) -> int:
    """What the gateway origin answers: the stand-in's 200 for an admitted pair
    (``limit_except GET`` lets HEAD through too), 403 for another method at an
    admitted path, 404 for everything else."""
    bare = path.split("?", 1)[0]
    if bare not in ADMITTED_PATHS:
        return 404
    passing = DECLARED | {("HEAD", p) for m, p in DECLARED if m == "GET"}
    return 200 if (method, bare) in passing else 403


def test_host_routing_serves_the_allow_list_on_its_gateway_name_and_444s_any_other(
    specs: dict[str, Spec], project: Path, network: Network, stub_upstream: Stub
) -> None:
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        for path in PATHS:
            for method in ("GET", "POST"):
                assert nginx.status(8080, path, GATEWAY_HOST, method) == _gateway_status(
                    method, path
                ), (method, path)
                assert nginx.status(8080, path, LANGFUSE_HOST, method) == 404, (method, path)
        for host in UNMATCHED_HOSTS:
            for path in ("/", "/v1/messages"):
                assert nginx.status(8080, path, host, "POST") == 444, (host, path)


@pytest.mark.parametrize(
    ("path", "status"),
    [("/v1/messages", 403), ("/v1/messages/count_tokens", 404)],
    ids=["refused-method", "refused-path"],
)
def test_a_denied_request_writes_nothing_of_itself_to_the_container_log(
    specs: dict[str, Spec], project: Path, network: Network, path: str, status: int
) -> None:
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        url = f"http://127.0.0.1:{nginx.ports[8080]}{path}?code={LEAK_CANARY}"
        with httpx.Client(trust_env=False, timeout=10) as client:
            response = client.get(
                url, headers={"Host": GATEWAY_HOST}, auth=("leak-user", LEAK_CANARY)
            )
        assert response.status_code == status
        entries = nginx.access_log(expected=1)
        logs = nginx.logs()

    assert LEAK_CANARY not in logs
    assert "leak-user" not in logs
    # The path is logged — as the normalized $uri, once, in the corp_gate line.
    assert [(e["uri"], e["status"]) for e in entries] == [(path, str(status))]
    assert logs.count(path) == 1


def test_port_routing_serves_the_allow_list_on_8080_and_404s_8081_whatever_the_host(
    specs: dict[str, Spec], project: Path, network: Network, stub_upstream: Stub
) -> None:
    with started(specs["port"], project, network, VALID_BEHIND_PROXY) as nginx:
        for host in (GATEWAY_HOST, "other.example.test", "10.1.2.3"):
            for path in PATHS:
                for method in ("GET", "POST"):
                    assert nginx.status(8080, path, host, method) == _gateway_status(
                        method, path
                    ), (host, method, path)
                    assert nginx.status(8081, path, host, method) == 404, (host, method, path)


# --------------------------------------------------------------------------- #
# the http context: real_ip, the log gate, the trusted-peer gate
# --------------------------------------------------------------------------- #


def _block(dump: str, opener: str) -> list[str]:
    body = re.search(rf"{re.escape(opener)}\s*\{{([^{{}}]*)\}}", dump)
    assert body, opener
    return [line.strip() for line in body.group(1).splitlines() if line.strip()]


def test_the_trusted_list_renders_one_real_ip_line_and_one_geo_entry_per_entry(
    specs: dict[str, Spec], project: Path, network: Network
) -> None:
    env = {**VALID_BEHIND_PROXY, "NGINX_TRUSTED_PROXIES": "10.0.0.0/8 192.168.0.0/16"}
    with started(specs["host"], project, network, env, trust_client=False) as nginx:
        dump = nginx.exec("nginx", "-T")

    assert dump.returncode == 0, dump.stderr
    directives = re.sub(r"#[^\n]*", "", dump.stdout)
    assert re.findall(r"^\s*set_real_ip_from\s+([^;]+);", directives, re.MULTILINE) == [
        "10.0.0.0/8",
        "192.168.0.0/16",
    ]
    assert re.findall(r"^\s*real_ip_header\s+([^;]+);", directives, re.MULTILINE) == [
        "X-Forwarded-For"
    ]
    assert _block(directives, "geo $realip_remote_addr $from_trusted_proxy") == [
        "default 0;",
        "10.0.0.0/8 1;",
        "192.168.0.0/16 1;",
    ]


HEALTH_PROBES = 5


def test_the_health_probe_writes_no_access_entry(
    specs: dict[str, Spec], project: Path, network: Network
) -> None:
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        for _ in range(HEALTH_PROBES):
            assert nginx.exec(*HEALTH_PROBE).returncode == 0
        assert nginx.status(8080, "/ui", GATEWAY_HOST) == 404
        nginx.access_log(expected=1)
        entries = access_entries(nginx.log_streams()[0])

    assert [(e["server_port"], e["uri"], e["status"]) for e in entries] == [("8080", "/ui", "404")]


PROXY_CANARY = "LEAK-CANARY-7f3a"
AUTHORIZATION = "Bearer sk-ant-api03-LEAK-AUTH-5d2b"
CORP_AUTH = "corp-LEAK-TOKEN-8e4a"
SECRETS = (PROXY_CANARY, AUTHORIZATION, "LEAK-AUTH-5d2b", "LEAK-TOKEN-8e4a")
CREDENTIALS = {"Authorization": AUTHORIZATION, "X-Corp-Auth": CORP_AUTH}


def _assert_no_secret_in(nginx: Running) -> None:
    stdout, stderr = nginx.log_streams()
    for secret in SECRETS:
        assert secret not in stdout, secret
        assert secret not in stderr, secret


@pytest.mark.parametrize("routing", ["host", "port"])
def test_a_proxied_success_logs_one_corp_gate_line_and_no_secret(
    specs: dict[str, Spec],
    project: Path,
    network: Network,
    stub_upstream: Stub,
    routing: str,
) -> None:
    inject_test_only_proxy(project)
    hits = stub_upstream.hits()
    with started(specs[routing], project, network, VALID_BEHIND_PROXY) as nginx:
        status = nginx.status(
            8080, f"/stub/ok?code={PROXY_CANARY}", GATEWAY_HOST, headers=CREDENTIALS
        )
        entries = nginx.access_log(expected=1)
        _assert_no_secret_in(nginx)

    assert status == 200
    assert stub_upstream.hits() == hits + 1
    assert len(entries) == 1, entries
    entry = entries[0]
    assert (entry["uri"], entry["status"], entry["upstream_status"]) == ("/stub/ok", "200", "200")
    assert entry["upstream_response_time"] not in ("", "-")
    assert (entry["realip_remote_addr"], entry["from_trusted_proxy"]) == (network.client_peer, "1")


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/stub/refused", "502"), ("/stub/slow", "504")],
    ids=["upstream-refuses", "upstream-times-out"],
)
def test_a_failed_upstream_logs_its_status_and_no_secret(
    specs: dict[str, Spec],
    project: Path,
    network: Network,
    stub_upstream: Stub,
    path: str,
    expected: str,
) -> None:
    # At error/warn nginx would write the request line and the upstream URL,
    # query string included, to stderr for exactly these two failures.
    inject_test_only_proxy(project)
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        status = nginx.status(
            8080, f"{path}?code={PROXY_CANARY}", GATEWAY_HOST, headers=CREDENTIALS
        )
        entries = nginx.access_log(expected=1)
        _assert_no_secret_in(nginx)

    assert str(status) == expected
    assert [(e["uri"], e["status"]) for e in entries] == [(path, expected)]
    assert entries[0]["upstream_status"] == expected


UNTRUSTED_ONLY = "192.0.2.1"
# (port, path, host) per routing: an admitted (proxied) path, a denied path,
# and a Host that names neither origin.
PEER_PROBES = {
    "host": [
        (8080, "/stub/ok", GATEWAY_HOST),
        (8080, "/v1/models", GATEWAY_HOST),
        (8080, "/", LANGFUSE_HOST),
        (8080, "/stub/ok", "other.example.test"),
    ],
    "port": [
        (8080, "/stub/ok", GATEWAY_HOST),
        (8080, "/v1/models", GATEWAY_HOST),
        (8081, "/", LANGFUSE_HOST),
        (8080, "/stub/ok", "other.example.test"),
        (8081, "/", "other.example.test"),
    ],
}
TRUSTED_STATUS = {"/stub/ok": 200, "/v1/models": 404, "/": 404}

# HTTP/1.1 without a Host header: nginx refuses it while reading the headers.
NO_HOST_REQUEST = (
    f"GET /no-host-probe?code={PROXY_CANARY} HTTP/1.1\r\n"
    f"X-Corp-Auth: {CORP_AUTH}\r\n"
    "Connection: close\r\n\r\n"
).encode()


def _raw_exchange(port: int, request: bytes) -> bytes:
    """Send bytes httpx would not (it always adds Host); read until nginx closes."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        conn.sendall(request)
        chunks = []
        while chunk := conn.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.parametrize("routing", ["host", "port"])
def test_an_untrusted_peer_gets_no_response_at_all(
    specs: dict[str, Spec],
    project: Path,
    network: Network,
    stub_upstream: Stub,
    routing: str,
) -> None:
    """Every request an untrusted peer gets past nginx's header parser is closed
    with no response. nginx's own pre-phase errors — no ``Host``, ``Host: a..b``,
    an oversized header — are answered 400 with ``Server: nginx`` before the
    server-level ``if`` runs: a fingerprint, never request content, accepted by
    the plan (Task 9 documents it)."""
    assert network.client_peer != UNTRUSTED_ONLY
    inject_test_only_proxy(project)
    env = {**VALID_BEHIND_PROXY, "NGINX_TRUSTED_PROXIES": UNTRUSTED_ONLY}
    hits = stub_upstream.hits()
    probes = PEER_PROBES[routing]
    with started(specs[routing], project, network, env, trust_client=False) as nginx:
        for port, path, host in probes:
            assert nginx.status(port, path, host, "POST") == 444, (port, path, host)
        # An untrusted peer cannot talk its way in by naming a trusted address in
        # X-Forwarded-For. real_ip never rewrites for an untrusted peer, so this
        # holds whether geo keys on $realip_remote_addr or $remote_addr; the
        # trusted-peer test below is the one that tells the two apart.
        spoofed = nginx.status(
            8080, "/stub/ok", GATEWAY_HOST, headers={"X-Forwarded-For": UNTRUSTED_ONLY}
        )
        assert spoofed == 444
        no_host = _raw_exchange(nginx.ports[8080], NO_HOST_REQUEST)
        entries = nginx.access_log(expected=len(probes) + 2)
        _assert_no_secret_in(nginx)
        assert nginx.exec(*HEALTH_PROBE).returncode == 0

    assert no_host.startswith(b"HTTP/1.1 400 "), no_host
    for sent in (b"no-host-probe", PROXY_CANARY.encode(), CORP_AUTH.encode()):
        assert sent not in no_host, sent
    assert stub_upstream.hits() == hits
    assert len(entries) == len(probes) + 2, entries
    assert sorted(entry["status"] for entry in entries) == ["400"] + ["444"] * (len(probes) + 1)
    for entry in entries:
        assert entry["realip_remote_addr"] == network.client_peer, entry
        assert entry["remote_addr"] == network.client_peer, entry
        assert entry["from_trusted_proxy"] == "0", entry
        assert entry["upstream_status"] in ("", "-"), entry


@pytest.mark.parametrize("routing", ["host", "port"])
def test_a_trusted_peer_is_served_and_its_forwarded_client_is_logged(
    specs: dict[str, Spec],
    project: Path,
    network: Network,
    stub_upstream: Stub,
    routing: str,
) -> None:
    inject_test_only_proxy(project)
    hits = stub_upstream.hits()
    probes = PEER_PROBES[routing]
    # Not in NGINX_TRUSTED_PROXIES. real_ip rewrites $remote_addr to it, so a geo
    # keyed on $remote_addr would 444 every probe here; being served (and
    # from_trusted_proxy "1") is what pins the gate to $realip_remote_addr.
    client = "203.0.113.9"
    # Host routing still drops a Host that names neither origin.
    expected = [
        444 if routing == "host" and host == "other.example.test" else TRUSTED_STATUS[path]
        for _, path, host in probes
    ]
    with started(specs[routing], project, network, VALID_BEHIND_PROXY) as nginx:
        statuses = [
            nginx.status(port, path, host, "POST", {"X-Forwarded-For": client})
            for port, path, host in probes
        ]
        entries = nginx.access_log(expected=len(probes))
        assert nginx.exec(*HEALTH_PROBE).returncode == 0

    assert statuses == expected
    assert stub_upstream.hits() == hits + expected.count(200)
    assert len(entries) == len(probes), entries
    for entry in entries:
        assert entry["realip_remote_addr"] == network.client_peer, entry
        assert entry["from_trusted_proxy"] == "1", entry
        # real_ip: behind a trusted terminator, the client is its X-Forwarded-For.
        assert entry["remote_addr"] == client, entry


# --------------------------------------------------------------------------- #
# the gateway allow-list, against the litellm stand-in
# --------------------------------------------------------------------------- #


def _headers(record: dict[str, Any]) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for name, value in record["headers"]:
        headers.setdefault(name, []).append(value)
    return headers


def _raw_status(port: int, request: bytes) -> int:
    """Send bytes httpx would rewrite or normalize; the status nginx answers."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        conn.sendall(request)
        received = b""
        while b"\r\n" not in received:
            chunk = conn.recv(65536)
            if not chunk:
                return 444
            received += chunk
    return int(received.split(b" ", 2)[1])


def _raw_request(method: str, target: str, *headers: str, body: bytes = b"") -> bytes:
    lines = [f"{method} {target} HTTP/1.1", f"Host: {GATEWAY_HOST}", *headers]
    if body:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join([*lines, "Connection: close"]) + "\r\n\r\n").encode() + body


def test_the_rendered_gateway_snippet_is_the_tracked_allow_list(
    specs: dict[str, Spec], project: Path, network: Network
) -> None:
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        rendered = nginx.exec("cat", f"{RENDERED}/snippets/gateway-locations.inc").stdout

    assert "$http_upgrade" not in rendered
    assert "$connection_upgrade" not in rendered
    snippet = parse_snippet(rendered)
    assert declared_pairs(snippet) == DECLARED
    assert all(location.find("limit_except") for location in snippet.exact())


def test_nginx_starts_before_litellm_exists_and_resolves_it_per_request(
    specs: dict[str, Spec], project: Path
) -> None:
    # A literal upstream would fail `nginx -t` with no litellm on the network; the
    # variable is resolved per request (the resolver line in 00-http), so nginx
    # boots first and follows a litellm that appears — or restarts — later.
    with (
        user_network(specs["host"].image) as fresh,
        started(specs["host"], project, fresh, VALID_BEHIND_PROXY) as nginx,
        launched_stub(fresh) as stub,
    ):
        status = nginx.status(8080, "/v1/models", GATEWAY_HOST)
        records = stub.requests()

    assert status == 200
    assert [(r["method"], r["target"]) for r in records] == [("GET", "/v1/models")]


WEBSOCKET_HANDSHAKE = _raw_request(
    "GET",
    "/v1/responses",
    "Upgrade: websocket",
    "Sec-WebSocket-Version: 13",
    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
)
REFUSED_METHODS = (
    ("GET", "/v1/messages"),
    ("DELETE", "/v1/responses"),
    ("POST", "/v1/models"),
    ("GET", "/internal/issue-token"),
    ("OPTIONS", "/healthz/live"),
    ("PUT", "/v1/chat/completions"),
)


def test_a_websocket_handshake_and_every_other_method_are_refused_by_nginx(
    specs: dict[str, Spec], project: Path, network: Network, stub_upstream: Stub
) -> None:
    seen = len(stub_upstream.requests())
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        # `Connection: Upgrade` rides in the raw request's own header block.
        handshake = WEBSOCKET_HANDSHAKE.replace(b"Connection: close", b"Connection: Upgrade")
        assert _raw_status(nginx.ports[8080], handshake) == 403
        for method, path in REFUSED_METHODS:
            assert nginx.status(8080, path, GATEWAY_HOST, method) == 403, (method, path)
        refused = stub_upstream.requests_since(seen)
        # limit_except GET always lets HEAD through: nginx does not refuse it,
        # whatever the gateway then answers.
        head = nginx.status(8080, "/healthz/live", GATEWAY_HOST, "HEAD")

    assert refused == []
    assert head != 403
    assert [(r["method"], r["target"]) for r in stub_upstream.requests_since(seen)] == [
        ("HEAD", "/healthz/live")
    ]


TWO_MIB = 2 * 1024 * 1024


def test_a_2_mib_messages_body_reaches_the_gateway_intact(
    specs: dict[str, Spec], project: Path, network: Network, stub_upstream: Stub
) -> None:
    # The gateway's 100 KiB figure is per text leaf, not a body cap: no 413.
    body = b'{"messages":"' + b"x" * TWO_MIB + b'"}'
    seen = len(stub_upstream.requests())
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        status = nginx.status(8080, "/v1/messages", GATEWAY_HOST, "POST", content=body)

    assert status == 200
    (record,) = stub_upstream.requests_since(seen)
    assert (record["method"], record["target"]) == ("POST", "/v1/messages")
    assert record["body_length"] == len(body)
    assert record["body_sha256"] == hashlib.sha256(body).hexdigest()


ADMITTED_REQUESTS = (
    ("POST", "/v1/messages?beta=true", "/v1/messages?beta=true"),
    ("POST", "/v1/messages", "/v1/messages"),
    ("POST", "/v1/chat/completions", "/v1/chat/completions"),
    ("POST", "/v1/responses", "/v1/responses"),
    ("GET", "/v1/models", "/v1/models"),
    ("GET", "/healthz/live", "/healthz/live"),
    ("POST", "/internal/issue-token", "/internal/issue-token"),
)


@pytest.mark.parametrize("routing", ["host", "port"])
def test_each_admitted_pair_arrives_with_its_credentials_and_query_untouched(
    specs: dict[str, Spec], project: Path, network: Network, stub_upstream: Stub, routing: str
) -> None:
    assert {(m, t.split("?")[0]) for m, t, _ in ADMITTED_REQUESTS} == DECLARED
    seen = len(stub_upstream.requests())
    with started(specs[routing], project, network, VALID_BEHIND_PROXY) as nginx:
        for method, path, _ in ADMITTED_REQUESTS:
            content = b"{}" if method == "POST" and path != "/internal/issue-token" else None
            status = nginx.status(8080, path, GATEWAY_HOST, method, CREDENTIALS, content)
            assert status == 200, (method, path)
        # A client's own upgrade request never reaches the gateway.
        upgrade = _raw_request("POST", "/v1/responses", "Upgrade: websocket", body=b"{}").replace(
            b"Connection: close", b"Connection: Upgrade"
        )
        assert _raw_status(nginx.ports[8080], upgrade) == 200
        _assert_no_secret_in(nginx)

    records = stub_upstream.requests_since(seen)
    assert [(r["method"], r["target"]) for r in records] == [
        *((m, target) for m, _, target in ADMITTED_REQUESTS),
        ("POST", "/v1/responses"),
    ]
    for record in records[:-1]:
        headers = _headers(record)
        # Invariant 3: the developer's bearer, byte for byte; X-Corp-Auth likewise.
        assert headers["authorization"] == [AUTHORIZATION], record
        assert headers["x-corp-auth"] == [CORP_AUTH], record
    for record in records:
        headers = _headers(record)
        assert headers["x-forwarded-proto"] == ["https"], record
        assert headers["host"] == [GATEWAY_HOST], record
        assert "upgrade" not in headers, record
        assert headers.get("connection", ["close"]) != ["upgrade"], record


def test_an_issuance_body_over_1k_is_refused_by_nginx(
    specs: dict[str, Spec], project: Path, network: Network, stub_upstream: Stub
) -> None:
    seen = len(stub_upstream.requests())
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        status = nginx.status(
            8080, "/internal/issue-token", GATEWAY_HOST, "POST", content=b"x" * 2048
        )

    assert status == 413
    assert stub_upstream.requests_since(seen) == []


# Denied by the allow-list's shape, not by these names: the control-plane
# routes seen in litellm (the 2026-08-07 SSO probe, round 2, the bumps since)
# are evidence that the shape holds, never the boundary itself.
DENIED_BY_NAME = (
    "/v1/mcp/server",
    "/v1/model/info",
    "/v1/access_group",
    "/v1/tool/policy",
    "/v1/messages/count_tokens",
    "/v1/responses/input_tokens",
    "/v1/responses/compact",
    "/v1/responses/resp_123",
    "/v1/responses/resp_123/input_items",
    "/v1/embeddings",
    "/v1/completions",
    "/v1/moderations",
    "/v1/audio/speech",
    "/health",
    "/health/services",
    "/health/drain",
    "/health/liveliness",
    "/health/readiness",
    "/healthz/ready",
    "/healthz/sanitization",
    "/healthz/extensions",
    "/metrics",
    "/key/generate",
    "/model/new",
    "/ui",
    "/sso/key/generate",
    "/sso/cli/start",
    "/sso/cli/poll/key_1",
    "/sso/cli/complete/login_1",
    "/policies",
    "/guardrails/apply_guardrail",
    "/v1/some/future/router",
)


def test_every_named_route_is_404_and_reaches_nothing(
    specs: dict[str, Spec], project: Path, network: Network, stub_upstream: Stub
) -> None:
    seen = len(stub_upstream.requests())
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        for path in DENIED_BY_NAME:
            for method in ("GET", "POST"):
                assert nginx.status(8080, path, GATEWAY_HOST, method, CREDENTIALS) == 404, (
                    method,
                    path,
                )

    assert stub_upstream.requests_since(seen) == []


# Spelled so that a location match and the forwarded path could disagree.
TRICK_TARGETS = (
    "/v1/messages/..%2f..%2fkey/generate",
    "/key/generate/..%2f..%2fv1/messages",
    "//v1//messages",
    "/V1/Messages",
    "/v1/messages/",
    "/v1/messages%2f..%2fmodel/info",
)


@pytest.mark.parametrize("target", TRICK_TARGETS)
def test_the_gateway_never_sees_a_path_nginx_did_not_admit(
    specs: dict[str, Spec], project: Path, network: Network, stub_upstream: Stub, target: str
) -> None:
    seen = len(stub_upstream.requests())
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        status = _raw_status(nginx.ports[8080], _raw_request("POST", target, body=b"{}"))

    records = stub_upstream.requests_since(seen)
    if status == 404:
        assert records == [], target
    else:
        assert status == 200, (target, status)
        assert len(records) == 1, (target, records)
        assert records[0]["target"] in ADMITTED_PATHS, (target, records[0]["target"])


@pytest.mark.parametrize("overlay", ["base", "oauth"])
@pytest.mark.parametrize("routing", ["host", "port"])
def test_the_allow_list_holds_under_both_routings_and_the_oauth_overlay(
    specs: dict[str, Spec],
    oauth_specs: dict[str, Spec],
    project: Path,
    network: Network,
    stub_upstream: Stub,
    routing: str,
    overlay: str,
) -> None:
    # OAuth is the production mode, and the one where litellm's own auth is
    # weakest: no master key at all.
    spec = (oauth_specs if overlay == "oauth" else specs)[routing]
    seen = len(stub_upstream.requests())
    with started(spec, project, network, VALID_BEHIND_PROXY) as nginx:
        rendered = nginx.exec("cat", f"{RENDERED}/snippets/gateway-locations.inc").stdout
        for method, path in sorted(DECLARED):
            content = b"{}" if method == "POST" else None
            assert nginx.status(8080, path, GATEWAY_HOST, method, content=content) == 200
        for method, path in REFUSED_METHODS:
            assert nginx.status(8080, path, GATEWAY_HOST, method) == 403, (method, path)
        for path in DENIED_BY_NAME:
            assert nginx.status(8080, path, GATEWAY_HOST, "POST") == 404, path

    assert declared_pairs(parse_snippet(rendered)) == DECLARED
    assert sorted((r["method"], r["target"]) for r in stub_upstream.requests_since(seen)) == (
        sorted(DECLARED)
    )


TEMP_DIRS = ("/var/cache/nginx/client_temp", "/var/cache/nginx/proxy_temp")
TWO_HUNDRED_KB = 200 * 1024


def _fault_temp_files(nginx: Running) -> None:
    faulted = docker("exec", "-u", "0", nginx.name, "chmod", "000", *TEMP_DIRS)
    assert faulted.returncode == 0, faulted.stderr


def test_a_temp_file_fault_cannot_write_the_request_line_on_an_admitted_route(
    specs: dict[str, Spec], project: Path, network: Network, stub_upstream: Stub
) -> None:
    """``crit`` is not closed by the level: a temp-file fault is a ``[crit]``
    entry carrying the request line. With request buffering off and no temp
    file for responses, an admitted route never opens one."""
    body = b"x" * TWO_HUNDRED_KB
    target = f"/v1/messages?code={PROXY_CANARY}"
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        _fault_temp_files(nginx)
        status = nginx.status(8080, target, GATEWAY_HOST, "POST", CREDENTIALS, body)
        nginx.access_log(expected=1)
        _assert_no_secret_in(nginx)
        stdout, stderr = nginx.log_streams()

    assert status == 200
    for stream in (stdout, stderr):
        assert "POST /v1/messages" not in stream
        assert "client_temp" not in stream


def test_the_temp_file_fault_is_real_on_a_buffered_location(
    specs: dict[str, Spec], project: Path, network: Network, stub_upstream: Stub
) -> None:
    # The control: the same fault on a location that buffers the request body
    # (the test-only snippet) does write the request line at crit — so the test
    # above would see it if an admitted route could reach a temp file.
    inject_test_only_proxy(project)
    body = b"x" * TWO_HUNDRED_KB
    with started(specs["host"], project, network, VALID_BEHIND_PROXY) as nginx:
        _fault_temp_files(nginx)
        nginx.status(8080, f"/stub/ok?code={PROXY_CANARY}", GATEWAY_HOST, "POST", content=body)
        nginx.access_log(expected=1)
        stderr = nginx.log_streams()[1]

    assert "[crit]" in stderr
    assert "client_temp" in stderr
    assert PROXY_CANARY in stderr
