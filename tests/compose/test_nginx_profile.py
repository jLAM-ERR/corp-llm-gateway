"""The nginx front door on the production compose stack — static checks.

Opt-in contract: with COMPOSE_PROFILES unset the stack is exactly today's, and no
NGINX_* key may be required to render it. Assertions about services, ports and
mounts run against the MERGED ``docker compose config`` output, never a single
YAML layer. What the container renders is asserted in ``test_nginx_runtime.py``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from corp_llm_gateway.tokens.issuance import default_token_factory
from tests.compose.nginx_allowlist import (
    GATEWAY_SNIPPET,
    RATE_LIMITED,
    Location,
    Snippet,
    gateway_snippet,
    parse_snippet,
)
from tests.compose.nginx_support import (
    COMPOSE,
    COMPOSE_DIR,
    COMPOSE_ONLY_KEYS,
    ENTRYPOINT_KEYS,
    ISSUANCE,
    NGINX_DIR,
    OAUTH,
    PROFILES,
    ROOT,
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
    # The bind address moves nginx only, never the gateway's own port.
    litellm_ports = services["litellm"]["ports"]
    assert [p["host_ip"] for p in litellm_ports] == ["127.0.0.1"]


@pytest.mark.parametrize("profile", PROFILES)
def test_a_bracketed_ipv6_bind_address_renders(tmp_path: Path, profile: str) -> None:
    ports = render(tmp_path, profiles=profile, env_extra="NGINX_BIND_ADDR=[fd00::1]\n").services[
        profile
    ]["ports"]

    assert {p["host_ip"] for p in ports} == {"fd00::1"}


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
        "listeners/terminate.host.conf.template",
        "listeners/terminate.port.conf.template",
        "snippets/gateway-locations.inc.template",
        "snippets/langfuse-locations.inc.template",
    }
    present = {p.relative_to(templates).as_posix() for p in templates.rglob("*") if p.is_file()}

    # Exactly: a stray file here (the test-only proxy snippet, say) would ship.
    assert present == expected
    assert (NGINX_DIR / "entrypoint.sh").is_file()


def _locations(snippet: Path) -> tuple[list[str], dict[str, list[str]]]:
    """(server-context directives, location -> its directives), nested blocks
    rendered inline (``limit_except POST { deny all }``)."""
    parsed = parse_snippet(snippet.read_text())
    locations = {
        location.key: [str(d) for d in location.directives] for location in parsed.locations
    }
    assert len(locations) == len(parsed.locations), snippet.name
    return [str(d) for d in parsed.server], locations


def _proxied(path: str) -> str:
    return f"proxy_pass $gateway_upstream{path}$is_args$args"


# The one copy of the proxy settings, inherited by every location below.
GATEWAY_SERVER_DIRECTIVES = [
    "set $gateway_upstream http://litellm:4000",
    "proxy_http_version 1.1",
    "proxy_buffering off",
    "proxy_request_buffering off",
    "proxy_read_timeout 3600s",
    "chunked_transfer_encoding on",
    "proxy_max_temp_file_size 0",
    'proxy_set_header Upgrade ""',
    'proxy_set_header Connection ""',
    "proxy_set_header Host $host",
    "proxy_set_header X-Forwarded-Proto https",
    "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for",
    "client_max_body_size 25m",
    "limit_req_status 429",
    "limit_conn_status 429",
    "error_page 429 = @rate_limited",
]

TOKEN_LIMITS = [
    "limit_req zone=corp_token burst=${NGINX_TOKEN_BURST} nodelay",
    "limit_conn corp_conn ${NGINX_TOKEN_CONN}",
]
ISSUE_LIMIT = "limit_req zone=corp_issue burst=2 nodelay"
# Empty over plain HTTP, and nginx sends no header for an empty add_header value.
HSTS_ON_TLS = "add_header Strict-Transport-Security $hsts_value always"
HSTS_VALUE_MAP = 'map $https $hsts_value { on "max-age=31536000"; default ""; }'
RATE_LIMITED_BODY = [
    "default_type application/json",
    "add_header Retry-After 1 always",
    HSTS_ON_TLS,
    """return 429 '{"error":{"code":"E_RATE_LIMITED"}}'""",
]

LANGFUSE_SNIPPET = NGINX_DIR / "templates" / "snippets" / "langfuse-locations.inc.template"

# The Langfuse origin's proxy settings: the only snippet that forwards an
# upgrade, and the only consumer of $proxy_host_header (NextAuth needs the port).
LANGFUSE_SERVER_DIRECTIVES = [
    "set $langfuse_upstream http://langfuse-web:3000",
    "proxy_http_version 1.1",
    "proxy_buffering off",
    "proxy_request_buffering off",
    "proxy_read_timeout 3600s",
    "chunked_transfer_encoding on",
    "proxy_max_temp_file_size 0",
    "proxy_set_header Upgrade $http_upgrade",
    "proxy_set_header Connection $connection_upgrade",
    "proxy_set_header Host $proxy_host_header",
    "proxy_set_header X-Forwarded-Proto https",
    "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for",
    "client_max_body_size 25m",
]

# What the front door admits: the gateway's allow-list ("The security
# constraint"), and the whole Langfuse origin — its own server, at its root.
ADMITTED_LOCATIONS = {
    "gateway-locations.inc.template": (
        GATEWAY_SERVER_DIRECTIVES,
        {
            "= /v1/messages": [
                "limit_except POST { deny all }",
                *TOKEN_LIMITS,
                _proxied("/v1/messages"),
            ],
            "= /v1/chat/completions": [
                "limit_except POST { deny all }",
                *TOKEN_LIMITS,
                _proxied("/v1/chat/completions"),
            ],
            "= /v1/responses": [
                "limit_except POST { deny all }",
                *TOKEN_LIMITS,
                _proxied("/v1/responses"),
            ],
            "= /v1/models": [
                "limit_except GET { deny all }",
                *TOKEN_LIMITS,
                _proxied("/v1/models"),
            ],
            "= /healthz/live": ["limit_except GET { deny all }", _proxied("/healthz/live")],
            "= /internal/issue-token": [
                "limit_except POST { deny all }",
                ISSUE_LIMIT,
                "client_max_body_size 1k",
                _proxied("/internal/issue-token"),
            ],
            "/": ["return 404"],
            RATE_LIMITED: RATE_LIMITED_BODY,
        },
    ),
    "langfuse-locations.inc.template": (
        LANGFUSE_SERVER_DIRECTIVES,
        {"/": ["proxy_pass $langfuse_upstream$request_uri"]},
    ),
}


@pytest.mark.parametrize("snippet", sorted(ADMITTED_LOCATIONS))
def test_each_snippet_admits_exactly_the_pinned_locations(snippet: str) -> None:
    path = NGINX_DIR / "templates" / "snippets" / snippet

    assert _locations(path) == ADMITTED_LOCATIONS[snippet]


def test_the_parser_sees_a_nested_block_and_refuses_an_unbalanced_one() -> None:
    parsed = parse_snippet("location = /a { limit_except POST { deny all; } return 204; }")
    assert [str(d) for d in parsed.locations[0].directives] == [
        "limit_except POST { deny all }",
        "return 204",
    ]
    with pytest.raises(AssertionError):
        parse_snippet("location = /a { limit_except POST { deny all; } ")


@pytest.mark.parametrize(
    ("body", "found"),
    [("return 204;", 0), ("limit_except POST { deny all; } limit_except GET { deny all; }", 2)],
    ids=["none", "two"],
)
def test_a_location_without_exactly_one_limit_except_names_itself(body: str, found: int) -> None:
    (location,) = parse_snippet(f"location = /a {{ {body} }}").locations

    with pytest.raises(AssertionError, match=f"= /a: expected one limit_except, found {found}"):
        _ = location.methods


@pytest.mark.parametrize(
    "text",
    ["location @other { return 204; }", "location = @rate_limited { return 204; }"],
    ids=["another-name", "exact-modifier"],
)
def test_the_parser_admits_one_named_location_and_refuses_any_other(text: str) -> None:
    (named,) = parse_snippet("location @rate_limited { return 429; }").named()
    assert named.key == RATE_LIMITED

    with pytest.raises(AssertionError, match="unexpected named location"):
        parse_snippet(text)


# --------------------------------------------------------------------------- #
# the gateway allow-list
# --------------------------------------------------------------------------- #


def test_the_gateway_snippet_never_forwards_a_websocket_upgrade() -> None:
    text = GATEWAY_SNIPPET.read_text()

    assert "$http_upgrade" not in text
    assert "$connection_upgrade" not in text
    exact = gateway_snippet().exact()
    assert exact
    for location in exact:
        assert len(location.find("limit_except")) == 1, location.key


def test_every_gateway_location_is_exact_except_the_404_catch_all() -> None:
    snippet = gateway_snippet()
    others = [location for location in snippet.locations if location.modifier != "="]

    assert [(location.key, [str(d) for d in location.directives]) for location in others] == [
        ("/", ["return 404"]),
        (RATE_LIMITED, RATE_LIMITED_BODY),
    ]
    for location in snippet.exact():
        assert len(location.methods) == 1, location.key
        (limit,) = location.find("limit_except")
        assert [str(d) for d in limit.block or ()] == ["deny all"], location.key
        # A literal path: nothing request-derived but the query string reaches litellm.
        assert [str(d) for d in location.find("proxy_pass")] == [_proxied(location.path)]


def _effective(snippet: Snippet, location: Location, name: str) -> list[str]:
    own = location.find(name)
    inherited = [d for d in snippet.server if d.name == name]
    return [str(d) for d in (own or inherited)]


def _langfuse_snippet() -> Snippet:
    return parse_snippet(LANGFUSE_SNIPPET.read_text())


# snippet -> (its parse, how many locations it proxies)
PROXYING_SNIPPETS = {
    "gateway": (gateway_snippet, 6),
    "langfuse": (_langfuse_snippet, 1),
}


@pytest.mark.parametrize("which", sorted(PROXYING_SNIPPETS))
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("proxy_request_buffering", "proxy_request_buffering off"),
        ("proxy_max_temp_file_size", "proxy_max_temp_file_size 0"),
        ("proxy_buffering", "proxy_buffering off"),
        ("proxy_http_version", "proxy_http_version 1.1"),
        ("proxy_read_timeout", "proxy_read_timeout 3600s"),
        ("chunked_transfer_encoding", "chunked_transfer_encoding on"),
    ],
)
def test_no_proxied_location_can_write_a_temp_file_or_buffer(
    which: str, name: str, value: str
) -> None:
    # A temp-file fault is a [crit] entry, which carries the request line.
    parse, count = PROXYING_SNIPPETS[which]
    snippet = parse()
    proxied = [location for location in snippet.locations if location.find("proxy_pass")]

    assert len(proxied) == count
    for location in proxied:
        assert _effective(snippet, location, name) == [value], location.key


@pytest.mark.parametrize("which", sorted(PROXYING_SNIPPETS))
def test_the_credentials_pass_through_untouched(which: str) -> None:
    snippet = PROXYING_SNIPPETS[which][0]()
    every = [*snippet.server, *(d for loc in snippet.locations for d in loc.directives)]
    headers = [d.args[0].lower() for d in every if d.name == "proxy_set_header"]

    assert "authorization" not in headers
    assert "x-corp-auth" not in headers
    assert not [d for d in every if d.name in ("proxy_hide_header", "proxy_pass_request_headers")]
    # A location-level proxy_set_header would drop every inherited one there.
    for location in snippet.locations:
        assert not location.find("proxy_set_header"), location.key


def test_the_body_caps_are_25m_and_1k_on_issuance_alone() -> None:
    snippet = gateway_snippet()
    caps = {
        location.key: _effective(snippet, location, "client_max_body_size")
        for location in snippet.exact()
    }

    assert caps.pop("= /internal/issue-token") == ["client_max_body_size 1k"]
    assert set(map(tuple, caps.values())) == {("client_max_body_size 25m",)}


# --------------------------------------------------------------------------- #
# the per-token edge limits
# --------------------------------------------------------------------------- #

GATEWAY_OWNED_PATHS = {"/healthz/live", "/internal/issue-token"}


def _limits(location: Location) -> list[str]:
    return [str(d) for d in location.directives if d.name in ("limit_req", "limit_conn")]


def test_every_admitted_litellm_location_is_limited_per_token() -> None:
    litellm = [loc for loc in gateway_snippet().exact() if loc.path not in GATEWAY_OWNED_PATHS]

    assert {loc.path for loc in litellm} == {
        "/v1/messages",
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/models",
    }
    for location in litellm:
        assert _limits(location) == TOKEN_LIMITS, location.key


def test_issuance_is_limited_by_address_and_the_probe_not_at_all() -> None:
    locations = {location.key: location for location in gateway_snippet().locations}

    # An issuance call carries no corp token yet: the address zone instead.
    assert _limits(locations["= /internal/issue-token"]) == [ISSUE_LIMIT]
    # A load balancer's probe must never be told 429.
    assert _limits(locations["= /healthz/live"]) == []
    for key in ("/", RATE_LIMITED):
        assert _limits(locations[key]) == [], key


def test_both_limits_refuse_with_the_edges_json_429() -> None:
    snippet = gateway_snippet()
    server = [str(d) for d in snippet.server]

    # limit_conn would answer 503 by default, indistinguishable from an outage.
    assert "limit_req_status 429" in server
    assert "limit_conn_status 429" in server
    assert [d for d in server if d.startswith("error_page")] == ["error_page 429 = @rate_limited"]
    (named,) = snippet.named()
    assert [str(d) for d in named.directives] == RATE_LIMITED_BODY


# --------------------------------------------------------------------------- #
# the Langfuse origin
# --------------------------------------------------------------------------- #

# Where each upgrade / ported-Host variable may appear: defined once in the
# http context, consumed by the Langfuse snippet alone. The gateway snippet
# clears Upgrade and Connection and forwards $host.
UPGRADE_AND_HOST_USES = {
    "00-http.conf.template": [
        "map $http_upgrade $connection_upgrade {",
        "map $http_host $proxy_host_header {",
    ],
    "langfuse-locations.inc.template": [
        "proxy_set_header Upgrade $http_upgrade;",
        "proxy_set_header Connection $connection_upgrade;",
        "proxy_set_header Host $proxy_host_header;",
    ],
}


def test_only_the_langfuse_snippet_forwards_an_upgrade_or_a_ported_host() -> None:
    uses: dict[str, list[str]] = {}
    for path in _every_config_file():
        for line in _directives(path.read_text()).splitlines():
            if re.search(r"\$(http_upgrade|connection_upgrade|proxy_host_header)\b", line):
                uses.setdefault(path.name, []).append(line.strip())

    assert uses == UPGRADE_AND_HOST_USES


@pytest.mark.parametrize("which", sorted(PROXYING_SNIPPETS))
def test_no_snippet_forwards_the_scheme_nginx_spoke(which: str) -> None:
    # $scheme is http in behind-proxy; the public side is always https.
    snippet = PROXYING_SNIPPETS[which][0]()
    every = [*snippet.server, *(d for loc in snippet.locations for d in loc.directives)]

    assert [str(d) for d in every if d.name == "proxy_set_header" and "Proto" in d.args[0]] == [
        "proxy_set_header X-Forwarded-Proto https"
    ]
    assert not [str(d) for d in every if "$scheme" in str(d)]


PRODUCTION_FILES = (COMPOSE, OAUTH, ISSUANCE)
STACKS = pytest.mark.parametrize(
    "files", [(COMPOSE,), (COMPOSE, OAUTH), PRODUCTION_FILES], ids=["base", "oauth", "production"]
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
    # Mode A (base) and Mode B (OAuth, with and without issuance) must start the
    # same front door: the auth modes differ behind nginx, never in it.
    base, oauth, production = (
        _portable_service(tmp_path / stack, files, profile)
        for stack, files in (
            ("base", (COMPOSE,)),
            ("oauth", (COMPOSE, OAUTH)),
            ("production", PRODUCTION_FILES),
        )
    )

    assert "corp.example" in base and "9443" in base
    assert oauth == base
    assert production == base


TRUSTED_PEER_GATE = "if ($from_trusted_proxy = 0) { return 444; }"
GATE_MARKER = "corp_trusted_peer_gate"

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
HSTS = 'add_header Strict-Transport-Security "max-age=31536000" always'
# Mozilla "intermediate": a shared cache and no tickets, whose key nginx never
# rotates, which would undo forward secrecy until a restart.
TLS_SESSION = [
    "ssl_session_cache shared:corp_tls:10m",
    "ssl_session_timeout 1d",
    "ssl_session_tickets off",
]
TLS_POLICY = [
    "ssl_protocols TLSv1.2 TLSv1.3",
    f"ssl_ciphers {TLS_CIPHERS}",
    "ssl_prefer_server_ciphers off",
    *TLS_SESSION,
]
CERT_AND_KEY = [
    "ssl_certificate /etc/nginx/certs/NGINX_TLS_CERT",
    "ssl_certificate_key /etc/nginx/certs/NGINX_TLS_KEY",
]
# Beside listen and server_name, the only directives a listener block may hold
# besides its payload. Named one by one: a directive that is not here (another
# ssl_* or add_header) is payload, and payload is pinned below.
TLS_DIRECTIVES = {*TLS_POLICY, *CERT_AND_KEY, "ssl_reject_handshake on", HSTS}


def _listener_blocks(template: Path) -> list[list[str]]:
    """Each server block's directives, placeholders unwrapped and the gate as a marker."""
    body = re.sub(r"#[^\n]*", "", template.read_text())
    # An envsubst placeholder's braces are not nginx blocks.
    body = re.sub(r"\$\{([A-Z_]+)\}", r"\1", body)
    # The trusted-peer gate is the one nested block allowed, and only verbatim.
    body = body.replace(TRUSTED_PEER_GATE, f"{GATE_MARKER};")
    blocks = re.findall(r"server\s*\{([^{}]*)\}", body)
    # A nested brace hides its server block from the pattern above.
    assert blocks, template.name
    assert len(re.findall(r"server\s*\{", body)) == len(blocks), template.name
    return [[d.strip() for d in block.split(";") if d.strip()] for block in blocks]


def test_every_listener_server_block_is_one_include_of_a_snippet() -> None:
    templates = sorted((NGINX_DIR / "templates" / "listeners").glob("*.template"))
    assert templates
    for template in templates:
        gated = template.name.startswith("behind-proxy.")
        for directives in _listener_blocks(template):
            if gated:
                # Before anything else in the block, so nothing runs for an untrusted peer.
                assert directives[0] == GATE_MARKER, (template.name, directives)
                directives = directives[1:]
            payload = [
                d
                for d in directives
                if not d.startswith(("listen ", "server_name ")) and d not in TLS_DIRECTIVES
            ]
            assert payload in (
                ["return 444"],
                ["include /etc/nginx/rendered/snippets/gateway-locations.inc"],
                ["include /etc/nginx/rendered/snippets/langfuse-locations.inc"],
            ), (template.name, directives)


def test_both_behind_proxy_listeners_exist_and_every_server_block_is_gated() -> None:
    listeners = NGINX_DIR / "templates" / "listeners"
    for routing in ("host", "port"):
        text = (listeners / f"behind-proxy.{routing}.conf.template").read_text()
        body = re.sub(r"#[^\n]*", "", text)
        servers = len(re.findall(r"server\s*\{", body))
        assert servers >= 2, routing
        assert body.count(TRUSTED_PEER_GATE) == servers, routing


def test_the_terminate_listeners_do_not_carry_the_trusted_peer_gate() -> None:
    # The terminate listener is the public TLS endpoint: every client is a peer.
    templates = sorted((NGINX_DIR / "templates" / "listeners").glob("terminate.*.template"))
    assert [t.name for t in templates] == [
        "terminate.host.conf.template",
        "terminate.port.conf.template",
    ]
    for template in templates:
        assert "$from_trusted_proxy" not in template.read_text(), template.name


def _listens(directives: list[str]) -> list[str]:
    return [d.removeprefix("listen ") for d in directives if d.startswith("listen ")]


# (listen, what the block serves) per server block, in order.
TERMINATE_BLOCKS = {
    "host": [
        ("8080 ssl default_server", "return 444"),
        ("8080 ssl", "include /etc/nginx/rendered/snippets/gateway-locations.inc"),
        ("8080 ssl", "include /etc/nginx/rendered/snippets/langfuse-locations.inc"),
    ],
    "port": [
        ("8080 ssl default_server", "include /etc/nginx/rendered/snippets/gateway-locations.inc"),
        ("8081 ssl default_server", "include /etc/nginx/rendered/snippets/langfuse-locations.inc"),
    ],
}


@pytest.mark.parametrize("routing", ["host", "port"])
def test_every_terminate_listener_speaks_tls_and_only_tls(routing: str) -> None:
    template = NGINX_DIR / "templates" / "listeners" / f"terminate.{routing}.conf.template"
    blocks = _listener_blocks(template)

    assert [(_listens(d), d[-1]) for d in blocks] == [
        ([listen], served) for listen, served in TERMINATE_BLOCKS[routing]
    ]
    for directives in blocks:
        # nginx negotiates the protocol before SNI picks a server: every block,
        # the default_server included, carries the same policy.
        assert [d for d in directives if d in TLS_POLICY] == TLS_POLICY, directives
        if directives[-1] == "return 444":
            # Host routing's catch-all: no certificate, so no handshake either.
            assert "ssl_reject_handshake on" in directives
            assert not any(d.startswith("ssl_certificate") for d in directives)
            assert HSTS not in directives
        else:
            assert [d for d in directives if d.startswith("ssl_certificate")] == CERT_AND_KEY
            assert directives.count(HSTS) == 1
            assert "ssl_reject_handshake on" not in directives


def test_only_the_host_routing_catch_all_rejects_the_handshake() -> None:
    listeners = NGINX_DIR / "templates" / "listeners"
    rejecting = [
        (template.name, directives[-1])
        for template in sorted(listeners.glob("*.template"))
        for directives in _listener_blocks(template)
        if "ssl_reject_handshake on" in directives
    ]

    assert rejecting == [("terminate.host.conf.template", "return 444")]


@pytest.mark.parametrize("routing", ["host", "port"])
def test_the_behind_proxy_listeners_carry_no_tls_and_no_hsts(routing: str) -> None:
    template = NGINX_DIR / "templates" / "listeners" / f"behind-proxy.{routing}.conf.template"
    for directives in _listener_blocks(template):
        assert not any(d in TLS_DIRECTIVES or d.startswith("ssl_") for d in directives)
        assert all(
            re.fullmatch(r"80(80|81) default_server|80(80|81)", x) for x in _listens(directives)
        )


def test_hsts_is_set_in_the_terminate_listeners_and_on_the_edges_429_over_tls_alone() -> None:
    # Strict-Transport-Security on a plain-HTTP (behind-proxy) response is wrong,
    # and the snippets serve both modes. The 429's add_header replaces the
    # listener's, so it carries its own, empty unless the connection is TLS.
    carriers: dict[str, list[str]] = {}
    for path in _every_config_file():
        lines = [
            line.strip()
            for line in _directives(path.read_text()).splitlines()
            if re.search(r"strict-transport-security|\$hsts_value\b", line, re.IGNORECASE)
        ]
        if lines:
            carriers[path.name] = lines

    assert carriers == {
        "00-http.conf.template": ["map $https $hsts_value {"],
        "gateway-locations.inc.template": [f"{HSTS_ON_TLS};"],
        "terminate.host.conf.template": [f"{HSTS};"] * 2,
        "terminate.port.conf.template": [f"{HSTS};"] * 2,
    }
    assert HSTS_VALUE_MAP in re.sub(r"\s+", " ", _directives(HTTP_TEMPLATE.read_text()))


def test_no_listener_redirects_to_https_or_listens_on_another_port() -> None:
    for template in (NGINX_DIR / "templates" / "listeners").glob("*.template"):
        text = _directives(template.read_text())
        assert not re.search(r"\breturn\s+30[1278]\b|\brewrite\b", text), template.name
        for listen in re.findall(r"^\s*listen\s+([^;]+);", text, re.MULTILINE):
            assert listen.split()[0] in ("8080", "8081"), (template.name, listen)


# --------------------------------------------------------------------------- #
# the http context: the log gate, the trusted-peer check, real_ip, the maps
# --------------------------------------------------------------------------- #

HTTP_TEMPLATE = NGINX_DIR / "templates" / "00-http.conf.template"

# Every variable the access log may carry. None is a header, a cookie, an
# argument, the body, the request line or the Basic-auth user.
SAFE_LOG_VARIABLES = {
    "time_iso8601",
    "realip_remote_addr",
    "from_trusted_proxy",
    "remote_addr",
    "host",
    "server_port",
    "request_method",
    "uri",
    "status",
    "body_bytes_sent",
    "request_time",
    "upstream_addr",
    "upstream_status",
    "upstream_response_time",
}
DIAGNOSTIC_LOG_VARIABLES = {
    "realip_remote_addr",
    "status",
    "upstream_status",
    "upstream_response_time",
    "from_trusted_proxy",
    "uri",
}


def _directives(text: str) -> str:
    return re.sub(r"#[^\n]*", "", text)


def _every_config_file() -> list[Path]:
    return [NGINX_CONF, *sorted((NGINX_DIR / "templates").rglob("*.template"))]


def _log_formats() -> dict[str, str]:
    """name -> the whole directive, across nginx.conf and every template."""
    formats: dict[str, str] = {}
    for path in _every_config_file():
        for match in re.finditer(
            r"^\s*log_format\s+(\S+)\s+([^;]*(?:'[^']*'[^;]*)*);",
            _directives(path.read_text()),
            re.MULTILINE,
        ):
            assert match.group(1) not in formats, match.group(1)
            formats[match.group(1)] = match.group(0)
    return formats


def _log_variables(directive: str) -> set[str]:
    return set(re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", directive))


def test_the_corp_gate_log_format_carries_no_credential_and_no_body() -> None:
    formats = _log_formats()
    assert set(formats) == {"corp_gate"}
    corp_gate = formats["corp_gate"]
    variables = _log_variables(corp_gate)

    assert not re.search(r"authorization|corp_auth", corp_gate, re.IGNORECASE)
    assert not {v for v in variables if v.startswith(("http_", "cookie_", "sent_http_"))}
    assert not {v for v in variables if "body" in v and v != "body_bytes_sent"}
    assert "remote_user" not in variables
    assert variables <= SAFE_LOG_VARIABLES, variables - SAFE_LOG_VARIABLES
    assert variables >= DIAGNOSTIC_LOG_VARIABLES, DIAGNOSTIC_LOG_VARIABLES - variables


def test_no_log_format_carries_the_request_line_or_the_query_string() -> None:
    for name, directive in _log_formats().items():
        variables = _log_variables(directive)
        assert not variables & {"request", "request_uri", "args", "query_string"}, name
        assert not {v for v in variables if v.startswith("arg_")}, name


def test_error_log_is_crit_in_nginx_conf_and_nowhere_else() -> None:
    assert re.findall(
        r"^\s*error_log\s+([^;]+);", _directives(NGINX_CONF.read_text()), re.MULTILINE
    ) == ["/dev/stderr crit"]
    for template in sorted((NGINX_DIR / "templates").rglob("*.template")):
        assert not re.search(r"\berror_log\b", _directives(template.read_text())), template.name


def _depth_at(text: str, offset: int) -> int:
    return text.count("{", 0, offset) - text.count("}", 0, offset)


# Conditional, so the loopback health probe (one request every few seconds) is
# not logged — and still the one access_log directive.
ACCESS_LOG = "access_log /dev/stdout corp_gate if=$corp_loggable;"


def test_only_the_health_listener_is_kept_out_of_the_access_log() -> None:
    body = re.sub(r"\s+", " ", _directives(HTTP_TEMPLATE.read_text()))

    assert "map $server_port $corp_loggable { 8090 0; default 1; }" in body
    assert body.count("$corp_loggable {") == 1
    assert "listen 127.0.0.1:8090;" in body


def test_there_is_exactly_one_access_log_and_it_names_the_format_beside_it() -> None:
    found = [
        (path, match)
        for path in _every_config_file()
        for match in re.finditer(r"\baccess_log\b[^;]*;", _directives(path.read_text()))
    ]
    assert [(path.name, match.group(0)) for path, match in found] == [
        ("00-http.conf.template", ACCESS_LOG)
    ]
    body = re.sub(r"\$\{([A-Z_]+)\}", r"\1", _directives(HTTP_TEMPLATE.read_text()))
    # A quoted log_format string's braces are JSON, not blocks.
    body = re.sub(r"'[^']*'", "''", body)
    access_log = body.index(ACCESS_LOG)
    # The http context: not inside a server, map or geo block of the template.
    assert _depth_at(body, access_log) == 0
    # Immediately after the log_format it names, never before it.
    before = [d.strip() for d in re.split(r"[;{}]", body[:access_log]) if d.strip()]
    assert before[-1].startswith("log_format corp_gate "), before[-1]


def test_the_trusted_peer_check_reads_the_real_peer() -> None:
    body = re.sub(r"\$\{([A-Z_]+)\}", r"@\1@", _directives(HTTP_TEMPLATE.read_text()))
    geo = re.search(r"geo\s+(\S+)\s+(\S+)\s*\{([^{}]*)\}", body)

    assert geo, body
    # $realip_remote_addr is the connecting peer; $remote_addr is what it claims.
    assert (geo.group(1), geo.group(2)) == ("$realip_remote_addr", "$from_trusted_proxy")
    entries = [line.strip() for line in geo.group(3).splitlines() if line.strip()]
    assert entries == ["default 0;", "@TRUSTED_GEO_LINES@"]
    assert len(re.findall(r"^\s*geo\b", body, re.MULTILINE)) == 1


def test_real_ip_trusts_only_the_rendered_list() -> None:
    body = _directives(HTTP_TEMPLATE.read_text())

    assert re.findall(r"^\s*real_ip_header\s+([^;]+);", body, re.MULTILINE) == ["X-Forwarded-For"]
    assert re.findall(r"^\s*\$\{TRUSTED_SET_REAL_IP_LINES\}\s*$", body, re.MULTILINE)
    # The only source of set_real_ip_from is the entrypoint's validated expansion.
    for path in _every_config_file():
        assert "set_real_ip_from" not in _directives(path.read_text()), path.name
    assert "real_ip_recursive" not in body


def test_the_two_shared_maps_are_defined_once() -> None:
    body = re.sub(r"\s+", " ", _directives(HTTP_TEMPLATE.read_text()))

    assert "map $http_upgrade $connection_upgrade { default upgrade; '' close; }" in body
    assert "map $http_host $proxy_host_header { default $http_host; '' $host; }" in body
    assert body.count("$connection_upgrade {") == 1
    assert body.count("$proxy_host_header {") == 1


EDGE_ZONES = [
    "limit_req_zone $corp_token_key zone=corp_token:10m rate=${NGINX_TOKEN_RATE}r/s",
    "limit_conn_zone $corp_token_key zone=corp_conn:10m",
    "limit_req_zone $binary_remote_addr zone=corp_issue:1m rate=${NGINX_ISSUE_RATE}r/m",
]
# Keyed on the token's shape (`ct_` + token_urlsafe(32)): limit_conn skips a key
# over 255 bytes, and a repeated header joins into a fresh key.
TOKEN_KEY_MAP = (
    "map $http_x_corp_auth $corp_token_key {"
    ' "~^ct_[A-Za-z0-9_-]{43}$" $http_x_corp_auth;'
    " default $binary_remote_addr; }"
)


def test_the_http_context_defines_the_three_zones_and_the_token_key() -> None:
    body = re.sub(r"\s+", " ", _directives(HTTP_TEMPLATE.read_text()))
    flat = re.sub(r"'[^']*'", "''", body)

    assert TOKEN_KEY_MAP in body
    assert body.count("$corp_token_key {") == 1
    zones = re.findall(r"\blimit_(?:req|conn)_zone\b[^;]*", body)
    assert zones == EDGE_ZONES
    for zone in zones:
        # The http context: not inside the health server or a map.
        assert _depth_at(flat, flat.index(zone)) == 0, zone
    for path in _every_config_file():
        if path != HTTP_TEMPLATE:
            assert not re.search(r"\blimit_(req|conn)_zone\b", path.read_text()), path.name


def test_every_issued_token_matches_the_key_map_shape() -> None:
    # A token the map does not recognise is keyed by address: every developer
    # behind one NAT would share a bucket.
    shape = re.search(r'"~\^(ct_[^"]*)\$"', HTTP_TEMPLATE.read_text()).group(1)
    for _ in range(200):
        assert re.fullmatch(shape, default_token_factory())


def test_the_token_is_in_no_log_format_and_the_key_only_in_the_zones() -> None:
    for name, directive in _log_formats().items():
        variables = _log_variables(directive)
        assert "http_x_corp_auth" not in variables, name
        assert "corp_token_key" not in variables, name
    uses = [
        line.strip()
        for path in _every_config_file()
        for line in _directives(path.read_text()).splitlines()
        if "$corp_token_key" in line
    ]
    assert uses == [
        "map $http_x_corp_auth $corp_token_key {",
        f"{EDGE_ZONES[0]};",
        f"{EDGE_ZONES[1]};",
    ]


def test_nginxs_limiting_line_is_below_the_error_log_level() -> None:
    # nginx logs "limiting requests, excess: ... by zone" (request line
    # appended) at limit_req_log_level's default `error`, below the `crit` pin.
    for path in _every_config_file():
        text = _directives(path.read_text())
        assert not re.search(r"\blimit_(req|conn)_log_level\b", text), path.name
    assert re.findall(
        r"^\s*error_log\s+([^;]+);", _directives(NGINX_CONF.read_text()), re.MULTILINE
    ) == ["/dev/stderr crit"]


def test_no_config_intercepts_an_upstream_error() -> None:
    # error_page 429 = @rate_limited answers nginx's own refusals only; with
    # interception on, the gateway's E_CAPACITY 429 would be rewritten too.
    for path in _every_config_file():
        text = _directives(path.read_text())
        assert not re.search(r"\bproxy_intercept_errors\b", text), path.name


def test_no_config_reads_the_inbound_x_forwarded_proto() -> None:
    for path in _every_config_file():
        assert "$http_x_forwarded_proto" not in path.read_text().lower(), path.name


def _git_ignores(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    return result.returncode == 0


@pytest.mark.parametrize(
    "name",
    [
        "privkey",
        "server.cer",
        "gateway.pem",
        "gateway.crt",
        "gateway.key",
        "a.p12",
        "a.pfx",
        # What scripts/deploy/make-selfsigned-certs.sh writes, staged names included.
        "selfsigned-ca.crt",
        ".gateway.key.new",
    ],
)
def test_every_file_under_the_nginx_certs_dir_is_ignored(name: str) -> None:
    assert _git_ignores(f"compose/nginx/certs/{name}")


def test_the_nginx_certs_readme_is_not_ignored() -> None:
    assert not _git_ignores("compose/nginx/certs/README.md")


def _tracked(*pathspecs: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspecs],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [name for name in result.stdout.split("\0") if name]


def test_no_certificate_or_key_is_tracked_under_compose() -> None:
    assert _tracked("compose/nginx/certs") == ["compose/nginx/certs/README.md"]
    tracked = _tracked("compose")
    assert tracked
    assert [n for n in tracked if re.search(r"\.(pem|crt|cer|key|csr|p12|pfx)$", n, re.I)] == []
    assert [n for n in tracked if "-----BEGIN" in (ROOT / n).read_text(errors="replace")] == []


def test_the_certs_readme_covers_what_an_operator_must_supply() -> None:
    text = (NGINX_DIR / "certs" / "README.md").read_text()
    for needle in (
        "NGINX_TLS_CERT",
        "NGINX_TLS_KEY",
        "gateway.<GATEWAY_DOMAIN>",
        "langfuse.<GATEWAY_DOMAIN>",
        "IP SAN",
        "0600",
        "on the server",
        "make-selfsigned-certs.sh",
        "for a pilot or a local run",
        "--cacert",
        "ACME",
        "port 80",
    ):
        assert needle in text, needle


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
    "NGINX_TOKEN_RATE",
    "NGINX_TOKEN_BURST",
    "NGINX_TOKEN_CONN",
    "NGINX_ISSUE_RATE",
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

    assert compose_file < profiles
    between = lines[compose_file + 1 : profiles]
    assert not [line for line in between if re.match(r"[A-Z_]+=", line)], between
    first = next(line for line in lines[compose_file + 1 :] if line.strip())
    assert first.startswith("# ---- nginx front door"), first


def _langfuse_public_url_block() -> list[str]:
    """The comment above the shipped LANGFUSE_PUBLIC_URL line, and that line."""
    lines = ENV_EXAMPLE.read_text().splitlines()
    (index,) = [i for i, line in enumerate(lines) if line.startswith("LANGFUSE_PUBLIC_URL=")]
    start = index
    while lines[start - 1].startswith("#"):
        start -= 1
    return lines[start : index + 1]


def test_the_env_example_ships_the_tunnel_default_for_langfuse() -> None:
    # Right for a tunnel; nginx's entrypoint refuses it, so a profile forces a change.
    assert _langfuse_public_url_block()[-1] == "LANGFUSE_PUBLIC_URL=http://localhost:3000"


def test_the_env_example_says_what_langfuse_public_url_must_be_under_each_profile() -> None:
    comment = " ".join(line.removeprefix("#").strip() for line in _langfuse_public_url_block()[:-1])

    assert "COMPOSE_PROFILES" in comment
    assert "https://langfuse.<GATEWAY_DOMAIN> under `nginx`" in comment
    assert "https://<address>:<NGINX_LANGFUSE_PORT> under `nginx-ports`" in comment
    assert "cannot both be correct" in comment
    # The entrypoint's step-7 rule, stated where the value is set.
    for rule in ("`https://<host>[:port]`", "no path", "no credentials", "no bracketed IPv6"):
        assert rule in comment, rule
    assert 'compose/README.md, "Reaching the UI"' in comment
    assert "### Reaching the UI" in (COMPOSE_DIR / "README.md").read_text()


# The entrypoint's step-7a defaults.
EDGE_DEFAULTS = {
    "NGINX_TOKEN_RATE": "10",
    "NGINX_TOKEN_BURST": "20",
    "NGINX_TOKEN_CONN": "8",
    "NGINX_ISSUE_RATE": "5",
}


def test_the_env_example_shows_the_edge_defaults_beside_the_gateway_caps_they_front() -> None:
    text = ENV_EXAMPLE.read_text()
    start = text.index("# ---- nginx front door")
    block = text[start : text.index("\n# ----", start + 1)]

    entrypoint = (NGINX_DIR / "entrypoint.sh").read_text()
    for key, default in EDGE_DEFAULTS.items():
        assert f"\n# {key}={default}\n" in block, key
        assert f"\n{key}=${{{key}:-{default}}}\n" in entrypoint, key
    assert "CORP_LLM_MAX_INFLIGHT" in block
    assert "CORP_GATEWAY_ISSUE_RATE_PER_MINUTE" in block
    assert "E_RATE_LIMITED" in block and "E_CAPACITY" in block
