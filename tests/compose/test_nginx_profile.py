"""The nginx front door on the production compose stack — static checks.

Opt-in contract: with COMPOSE_PROFILES unset the stack is exactly today's, and no
NGINX_* key may be required to render it. Assertions about services, ports and
mounts run against the MERGED ``docker compose config`` output, never a single
YAML layer. What the container renders is asserted in ``test_nginx_runtime.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from corp_llm_gateway.tokens.issuance import default_token_factory
from tests.compose.nginx_support import (
    COMPOSE,
    ENTRYPOINT_KEYS,
    ISSUANCE,
    NGINX_DIR,
    PROFILES,
    ROUTING,
    render,
    require_compose_cli,
)

IMAGE = "nginx:1.27-alpine"
MOUNTS = {
    "nginx/nginx.conf": "/etc/nginx/nginx.conf",
    "nginx/entrypoint.sh": "/corp/entrypoint.sh",
    "nginx/templates": "/corp/templates",
    "nginx/certs": "/etc/nginx/certs",
}
HEALTHCHECK = ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8090/nginx-health"]
NGINX_SERVICES = set(PROFILES)
NAMED_KEYS = re.compile(r"\b(NGINX_[A-Z_]+|GATEWAY_DOMAIN|COMPOSE_PROFILES)\b")


@pytest.fixture(autouse=True)
def _compose_cli() -> None:
    require_compose_cli()


def _unset_warnings(stderr: str) -> list[str]:
    return [
        line for line in stderr.splitlines() if "is not set" in line and NAMED_KEYS.search(line)
    ]


def _all_ports(services: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(name, port) for name, svc in services.items() for port in svc.get("ports", [])]


# --------------------------------------------------------------------------- #
# the opt-in contract
# --------------------------------------------------------------------------- #


def test_no_nginx_service_without_a_profile(tmp_path: Path) -> None:
    services = render(tmp_path).services

    assert NGINX_SERVICES.isdisjoint(services)


@pytest.mark.parametrize("profile", PROFILES)
def test_each_profile_brings_up_its_own_service_alone(tmp_path: Path, profile: str) -> None:
    services = render(tmp_path, profiles=profile).services

    assert NGINX_SERVICES & set(services) == {profile}


@pytest.mark.parametrize("files", [(COMPOSE,), (COMPOSE, ISSUANCE)], ids=["base", "production"])
@pytest.mark.parametrize("profile", [None, *PROFILES])
def test_the_render_needs_no_nginx_key(
    tmp_path: Path, files: tuple[Path, ...], profile: str | None
) -> None:
    # compose interpolates the whole file before filtering by profile, so one
    # ${NGINX_TLS_MODE:?} would break every stack that never asked for nginx.
    result = render(tmp_path, *files, profiles=profile, check=False)

    assert result.returncode == 0, result.stderr
    assert _unset_warnings(result.stderr) == []


# --------------------------------------------------------------------------- #
# published ports
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("profile", PROFILES)
def test_nginx_publishes_on_loopback_unless_told_otherwise(tmp_path: Path, profile: str) -> None:
    services = render(tmp_path, profiles=profile).services
    ports = services[profile]["ports"]

    assert all(port["host_ip"] == "127.0.0.1" for port in ports), ports
    published = sorted((int(p["published"]), p["target"]) for p in ports)
    if profile == "nginx":
        assert published == [(443, 8080)]
    else:
        assert published == [(443, 8080), (8443, 8081)]
    for name, port in _all_ports(services):
        assert port.get("host_ip") == "127.0.0.1", (name, port)


@pytest.mark.parametrize("profile", PROFILES)
def test_nginx_publishes_where_the_env_says(tmp_path: Path, profile: str) -> None:
    services = render(
        tmp_path,
        profiles=profile,
        env_extra="NGINX_BIND_ADDR=10.1.2.3\nNGINX_PORT=9443\nNGINX_LANGFUSE_PORT=9444\n",
    ).services
    ports = services[profile]["ports"]

    assert {p["host_ip"] for p in ports} == {"10.1.2.3"}
    assert {int(p["published"]) for p in ports} <= {9443, 9444}
    # The bind address moves nginx only, never a gateway's own port.
    gateways = [name for name in services if name.startswith("gateway-")]
    assert gateways
    for name in gateways:
        assert [p["host_ip"] for p in services[name]["ports"]] == ["127.0.0.1"], name


@pytest.mark.parametrize("profile", PROFILES)
def test_a_bracketed_ipv6_bind_address_renders(tmp_path: Path, profile: str) -> None:
    ports = render(tmp_path, profiles=profile, env_extra="NGINX_BIND_ADDR=[fd00::1]\n").services[
        profile
    ]["ports"]

    assert {p["host_ip"] for p in ports} == {"fd00::1"}


@pytest.mark.parametrize("profile", PROFILES)
def test_the_litellm_port_stays_on_loopback_with_a_profile_on(tmp_path: Path, profile: str) -> None:
    services = render(tmp_path, profiles=profile, providers="anthropic,codex,corp").services

    for name in ("gateway-anthropic", "gateway-codex", "gateway-corp"):
        ports = services[name]["ports"]
        assert [(p["host_ip"], p["target"]) for p in ports] == [("127.0.0.1", 4000)], name


@pytest.mark.parametrize("profile", PROFILES)
def test_the_production_stack_with_a_profile_renders_clean_and_leaves_config_toml_alone(
    tmp_path: Path, profile: str
) -> None:
    result = render(tmp_path, COMPOSE, ISSUANCE, profiles=profile, check=False)

    assert result.returncode == 0, result.stderr
    assert _unset_warnings(result.stderr) == []
    services = result.services
    assert NGINX_SERVICES & set(services) == {profile}
    config_toml = result.project_dir / "gateway" / "config.toml"
    for volume in services[profile]["volumes"]:
        source = Path(volume["source"]).resolve()
        assert source.is_relative_to(result.project_dir / "nginx"), volume
        assert not config_toml.is_relative_to(source), volume
        assert not volume["target"].startswith("/etc/corp-llm-gateway"), volume


# --------------------------------------------------------------------------- #
# the service shape ("Rendering design")
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("profile", PROFILES)
def test_the_service_runs_the_validating_entrypoint_with_its_routing(
    tmp_path: Path, profile: str
) -> None:
    service = render(tmp_path, profiles=profile).services[profile]

    assert service["image"] == IMAGE
    assert service["entrypoint"] == ["/bin/sh", "/corp/entrypoint.sh"]
    assert service["command"] == [ROUTING[profile]]
    assert service["tmpfs"] == ["/etc/nginx/rendered"]
    assert service["restart"] == "unless-stopped"


@pytest.mark.parametrize("profile", PROFILES)
def test_the_service_mounts_exactly_the_four_read_only_paths(tmp_path: Path, profile: str) -> None:
    result = render(tmp_path, profiles=profile)
    volumes = result.services[profile]["volumes"]

    mounts = {
        Path(v["source"]).resolve().relative_to(result.project_dir).as_posix(): v["target"]
        for v in volumes
    }
    assert mounts == MOUNTS
    assert all(v["type"] == "bind" and v["read_only"] is True for v in volumes), volumes
    # The stock renderer and conf.d are never fed.
    assert not any(v["target"].startswith("/etc/nginx/templates") for v in volumes)
    assert not any("conf.d" in v["target"] for v in volumes)


@pytest.mark.parametrize("profile", PROFILES)
def test_the_service_passes_every_entrypoint_key(tmp_path: Path, profile: str) -> None:
    environment = render(tmp_path, profiles=profile).services[profile]["environment"]

    assert set(environment) == set(ENTRYPOINT_KEYS)
    assert environment["NGINX_BIND_ADDR"] == "127.0.0.1"
    # The provider check reads the same line compose selected the services from.
    assert environment["COMPOSE_PROFILES"] == f"anthropic,{profile}"
    set_here = {"NGINX_BIND_ADDR", "COMPOSE_PROFILES"}
    assert {k: v for k, v in environment.items() if k not in set_here} == dict.fromkeys(
        set(ENTRYPOINT_KEYS) - set_here, ""
    )


@pytest.mark.parametrize("profile", PROFILES)
def test_the_env_reaches_the_container(tmp_path: Path, profile: str) -> None:
    environment = render(
        tmp_path,
        profiles=profile,
        env_extra=(
            "NGINX_TLS_MODE=behind-proxy\nGATEWAY_DOMAIN=corp.example\n"
            "NGINX_TRUSTED_PROXIES=10.0.0.0/8 192.168.0.0/16\n"
            "LANGFUSE_PUBLIC_URL=https://langfuse.corp.example\n"
        ),
    ).services[profile]["environment"]

    assert environment["NGINX_TLS_MODE"] == "behind-proxy"
    assert environment["GATEWAY_DOMAIN"] == "corp.example"
    assert environment["NGINX_TRUSTED_PROXIES"] == "10.0.0.0/8 192.168.0.0/16"
    assert environment["LANGFUSE_PUBLIC_URL"] == "https://langfuse.corp.example"


@pytest.mark.parametrize("profile", PROFILES)
def test_the_healthcheck_probes_nginx_itself(tmp_path: Path, profile: str) -> None:
    service = render(tmp_path, profiles=profile).services[profile]

    assert service["healthcheck"]["test"] == HEALTHCHECK
    # Start order only: the variable upstreams mean nginx never waits for health.
    gateways = {"gateway-anthropic", "gateway-codex", "gateway-corp"}
    assert set(service["depends_on"]) == {*gateways, "langfuse-web"}
    assert {d["condition"] for d in service["depends_on"].values()} == {"service_started"}
    # A provider whose profile is off is no dependency.
    assert {name for name, d in service["depends_on"].items() if d.get("required") is False} == (
        gateways
    )


# --------------------------------------------------------------------------- #
# the Langfuse origin
# --------------------------------------------------------------------------- #

PRODUCTION_FILES = (COMPOSE, ISSUANCE)
STACKS = pytest.mark.parametrize(
    "files", [(COMPOSE,), PRODUCTION_FILES], ids=["base", "production"]
)


@STACKS
@pytest.mark.parametrize("profile", [None, *PROFILES])
def test_langfuse_web_is_never_published(
    tmp_path: Path, files: tuple[Path, ...], profile: str | None
) -> None:
    # Reachable only through nginx or a tunnel.
    service = render(tmp_path, *files, profiles=profile).services["langfuse-web"]

    assert "ports" not in service
    assert "network_mode" not in service


PUBLIC_LANGFUSE = "https://langfuse.example.test"


def _nextauth_url(service: dict[str, Any]) -> str:
    environment = service["environment"]
    assert isinstance(environment, dict), environment
    return environment["NEXTAUTH_URL"]


@STACKS
@pytest.mark.parametrize("profile", PROFILES)
def test_nextauth_url_is_the_langfuse_public_url_with_a_profile_on(
    tmp_path: Path, files: tuple[Path, ...], profile: str
) -> None:
    services = render(
        tmp_path, *files, profiles=profile, env_extra=f"LANGFUSE_PUBLIC_URL={PUBLIC_LANGFUSE}\n"
    ).services

    assert _nextauth_url(services["langfuse-web"]) == PUBLIC_LANGFUSE
    # The value the entrypoint checks is the one NextAuth is given.
    assert services[profile]["environment"]["LANGFUSE_PUBLIC_URL"] == PUBLIC_LANGFUSE


@STACKS
@pytest.mark.parametrize("profile", [None, *PROFILES])
def test_nextauth_url_keeps_the_tunnel_default_when_nothing_is_set(
    tmp_path: Path, files: tuple[Path, ...], profile: str | None
) -> None:
    services = render(tmp_path, *files, profiles=profile).services

    assert _nextauth_url(services["langfuse-web"]) == "http://localhost:3000"


# Every key the service reads, set, so an overlay that re-interpolated one would show.
EVERY_NGINX_KEY = (
    "NGINX_TLS_MODE=terminate\nGATEWAY_DOMAIN=corp.example\nNGINX_TLS_CERT=gateway.crt\n"
    "NGINX_TLS_KEY=gateway.key\nNGINX_TRUSTED_PROXIES=10.0.0.0/8\nNGINX_BIND_ADDR=10.1.2.3\n"
    "NGINX_PORT=9443\nNGINX_LANGFUSE_PORT=9444\nLANGFUSE_PUBLIC_URL=https://langfuse.corp.example\n"
    "NGINX_TOKEN_RATE=7\nNGINX_TOKEN_BURST=11\nNGINX_TOKEN_CONN=3\nNGINX_ISSUE_RATE=4\n"
)


def _portable_service(tmp_path: Path, files: tuple[Path, ...], profile: str) -> str:
    """The service as rendered, with its bind-mount sources relative to the project."""
    rendered = render(tmp_path, *files, profiles=profile, env_extra=EVERY_NGINX_KEY)
    service = rendered.services[profile]
    for volume in service["volumes"]:
        source = Path(volume["source"]).resolve()
        volume["source"] = source.relative_to(rendered.project_dir).as_posix()
    return yaml.safe_dump(service, sort_keys=True)


@pytest.mark.parametrize("profile", PROFILES)
def test_no_auth_overlay_touches_the_nginx_service(tmp_path: Path, profile: str) -> None:
    # The stack with and without developer token issuance must start the same
    # front door: issuance differs behind nginx, never in it.
    base, production = (
        _portable_service(tmp_path / stack, files, profile)
        for stack, files in (
            ("base", (COMPOSE,)),
            ("production", PRODUCTION_FILES),
        )
    )

    assert "corp.example" in base and "9443" in base
    assert production == base


# --------------------------------------------------------------------------- #
# the TLS policy test_nginx_runtime.py checks the rendered listeners against
# --------------------------------------------------------------------------- #

# TLSv1.2+ with forward-secret AEAD suites only (Mozilla "intermediate", no DHE).
TLS_CIPHERS = ":".join(
    [
        "ECDHE-ECDSA-AES128-GCM-SHA256",
        "ECDHE-RSA-AES128-GCM-SHA256",
        "ECDHE-ECDSA-AES256-GCM-SHA384",
        "ECDHE-RSA-AES256-GCM-SHA384",
        "ECDHE-ECDSA-CHACHA20-POLY1305",
        "ECDHE-RSA-CHACHA20-POLY1305",
    ]
)
# Mozilla "intermediate": a shared cache and no tickets, whose key nginx never
# rotates, which would undo forward secrecy until a restart.
TLS_SESSION = [
    "ssl_session_cache shared:corp_tls:10m",
    "ssl_session_timeout 1d",
    "ssl_session_tickets off",
]


# --------------------------------------------------------------------------- #
# the http context: the token key map
# --------------------------------------------------------------------------- #

HTTP_TEMPLATE = NGINX_DIR / "templates" / "00-http.conf.template"


def test_every_issued_token_matches_the_key_map_shape() -> None:
    # A token the map does not recognise is keyed by address: every developer
    # behind one NAT would share a bucket.
    shape = re.search(r'"~\^(ct_[^"]*)\$"', HTTP_TEMPLATE.read_text()).group(1)
    for _ in range(200):
        assert re.fullmatch(shape, default_token_factory())
