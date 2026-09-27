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

from tests.compose.nginx_allowlist import (
    GATEWAY_SNIPPET,
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
]

# What the front door admits: the gateway's allow-list ("The security
# constraint"), and nothing on Langfuse yet — Task 5 replaces that pin.
ADMITTED_LOCATIONS = {
    "gateway-locations.inc.template": (
        GATEWAY_SERVER_DIRECTIVES,
        {
            "= /v1/messages": ["limit_except POST { deny all }", _proxied("/v1/messages")],
            "= /v1/chat/completions": [
                "limit_except POST { deny all }",
                _proxied("/v1/chat/completions"),
            ],
            "= /v1/responses": ["limit_except POST { deny all }", _proxied("/v1/responses")],
            "= /v1/models": ["limit_except GET { deny all }", _proxied("/v1/models")],
            "= /healthz/live": ["limit_except GET { deny all }", _proxied("/healthz/live")],
            "= /internal/issue-token": [
                "limit_except POST { deny all }",
                "client_max_body_size 1k",
                _proxied("/internal/issue-token"),
            ],
            "/": ["return 404"],
        },
    ),
    "langfuse-locations.inc.template": ([], {"/": ["return 404"]}),
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
        ("/", ["return 404"])
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


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("proxy_request_buffering", "proxy_request_buffering off"),
        ("proxy_max_temp_file_size", "proxy_max_temp_file_size 0"),
        ("proxy_buffering", "proxy_buffering off"),
        ("proxy_http_version", "proxy_http_version 1.1"),
    ],
)
def test_no_proxied_location_can_write_a_temp_file_or_buffer(name: str, value: str) -> None:
    # A temp-file fault is a [crit] entry, which carries the request line.
    snippet = gateway_snippet()
    proxied = [location for location in snippet.locations if location.find("proxy_pass")]

    assert len(proxied) == 6
    for location in proxied:
        assert _effective(snippet, location, name) == [value], location.key


def test_the_credentials_pass_through_untouched() -> None:
    snippet = gateway_snippet()
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


TRUSTED_PEER_GATE = "if ($from_trusted_proxy = 0) { return 444; }"
GATE_MARKER = "corp_trusted_peer_gate"


def test_every_listener_server_block_is_one_include_of_a_snippet() -> None:
    templates = sorted((NGINX_DIR / "templates" / "listeners").glob("*.template"))
    assert templates
    for template in templates:
        body = re.sub(r"#[^\n]*", "", template.read_text())
        # An envsubst placeholder's braces are not nginx blocks.
        body = re.sub(r"\$\{([A-Z_]+)\}", r"\1", body)
        # The trusted-peer gate is the one nested block allowed, and only verbatim.
        body = body.replace(TRUSTED_PEER_GATE, f"{GATE_MARKER};")
        blocks = re.findall(r"server\s*\{([^{}]*)\}", body)
        # A nested brace hides its server block from the pattern above.
        assert blocks, template.name
        assert len(re.findall(r"server\s*\{", body)) == len(blocks), template.name
        gated = template.name.startswith("behind-proxy.")
        for block in blocks:
            directives = [d.strip() for d in block.split(";") if d.strip()]
            if gated:
                # Before anything else in the block, so nothing runs for an untrusted peer.
                assert directives[0] == GATE_MARKER, (template.name, directives)
                directives = directives[1:]
            payload = [
                d for d in directives if not d.startswith(("listen ", "server_name ", "ssl_"))
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
    for template in (NGINX_DIR / "templates" / "listeners").glob("terminate.*.template"):
        assert "$from_trusted_proxy" not in template.read_text(), template.name


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
    "name", ["privkey", "server.cer", "gateway.pem", "gateway.crt", "gateway.key", "a.p12", "a.pfx"]
)
def test_every_file_under_the_nginx_certs_dir_is_ignored(name: str) -> None:
    assert _git_ignores(f"compose/nginx/certs/{name}")


def test_the_nginx_certs_readme_is_not_ignored() -> None:
    assert not _git_ignores("compose/nginx/certs/README.md")


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
