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

from tests.compose.nginx_support import (
    COMPOSE,
    COMPOSE_DIR,
    COMPOSE_ONLY_KEYS,
    ENTRYPOINT_KEYS,
    ISSUANCE,
    NGINX_DIR,
    OAUTH,
    PROFILES,
    ROUTING,
    render,
    require_compose_cli,
)

ENV_EXAMPLE = COMPOSE_DIR / ".env.example"
NGINX_CONF = NGINX_DIR / "nginx.conf"

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


def _raw_services() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text())["services"]


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


@pytest.mark.parametrize("files", [(COMPOSE,), (COMPOSE, OAUTH)], ids=["virtual-keys", "oauth"])
@pytest.mark.parametrize("profile", [None, *PROFILES])
def test_the_render_needs_no_nginx_key(
    tmp_path: Path, files: tuple[Path, ...], profile: str | None
) -> None:
    # compose interpolates the whole file before filtering by profile, so one
    # ${NGINX_TLS_MODE:?} would break every stack that never asked for nginx.
    result = render(tmp_path, *files, profiles=profile, check=False)

    assert result.returncode == 0, result.stderr
    assert _unset_warnings(result.stderr) == []


def test_no_nginx_interpolation_is_a_required_form() -> None:
    raw = COMPOSE.read_text()
    required = re.findall(r"\$\{(NGINX_[A-Z_]+|GATEWAY_DOMAIN|COMPOSE_PROFILES):?\?", raw)

    assert required == []
    for key in (*ENTRYPOINT_KEYS, *COMPOSE_ONLY_KEYS):
        forms = set(re.findall(rf"\$\{{{key}([^}}]*)\}}", raw))
        assert forms, f"{key} is never interpolated"
        assert all(form.startswith(":-") for form in forms), (key, forms)


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
        assert port.get("host_ip") not in (None, "", "0.0.0.0", "::"), (name, port)


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
    # The bind address moves nginx only, never the gateway's own port.
    litellm_ports = services["litellm"]["ports"]
    assert [p["host_ip"] for p in litellm_ports] == ["127.0.0.1"]


@pytest.mark.parametrize("profile", PROFILES)
def test_the_litellm_port_stays_on_loopback_with_a_profile_on(tmp_path: Path, profile: str) -> None:
    ports = render(tmp_path, profiles=profile).services["litellm"]["ports"]

    assert [(p["host_ip"], p["target"]) for p in ports] == [("127.0.0.1", 4000)]


@pytest.mark.parametrize("profile", PROFILES)
def test_the_production_stack_with_a_profile_renders_clean_and_leaves_config_toml_alone(
    tmp_path: Path, profile: str
) -> None:
    result = render(tmp_path, COMPOSE, OAUTH, ISSUANCE, profiles=profile, check=False)

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
    assert {k: v for k, v in environment.items() if k != "NGINX_BIND_ADDR"} == dict.fromkeys(
        set(ENTRYPOINT_KEYS) - {"NGINX_BIND_ADDR"}, ""
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
    assert set(service["depends_on"]) == {"litellm", "langfuse-web"}
    assert {d["condition"] for d in service["depends_on"].values()} == {"service_started"}


def test_both_services_share_one_definition() -> None:
    services = _raw_services()
    nginx, ports = services["nginx"], services["nginx-ports"]
    differing = {key for key in nginx.keys() | ports.keys() if nginx.get(key) != ports.get(key)}

    assert differing == {"profiles", "command", "ports"}
    assert "<<: *nginx-front-door" in COMPOSE.read_text()


# --------------------------------------------------------------------------- #
# compose/nginx/ itself
# --------------------------------------------------------------------------- #


def test_no_path_under_compose_nginx_is_a_conf_d() -> None:
    offending = [
        path for path in NGINX_DIR.rglob("*") if "conf.d" in path.relative_to(NGINX_DIR).parts
    ]

    assert offending == []


def test_nginx_conf_includes_only_the_render() -> None:
    text = NGINX_CONF.read_text()
    directives = re.sub(r"#[^\n]*", "", text)

    assert re.findall(r"^\s*include\s+([^;]+);", directives, re.MULTILINE) == [
        "/etc/nginx/rendered/*.conf"
    ]
    assert "conf.d" not in directives
    assert re.findall(r"^\s*error_log\s+([^;]+);", directives, re.MULTILINE) == ["/dev/stderr crit"]
    assert re.search(r"^\s*server_tokens\s+off;", directives, re.MULTILINE)
    # Static: nothing in it is ever substituted.
    assert "${" not in text


def test_the_rendering_design_layout_is_in_place() -> None:
    templates = NGINX_DIR / "templates"
    expected = {
        "00-http.conf.template",
        "listeners/behind-proxy.host.conf.template",
        "listeners/behind-proxy.port.conf.template",
        "snippets/gateway-locations.inc.template",
        "snippets/langfuse-locations.inc.template",
    }
    present = {p.relative_to(templates).as_posix() for p in templates.rglob("*") if p.is_file()}

    assert expected <= present
    assert (NGINX_DIR / "entrypoint.sh").is_file()


def test_every_listener_server_block_is_one_include_of_a_snippet() -> None:
    for template in (NGINX_DIR / "templates" / "listeners").glob("*.template"):
        body = re.sub(r"#[^\n]*", "", template.read_text())
        for block in re.findall(r"server\s*\{([^{}]*)\}", body):
            directives = [d.strip() for d in block.split(";") if d.strip()]
            payload = [
                d for d in directives if not d.startswith(("listen ", "server_name ", "ssl_"))
            ]
            assert payload in (
                ["return 444"],
                ["include /etc/nginx/rendered/snippets/gateway-locations.inc"],
                ["include /etc/nginx/rendered/snippets/langfuse-locations.inc"],
            ), (template.name, directives)


# --------------------------------------------------------------------------- #
# .env.example
# --------------------------------------------------------------------------- #

ENV_EXAMPLE_KEYS = (
    "COMPOSE_PROFILES",
    "NGINX_TLS_MODE",
    "GATEWAY_DOMAIN",
    "NGINX_TLS_CERT",
    "NGINX_TLS_KEY",
    "NGINX_TRUSTED_PROXIES",
    "NGINX_BIND_ADDR",
    "NGINX_PORT",
    "NGINX_LANGFUSE_PORT",
)


@pytest.mark.parametrize("key", ENV_EXAMPLE_KEYS)
def test_the_env_example_offers_each_key_commented(key: str) -> None:
    text = ENV_EXAMPLE.read_text()

    assert re.search(rf"^# {key}=", text, re.MULTILINE), key
    # Uncommented, a copied .env would switch the front door on (or half on).
    assert not re.search(rf"^{key}=", text, re.MULTILINE), key


def test_the_env_example_block_sits_beside_the_compose_file_line() -> None:
    lines = ENV_EXAMPLE.read_text().splitlines()
    compose_file = max(i for i, line in enumerate(lines) if line.startswith("# COMPOSE_FILE="))
    profiles = lines.index("# COMPOSE_PROFILES=nginx")

    assert 0 < profiles - compose_file < 15
