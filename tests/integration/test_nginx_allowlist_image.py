"""The nginx front door in front of the real gateway image.

Two things only the shipped image can show:

* **its route table** — litellm's app as the image imports it, with every lazy
  feature warmed (``nginx_route_dump.py``), checked against the allow-list by the
  same two properties the ``ast`` guard checks (``tests/compose/nginx_allowlist.py``);
* **the guardrail runs behind nginx** — a canary sent through nginx to the gateway
  container arrives at the stub provider as a placeholder, with an audit record,
  on every admitted ``POST``; and ``POST /internal/issue-token`` terminates in the
  gateway's ``HealthRouter``, never in litellm.

The gateway runs in the OAuth posture: no ``LITELLM_MASTER_KEY``, the developer's
bearer forwarded — the production mode, and the one where litellm's own auth is
weakest. nginx joins the gateway's egress-blocked network, where the gateway
answers as ``litellm``; the stub provider is still the only upstream it can reach.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.compose.nginx_allowlist import (
    BLOCKED_AT_NGINX,
    WEBSOCKET,
    declared_pairs,
    gateway_owned,
    gateway_paths_litellm_registers,
    gateway_snippet,
    missing_from_litellm,
    stale_blocked_entries,
    unaccounted_at_admitted_paths,
)
from tests.compose.nginx_container import (
    Running,
    Spec,
    copy_nginx_dir,
    docker,
    nginx_specs,
    started,
    user_network,
)
from tests.compose.nginx_support import COMPOSE, OAUTH, run_status_cli
from tests.integration.conftest import ROOT, Stack, running_stack, skip_or_fail
from tests.integration.route_gate_stub_provider import SSE_DELAY_SECONDS

HERE = Path(__file__).resolve().parent
DUMP_SCRIPT = HERE / "nginx_route_dump.py"
DUMP_MARKER = "@@ROUTES@@"

DOMAIN = "example.test"
GATEWAY_HOST = f"gateway.{DOMAIN}"
NGINX_ENV: dict[str, str | None] = {
    "NGINX_TLS_MODE": "behind-proxy",
    "GATEWAY_DOMAIN": DOMAIN,
    "NGINX_TRUSTED_PROXIES": "10.0.0.0/8",
    "LANGFUSE_PUBLIC_URL": f"https://langfuse.{DOMAIN}",
}

DECLARED = declared_pairs(gateway_snippet())
ADMITTED_PATHS = {path for _, path in DECLARED}


# --------------------------------------------------------------------------- #
# the warmed route table of the pinned image
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Dump:
    pairs: set[tuple[str, str]]
    unknown: list[str]
    failed: list[str]

    @property
    def mounts(self) -> set[str]:
        return {path for method, path in self.pairs if method == "MOUNT"}

    @property
    def routes(self) -> set[tuple[str, str]]:
        return {(method, path) for method, path in self.pairs if method != "MOUNT"}


@pytest.fixture(scope="module")
def dump(gateway_image: str) -> Dump:
    argv = ["run", "--rm", "-i", "--network", "none", "--entrypoint", "/app/.venv/bin/python"]
    result = subprocess.run(
        ["docker", *argv, gateway_image, "-"],
        input=DUMP_SCRIPT.read_text(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith(DUMP_MARKER)]
    assert result.returncode == 0 and len(lines) == 1, (
        f"the route dump failed in {gateway_image} (exit {result.returncode}) — if "
        f"litellm renamed _force_load, re-read how lazy loading works:\n{result.stderr[-3000:]}"
    )
    raw = json.loads(lines[0].removeprefix(DUMP_MARKER))
    return Dump({tuple(pair) for pair in raw["pairs"]}, raw["unknown"], raw["failed"])


def test_the_dump_knows_every_route_class_and_warmed_every_lazy_feature(dump: Dump) -> None:
    # A route class nobody mapped to (method, path) pairs could hide a WebSocket;
    # a lazy feature that failed to load hides its routers.
    assert dump.unknown == []
    assert dump.failed == []


def test_the_warm_up_worked_and_the_websocket_is_seen(dump: Dump) -> None:
    # POST /v1/messages exists only once the anthropic_passthrough feature is
    # loaded; the WebSocket is the one BLOCKED_AT_NGINX entry, checked, not vacuous.
    assert ("POST", "/v1/messages") in dump.routes
    assert (WEBSOCKET, "/v1/responses") in dump.routes


def test_every_admitted_litellm_pair_is_served_by_the_image(dump: Dump) -> None:
    assert gateway_owned(DECLARED) == {("GET", "/healthz/live"), ("POST", "/internal/issue-token")}
    assert missing_from_litellm(DECLARED, dump.routes) == []
    assert gateway_paths_litellm_registers(DECLARED, dump.routes) == []


def test_every_pair_the_image_serves_at_an_admitted_path_is_admitted_or_blocked(
    dump: Dump,
) -> None:
    assert unaccounted_at_admitted_paths(DECLARED, dump.routes) == []
    assert stale_blocked_entries(DECLARED, dump.routes) == []
    assert set(BLOCKED_AT_NGINX) <= dump.routes


def test_no_mount_sits_at_or_above_an_admitted_path(dump: Dump) -> None:
    assert dump.mounts, "the dump found no Mount; the warm-up mounts /mcp"
    for mount in dump.mounts:
        prefix = mount.rstrip("/") + "/"
        covered = sorted(p for p in ADMITTED_PATHS if p == mount or p.startswith(prefix))
        assert covered == [], (mount, covered)


# --------------------------------------------------------------------------- #
# nginx -> the gateway container -> the stub provider
# --------------------------------------------------------------------------- #

# An email, so the local-first regex floor redacts it without the NER extras
# the `base` image profile leaves out.
CANARY = "nginx.front.canary.7d21@corp.lan"
CORP_TOKEN = "nginx-front-door-team-token"
OAUTH_TOKEN = "sk-ant-oat01-fake-nginx-front-door"
ANTHROPIC_MODEL = "claude-3-5-sonnet-20241022"
OPENAI_MODEL = "gpt-4o-mini"
STUB_BASE = "http://stub:8000"

GATEWAY_CONFIG = f"""
model_list:
  - model_name: "claude-*"
    litellm_params:
      model: "anthropic/{ANTHROPIC_MODEL}"
      api_base: "{STUB_BASE}"
      api_key: "oauth-passthrough-placeholder"
  - model_name: "gpt-*"
    litellm_params:
      model: "openai/{OPENAI_MODEL}"
      api_base: "{STUB_BASE}/v1"
      api_key: "stub-openai-key"
litellm_settings:
  callbacks: ["corp_llm_gateway.bootstrap.guardrail"]
  drop_params: true
  json_logs: true
"""

BASE_ENV = {
    "CORP_LLM_ORACLE_ENABLED": "0",
    "CORP_LLM_LOCAL_FIRST": "1",
    "CORP_AUDIT_SINK": "stdout",
    "CORP_METRICS_EXPORTER": "prometheus",
    "CORP_LLM_DEV_TEAM_TOKEN": CORP_TOKEN,
    "CORP_ENV": "dev",
    "CORP_LLM_STRIP_INBOUND_HEADERS": "1",
    # No LITELLM_MASTER_KEY at all: the OAuth posture.
    "CORP_LLM_FORWARD_ANTHROPIC_AUTH": "1",
}

ISSUER = "http://keycloak.invalid/realms/corp"
ISSUANCE_TOML = '[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]\n"/devs" = "t1"\n'
ISSUANCE_ENV = {
    "CORP_LLM_GATEWAY_CONFIG_FILE": "/etc/corp-llm-gateway/config.toml",
    "CORP_GATEWAY_ISSUE_OIDC_ISSUER": ISSUER,
    "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE": "corp-gateway-issuance",
    "CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID": "corp-gateway-cli",
    "CORP_LLM_PG_DSN": "postgresql://gw:gw@pg:5432/litellm",
}


@dataclass
class FrontDoor:
    stack: Stack
    nginx: Running

    def request(
        self, method: str, path: str, *, body: bytes | None = None, **headers: str
    ) -> httpx.Response:
        url = f"http://127.0.0.1:{self.nginx.ports[8080]}{path}"
        with httpx.Client(trust_env=False, timeout=120) as client:
            return client.request(
                method,
                url,
                content=body,
                headers={
                    "Host": GATEWAY_HOST,
                    "content-type": "application/json",
                    "Authorization": f"Bearer {OAUTH_TOKEN}",
                    "X-Corp-Auth": CORP_TOKEN,
                    **headers,
                },
            )


@pytest.fixture(scope="module")
def front_door_spec(tmp_path_factory: pytest.TempPathFactory) -> Spec:
    # The OAuth overlay's merged render, host routing.
    return nginx_specs(tmp_path_factory, COMPOSE, OAUTH)["host"]


@contextlib.contextmanager
def front_door(
    image: str,
    spec: Spec,
    tmp_path: Path,
    env: Mapping[str, str],
    *,
    gateway_args: Sequence[str] = (),
    postgres_sql: str | None = None,
) -> Iterator[FrontDoor]:
    """The gateway stack, answering as ``litellm``, with nginx joined to its network."""
    config = tmp_path / "config.yaml"
    config.write_text(GATEWAY_CONFIG)
    with (
        running_stack(
            image,
            config,
            env,
            with_postgres=postgres_sql is not None,
            postgres_sql=postgres_sql,
            gateway_args=["--network-alias", "litellm", *gateway_args],
        ) as stack,
        user_network(spec.image) as network,
        started(spec, copy_nginx_dir(tmp_path), network, NGINX_ENV) as nginx,
    ):
        connected = docker("network", "connect", stack.network, nginx.name)
        if connected.returncode != 0:
            skip_or_fail(f"cannot attach nginx to the gateway network: {connected.stderr}")
        yield FrontDoor(stack, nginx)


@pytest.fixture(scope="module")
def oauth_front_door(
    gateway_image: str, front_door_spec: Spec, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[FrontDoor]:
    with front_door(
        gateway_image, front_door_spec, tmp_path_factory.mktemp("front-door"), BASE_ENV
    ) as door:
        yield door


@pytest.fixture(scope="module")
def issuing_front_door(
    gateway_image: str, front_door_spec: Spec, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[FrontDoor]:
    tmp_path = tmp_path_factory.mktemp("issuing-front-door")
    gateway_toml = tmp_path / "gateway.toml"
    gateway_toml.write_text(ISSUANCE_TOML)
    schema = (ROOT / "src/corp_llm_gateway/tokens/schema.sql").read_text()
    with front_door(
        gateway_image,
        front_door_spec,
        tmp_path,
        {**BASE_ENV, **ISSUANCE_ENV},
        gateway_args=["-v", f"{gateway_toml}:/etc/corp-llm-gateway/config.toml:ro"],
        postgres_sql=schema,
    ) as door:
        yield door


ADMITTED_POSTS: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "/v1/messages?beta=true",
        {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": f"escalate to {CANARY}"}],
        },
    ),
    (
        "/v1/chat/completions",
        {"model": OPENAI_MODEL, "messages": [{"role": "user", "content": f"escalate to {CANARY}"}]},
    ),
    ("/v1/responses", {"model": OPENAI_MODEL, "input": f"escalate to {CANARY}"}),
)


def test_every_admitted_post_is_named_here() -> None:
    posts = {("POST", path.split("?")[0]) for path, _ in ADMITTED_POSTS}
    assert posts == {pair for pair in DECLARED if pair[0] == "POST"} - gateway_owned(DECLARED)


def _audit_record(stack: Stack, request_id: str) -> dict[str, Any]:
    records = [r for r in stack.audit_records() if str(r["request_id"]) == request_id]
    assert len(records) == 1, (request_id, records)
    return records[0]


@pytest.mark.parametrize(("path", "payload"), ADMITTED_POSTS, ids=[p for p, _ in ADMITTED_POSTS])
def test_the_canary_reaches_the_provider_as_a_placeholder_through_nginx(
    oauth_front_door: FrontDoor, path: str, payload: dict[str, Any]
) -> None:
    stack = oauth_front_door.stack
    stack.mark()

    response = oauth_front_door.request("POST", path, body=json.dumps(payload).encode())

    assert response.status_code == 200, response.text
    captured = stack.new_captures()
    assert len(captured) == 1, captured
    sent = json.dumps(captured[0]["body"])
    # The two observable proofs that the guardrail ran: what left carries the
    # placeholder and never the original, and the request was audited.
    assert CANARY not in sent
    assert "EMAIL" in sent, sent[:400]
    record = _audit_record(stack, response.headers["x-litellm-call-id"])
    assert (record["status"], record["redaction_count"]) == ("ok", 1), record
    assert CANARY not in json.dumps(record)


def test_count_tokens_is_nginxs_404_and_reaches_nothing(oauth_front_door: FrontDoor) -> None:
    stack = oauth_front_door.stack
    stack.mark()
    payload = {
        "model": ANTHROPIC_MODEL,
        "messages": [{"role": "user", "content": f"write to {CANARY}"}],
    }

    response = oauth_front_door.request(
        "POST", "/v1/messages/count_tokens", body=json.dumps(payload).encode()
    )

    assert response.status_code == 404
    # nginx's page, not the gateway's JSON refusal: the request never left nginx.
    assert "E_ROUTE_BLOCKED" not in response.text
    assert stack.new_captures() == []
    # The container is module-scoped and already holds entries: wait for this one.
    deadline = time.monotonic() + 10
    while True:
        entries = oauth_front_door.nginx.access_log(expected=0)
        counted = [e for e in entries if e["uri"] == "/v1/messages/count_tokens"]
        if counted or time.monotonic() > deadline:
            break
        time.sleep(0.2)
    assert counted, f"no /v1/messages/count_tokens entry within 10 s: {entries}"
    assert [e["status"] for e in counted] == ["404"]
    assert counted[0]["upstream_status"] in ("", "-")


def _stream(url: str, **headers: str) -> tuple[int, list[float], bytes]:
    """The status, each non-empty chunk's arrival (seconds after sending), and
    the bytes, for one streamed ``/v1/messages`` call."""
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 64,
        "stream": True,
        "messages": [{"role": "user", "content": f"escalate to {CANARY}"}],
    }
    arrivals: list[float] = []
    received = b""
    with httpx.Client(trust_env=False, timeout=120) as client:
        sent_at = time.monotonic()
        with client.stream(
            "POST",
            url,
            content=json.dumps(payload).encode(),
            headers={
                "content-type": "application/json",
                "Authorization": f"Bearer {OAUTH_TOKEN}",
                "X-Corp-Auth": CORP_TOKEN,
                **headers,
            },
        ) as response:
            for chunk in response.iter_raw():
                if chunk.strip():
                    arrivals.append(time.monotonic() - sent_at)
                    received += chunk
    return response.status_code, arrivals, received


def test_a_stream_through_nginx_arrives_as_spread_out_as_from_the_gateway(
    oauth_front_door: FrontDoor,
) -> None:
    """The stub provider spaces its events. The gateway holds a little back
    (a placeholder can straddle two events), so the baseline is the same stream
    read from the gateway directly: nginx must not bunch it up any further."""
    stack = oauth_front_door.stack
    stack.mark()

    direct = _stream(f"{stack.base_url}/v1/messages")
    through = _stream(
        f"http://127.0.0.1:{oauth_front_door.nginx.ports[8080]}/v1/messages", Host=GATEWAY_HOST
    )

    for status, arrivals, received in (direct, through):
        assert status == 200, received[:400]
        assert b"message_stop" in received
        assert len(arrivals) >= 2, arrivals
    captured = stack.new_captures()
    assert len(captured) == 2, captured
    assert all(CANARY not in json.dumps(c["body"]) for c in captured)
    direct_spread = direct[1][-1] - direct[1][0]
    through_spread = through[1][-1] - through[1][0]
    # Several of the stub's gaps survive the gateway, so bunching would show.
    assert direct_spread >= 2 * SSE_DELAY_SECONDS, direct[1]
    assert through_spread >= direct_spread - SSE_DELAY_SECONDS / 2, (direct[1], through[1])


def test_the_status_cli_is_healthy_through_nginx_and_readiness_stays_inside(
    oauth_front_door: FrontDoor, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    (home / ".corp-llm-gateway").mkdir(parents=True)
    (home / ".corp-llm-gateway" / "token").write_text(CORP_TOKEN + "\n")
    url = f"http://{GATEWAY_HOST}:{oauth_front_door.nginx.ports[8080]}"

    code, report = run_status_cli(url, home, resolve=GATEWAY_HOST)
    outside = oauth_front_door.request("GET", "/healthz/ready")
    with httpx.Client(trust_env=False, timeout=60) as client:
        inside = client.get(f"{oauth_front_door.stack.base_url}/healthz/ready")

    assert (code, report["live"], report["token_present"], report["healthy"]) == (
        0,
        True,
        True,
        True,
    )
    # The gateway serves readiness; nginx does not admit it from outside.
    assert inside.status_code == 200, inside.text
    assert outside.status_code == 404
    assert "E_ROUTE_BLOCKED" not in outside.text


def _error_code(response: httpx.Response) -> str:
    # The HealthRouter's issuance shape: {"error": "<code>"}.
    body = response.json()
    assert set(body) == {"error"} and isinstance(body["error"], str), body
    return body["error"]


def test_issuance_off_is_the_gateways_own_404(oauth_front_door: FrontDoor) -> None:
    stack = oauth_front_door.stack
    stack.mark()

    empty = oauth_front_door.request("POST", "/internal/issue-token")
    with_body = oauth_front_door.request("POST", "/internal/issue-token", body=b"x")

    # E_ISSUE_DISABLED is the HealthRouter's answer; litellm has no such code.
    assert (empty.status_code, _error_code(empty)) == (404, "E_ISSUE_DISABLED")
    assert (with_body.status_code, _error_code(with_body)) == (404, "E_ISSUE_DISABLED")
    assert stack.new_captures() == []


def test_issuance_on_refuses_a_one_byte_body_in_the_gateway(
    issuing_front_door: FrontDoor,
) -> None:
    stack = issuing_front_door.stack
    stack.mark()

    response = issuing_front_door.request("POST", "/internal/issue-token", body=b"x")

    assert (response.status_code, _error_code(response)) == (400, "E_ISSUE_BODY")
    assert stack.new_captures() == []
