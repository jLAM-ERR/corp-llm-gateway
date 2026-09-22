"""The route gate on the real gateway image, in both auth modes.

Every other route-gate test reads the table, the classifier or the middleware in
process. This one runs the image the cluster runs — ``python -m
corp_llm_gateway.serve`` inside ``Dockerfile.gateway`` — on a docker network with
**no route off it**, with a stub provider (``route_gate_stub_provider.py``) as the
only reachable upstream. A refusal that leaked would show up as a capture in the
stub's log; there is nowhere else for it to go.

Two modes, the two the deployment supports:

* **Mode A** — litellm virtual keys. ``LITELLM_MASTER_KEY`` is set, Postgres is
  attached so the entrypoint's Prisma schema sequence runs, and requests carry a
  virtual key minted through ``POST /key/generate``.
* **Mode B** — Anthropic subscription (OAuth) passthrough. No master key at all
  (``settings.master_key_conflict`` refuses the combination), one native
  ``anthropic/`` route, and the developer's own bearer as the credential.

Both carry ``X-Corp-Auth``; the gate runs before litellm's auth, so a refusal here
is provably the gate's and not a 401.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.conftest import (
    Stack,
    booting_gateway,
    docker,
    running_stack,
    skip_or_fail,
    wait_for_exit,
)

# An email, so the local-first regex floor redacts it without the NER extras the
# `base` image profile leaves out. Whatever the stub receives must carry the
# placeholder, never this.
CANARY = "route.gate.canary.a41f@corp.lan"

CORP_TOKEN = "route-gate-team-token"
MASTER_KEY = "sk-corp-route-gate-master"
# Obvious fake; the `sk-ant-oat` prefix is the only part litellm reads.
OAUTH_TOKEN = "sk-ant-oat01-fake-route-gate-container"

ANTHROPIC_MODEL = "claude-3-5-sonnet-20241022"
OPENAI_MODEL = "gpt-4o-mini"

STUB_BASE = "http://stub:8000"

MODE_A_CONFIG = f"""
model_list:
  - model_name: "claude-*"
    litellm_params:
      model: "anthropic/{ANTHROPIC_MODEL}"
      api_base: "{STUB_BASE}"
      api_key: "stub-anthropic-key"
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

# The shape compose/litellm/config.oauth.yaml ships: one native anthropic route,
# no catch-all, no gateway-held key.
MODE_B_CONFIG = f"""
model_list:
  - model_name: "claude-*"
    litellm_params:
      model: "anthropic/{ANTHROPIC_MODEL}"
      api_base: "{STUB_BASE}"
      api_key: "oauth-passthrough-placeholder"
litellm_settings:
  callbacks: ["corp_llm_gateway.bootstrap.guardrail"]
  drop_params: true
  json_logs: true
"""

NO_CALLBACK_CONFIG = f"""
model_list:
  - model_name: "claude-*"
    litellm_params:
      model: "anthropic/{ANTHROPIC_MODEL}"
      api_base: "{STUB_BASE}"
      api_key: "stub-anthropic-key"
litellm_settings:
  drop_params: true
  json_logs: true
"""

BASE_ENV = {
    # Offline and deterministic: the oracle is a second upstream this harness
    # does not mock, and the local-first cascade is what the body depends on.
    "CORP_LLM_ORACLE_ENABLED": "0",
    "CORP_LLM_LOCAL_FIRST": "1",
    "CORP_AUDIT_SINK": "stdout",
    "CORP_METRICS_EXPORTER": "prometheus",
    "CORP_LLM_DEV_TEAM_TOKEN": CORP_TOKEN,
    "CORP_ENV": "dev",
    # What compose/.env.example sets. The guardrail puts the inbound wire headers
    # into `data["headers"]` and litellm forwards them, so without this the
    # client's `content-length` rides along and the upstream body is cut to that
    # length whenever sanitization made it longer (wire-verified here).
    "CORP_LLM_STRIP_INBOUND_HEADERS": "1",
}


@dataclass
class Mode:
    """A running stack plus the credentials that mode authenticates with."""

    name: str
    stack: Stack
    key: str
    admin_key: str | None = None

    def headers(self, **extra: str) -> dict[str, str]:
        return {
            "content-type": "application/json",
            "Authorization": f"Bearer {self.key}",
            "X-Corp-Auth": CORP_TOKEN,
            **extra,
        }

    def post(self, path: str, payload: Any, **kwargs: Any) -> httpx.Response:
        return httpx.post(
            f"{self.stack.base_url}{path}",
            json=payload,
            headers=self.headers(),
            timeout=90,
            **kwargs,
        )

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return httpx.get(
            f"{self.stack.base_url}{path}", headers=self.headers(), timeout=60, **kwargs
        )


def _raw_get(
    mode: Mode, path: str, *, extra_headers: tuple[tuple[str, str], ...] = ()
) -> tuple[int, str]:
    """A GET written byte by byte, so the request line reaches uvicorn verbatim."""
    url = httpx.URL(mode.stack.base_url)
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {url.host}:{url.port}",
        "Connection: close",
        f"Authorization: Bearer {mode.key}",
        f"X-Corp-Auth: {CORP_TOKEN}",
        *(f"{name}: {value}" for name, value in extra_headers),
    ]
    # `Connection: close` unless the caller asked to upgrade — the reader below
    # waits for EOF.
    if extra_headers:
        lines = [line for line in lines if line != "Connection: close"]
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode()

    received = b""
    with socket.create_connection((url.host, url.port), timeout=60) as sock:
        sock.sendall(raw)
        sock.settimeout(30)
        try:
            while chunk := sock.recv(65536):
                received += chunk
        except OSError:
            pass
    head, _, body = received.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, body.decode("utf-8", "replace")


def _json_log_records(logs: str) -> list[Any]:
    records = []
    for line in logs.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def _write(tmp_path_factory: pytest.TempPathFactory, name: str, text: str) -> Path:
    path = tmp_path_factory.mktemp(name) / "config.yaml"
    path.write_text(text)
    return path


@pytest.fixture(scope="module")
def mode_a(gateway_image: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Mode]:
    config = _write(tmp_path_factory, "route-gate-a", MODE_A_CONFIG)
    env = {**BASE_ENV, "LITELLM_MASTER_KEY": MASTER_KEY}
    with running_stack(gateway_image, config, env, with_postgres=True) as stack:
        minted = httpx.post(
            f"{stack.base_url}/key/generate",
            headers={"Authorization": f"Bearer {MASTER_KEY}", "content-type": "application/json"},
            json={"models": ["claude-*", "gpt-*"], "duration": "1h"},
            timeout=90,
        )
        assert minted.status_code == 200, minted.text
        yield Mode(name="A", stack=stack, key=minted.json()["key"], admin_key=MASTER_KEY)


@pytest.fixture(scope="module")
def mode_b(gateway_image: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Mode]:
    config = _write(tmp_path_factory, "route-gate-b", MODE_B_CONFIG)
    # No LITELLM_MASTER_KEY at all: present-but-empty is still a conflict.
    env = {**BASE_ENV, "CORP_LLM_FORWARD_ANTHROPIC_AUTH": "1"}
    with running_stack(gateway_image, config, env) as stack:
        yield Mode(name="B", stack=stack, key=OAUTH_TOKEN)


@pytest.fixture(params=["mode_a", "mode_b"])
def mode(request: pytest.FixtureRequest) -> Mode:
    return request.getfixturevalue(request.param)


# ── the harness's own premise: the gateway has no route off the network ──────

_CONNECT_PROBE = "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)"
_DNS_PROBE = "import socket; socket.getaddrinfo('api.anthropic.com', 443)"


# What a blocked socket looks like from inside the container. A `docker exec`
# that cannot run at all also exits non-zero (126/127), so the failure has to be
# provably the network's, not the harness's.
_NETWORK_ERRORS = ("OSError", "gaierror", "timed out", "Network is unreachable")


@pytest.mark.parametrize("probe", [_CONNECT_PROBE, _DNS_PROBE], ids=["connect", "dns"])
def test_the_gateway_container_cannot_reach_the_internet(mode: Mode, probe: str) -> None:
    # Everything else in this file reads "the stub saw nothing" as "nothing
    # leaked". That only holds while the gateway's only network is `--internal`:
    # if docker ever attached a default bridge as well, a refusal that leaked
    # would reach the real provider and every assertion here would still pass.
    control = docker(
        "exec", mode.stack.gateway, "/app/.venv/bin/python", "-c", "print(1)", timeout=60
    )
    assert control.returncode == 0, (
        f"`docker exec` itself cannot run in this container, so the probe below "
        f"proves nothing\n{control.stdout}\n{control.stderr}"
    )
    assert control.stdout.strip() == "1"

    result = docker("exec", mode.stack.gateway, "/app/.venv/bin/python", "-c", probe, timeout=60)

    assert result.returncode != 0, (
        f"the gateway container reached off its network: {probe}\n{result.stdout}"
    )
    assert any(marker in result.stderr for marker in _NETWORK_ERRORS), (
        f"the probe failed for a reason other than the blocked network: {probe}\n{result.stderr}"
    )


# ── startup negatives: the gateway does not serve half-configured ────────────


MISSING_CONFIG = "/nonexistent/config.yaml"


def test_a_missing_litellm_config_exits_78(gateway_image: str) -> None:
    # litellm's own lifespan skips a missing config in silence and would serve
    # with no guardrail at all; the entrypoint refuses first. The env var is set
    # as well as the mount left out, so the refusal is provably about the path
    # this test names and not about the image's default mount point.
    env = {**BASE_ENV, "CORP_LLM_LITELLM_CONFIG": MISSING_CONFIG}
    with booting_gateway(gateway_image, MISSING_CONFIG, env) as name:
        assert wait_for_exit(name, timeout=90) == 78


def test_a_config_without_the_callback_exits_70(
    gateway_image: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # The fail-open this plan exists to close: litellm starts happily with no
    # callback and sanitizes nothing. The exit happens after litellm's startup,
    # inside the lifespan wrapper.
    config = _write(tmp_path_factory, "route-gate-nocb", NO_CALLBACK_CONFIG)
    with booting_gateway(gateway_image, config, BASE_ENV) as name:
        assert wait_for_exit(name, timeout=300) == 70


def test_mode_a_ran_the_prisma_schema_setup_at_boot(mode_a: Mode) -> None:
    # DATABASE_URL is set, so the entrypoint replicates the CLI's Prisma
    # sequence; `/key/info` for a key that exists proves the schema is there.
    # The line is read as a JSON record, not as a substring: this config sets
    # `json_logs: true` and Vector parses this stdout, so a boot line that is
    # plain text there is a defect of its own.
    messages = [
        record.get("message")
        for record in _json_log_records(mode_a.stack.gateway_logs())
        if isinstance(record, dict)
    ]

    assert "prisma schema setup complete" in messages

    answered = httpx.get(
        f"{mode_a.stack.base_url}/key/info",
        params={"key": mode_a.key},
        headers={"Authorization": f"Bearer {MASTER_KEY}"},
        timeout=60,
    )
    assert answered.status_code == 200, answered.text


_ACCESS_LINE = re.compile(r'"(GET|POST|HEAD) \S+ HTTP/1\.1" \d{3}')


def test_uvicorns_own_lines_are_json_records_too(mode: Mode) -> None:
    # Vector parses this container's stdout record by record, so uvicorn's
    # access and error lines have to be JSON as well — that is what `serve.py`
    # passes litellm's JSON `log_config` for. A plain access line would be an
    # unparseable record between parseable ones.
    probe = httpx.get(f"{mode.stack.base_url}/healthz/live", timeout=60)
    assert probe.status_code == 200

    logs = mode.stack.gateway_logs()
    records = [record for record in _json_log_records(logs) if isinstance(record, dict)]

    access = [r for r in records if _ACCESS_LINE.search(str(r.get("message", "")))]
    assert access, "no uvicorn access line was written as a JSON record"
    plain = [
        line
        for line in logs.splitlines()
        if _ACCESS_LINE.search(line) and not line.strip().startswith("{")
    ]
    assert not plain, plain[:3]

    assert [r for r in records if "Uvicorn running on" in str(r.get("message", ""))], (
        "uvicorn's own startup line is not a JSON record"
    )


# ── the bypass routes: refused, in both modes ────────────────────────────────

BYPASS_ROUTES: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "/v1/messages/count_tokens",
        {
            "model": ANTHROPIC_MODEL,
            "system": f"Escalate to {CANARY}.",
            "messages": [{"role": "user", "content": f"write to {CANARY}"}],
            "tools": [{"name": "page", "description": f"pages {CANARY}", "input_schema": {}}],
        },
    ),
    (
        "/v1/responses/input_tokens",
        {"model": OPENAI_MODEL, "input": f"write to {CANARY}"},
    ),
    (
        "/utils/token_counter?call_endpoint=true",
        {
            "model": ANTHROPIC_MODEL,
            "messages": [{"role": "user", "content": f"write to {CANARY}"}],
            "prompt": f"write to {CANARY}",
        },
    ),
    (
        "/queue/chat/completions",
        {"model": OPENAI_MODEL, "messages": [{"role": "user", "content": f"write to {CANARY}"}]},
    ),
)


@pytest.mark.parametrize("path,payload", BYPASS_ROUTES, ids=[row[0] for row in BYPASS_ROUTES])
def test_the_bypass_routes_are_refused_and_reach_no_provider(
    mode: Mode, path: str, payload: dict[str, Any]
) -> None:
    mode.stack.mark()
    before = mode.stack.block_count("route_gate_listed")

    response = mode.post(path, payload)

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == "E_ROUTE_BLOCKED"
    assert body["error"]["reason"] == "route_gate_listed"
    assert CANARY not in response.text
    assert mode.stack.new_captures() == []
    assert mode.stack.block_count("route_gate_listed") > before


def test_a_refusal_is_audited_without_a_never_field(mode_a: Mode) -> None:
    mode_a.post("/v1/messages/count_tokens", {"model": ANTHROPIC_MODEL, "messages": []})

    blocked = [
        record
        for record in mode_a.stack.audit_records()
        if record.get("block_reason") == "route_gate_listed"
    ]

    assert blocked, "the gate emitted no audit record for a refused route"
    record = blocked[-1]
    assert record["error_code"] == "E_ROUTE_BLOCKED"
    assert record["status"] == "failed"
    from corp_llm_gateway.audit.invariants import assert_no_never_fields

    assert_no_never_fields(record)


WEBSOCKET_PATHS = ("/v1/responses", "/v1/realtime", "/openai/v1/realtime")


@pytest.mark.parametrize("path", WEBSOCKET_PATHS)
def test_a_websocket_handshake_is_refused(mode: Mode, path: str) -> None:
    # Every `response.create` frame after a successful handshake would never
    # reach the hook, so the handshake itself is where this has to end.
    # A raw socket, not httpx: httpx rewrites `Connection`, so uvicorn would
    # never build a websocket scope and the request would arrive as plain HTTP.
    mode.stack.mark()
    key = base64.b64encode(uuid.uuid4().bytes).decode()

    status, body = _raw_get(
        mode,
        path,
        extra_headers=(
            ("Connection", "Upgrade"),
            ("Upgrade", "websocket"),
            ("Sec-WebSocket-Version", "13"),
            ("Sec-WebSocket-Key", key),
        ),
    )

    assert status == 403, body
    assert json.loads(body)["error"]["reason"] == "route_gate_websocket"
    assert mode.stack.new_captures() == []


REFUSED_POSTS: tuple[tuple[str, dict[str, Any], int, str], ...] = (
    (
        "/v1/completions",
        {"model": OPENAI_MODEL, "prompt": f"write to {CANARY}"},
        403,
        "route_gate_listed",
    ),
    (
        "/v1/embeddings",
        {"model": OPENAI_MODEL, "input": f"write to {CANARY}"},
        403,
        "route_gate_listed",
    ),
    (
        "/v1/moderations",
        {"model": OPENAI_MODEL, "input": f"write to {CANARY}"},
        403,
        "route_gate_listed",
    ),
    ("/v1/ingest", {"text": f"write to {CANARY}"}, 404, "route_gate_unlisted"),
    (
        "/anthropic/v1/messages",
        {"model": ANTHROPIC_MODEL, "messages": [{"role": "user", "content": CANARY}]},
        403,
        "route_gate_listed",
    ),
    ("/v1/some/future/route", {"text": CANARY}, 404, "route_gate_unlisted"),
    ("/model/new_thing", {"text": CANARY}, 404, "route_gate_unlisted"),
)


@pytest.mark.parametrize(
    "path,payload,status,reason", REFUSED_POSTS, ids=[row[0] for row in REFUSED_POSTS]
)
def test_the_no_rewrite_and_unlisted_routes_are_refused(
    mode: Mode, path: str, payload: dict[str, Any], status: int, reason: str
) -> None:
    mode.stack.mark()

    response = mode.post(path, payload)

    assert response.status_code == status, response.text
    assert response.json()["error"]["reason"] == reason
    assert CANARY not in response.text
    assert mode.stack.new_captures() == []


MALFORMED_PATHS = (
    "/v1/messages/..%2f..%2fkey/generate",
    "//v1//messages",
    "/v1/../key/generate",
)


@pytest.mark.parametrize("path", MALFORMED_PATHS)
def test_a_malformed_path_is_refused_with_its_own_reason(mode_a: Mode, path: str) -> None:
    # Raw again: httpx normalises `..` out of the path before it leaves, so the
    # request line uvicorn parses has to be written by hand — that is also what
    # an attacker sends.
    mode_a.stack.mark()

    status, body = _raw_get(mode_a, path)

    assert status == 403, body
    assert json.loads(body)["error"]["reason"] == "route_gate_malformed"
    assert mode_a.stack.new_captures() == []


# ── the routes that must keep working ────────────────────────────────────────


def test_mode_a_rewrites_every_generation_route(mode_a: Mode) -> None:
    calls = (
        (
            "/v1/messages",
            {
                "model": ANTHROPIC_MODEL,
                "max_tokens": 64,
                "messages": [{"role": "user", "content": f"escalate to {CANARY}"}],
            },
        ),
        (
            "/v1/chat/completions",
            {
                "model": OPENAI_MODEL,
                "messages": [{"role": "user", "content": f"escalate to {CANARY}"}],
            },
        ),
        ("/v1/responses", {"model": OPENAI_MODEL, "input": f"escalate to {CANARY}"}),
    )
    for path, payload in calls:
        mode_a.stack.mark()

        response = mode_a.post(path, payload)

        assert response.status_code == 200, f"{path}: {response.text}"
        captured = mode_a.stack.new_captures()
        assert len(captured) == 1, f"{path}: {captured}"
        sent = json.dumps(captured[0]["body"])
        assert CANARY not in sent, f"{path} put the original on the wire"
        assert "EMAIL" in sent, f"{path} sent no placeholder: {sent[:400]}"


def test_mode_b_rewrites_messages_with_the_developers_own_token(mode_b: Mode) -> None:
    mode_b.stack.mark()

    response = mode_b.post(
        "/v1/messages",
        {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": f"escalate to {CANARY}"}],
        },
    )

    assert response.status_code == 200, response.text
    captured = mode_b.stack.new_captures()
    assert len(captured) == 1, captured
    assert CANARY not in json.dumps(captured[0]["body"])
    assert "EMAIL" in json.dumps(captured[0]["body"])
    # The bridge is what makes this Mode B: the developer's bearer is the
    # upstream credential, and the corp identity header never egresses.
    headers = captured[0]["headers"]
    assert OAUTH_TOKEN in json.dumps(headers)
    assert "x-corp-auth" not in headers


def test_streaming_is_not_buffered_by_the_gate(mode_a: Mode) -> None:
    # The stub waits between SSE events; a middleware that buffered the body
    # would deliver them all at once at the end.
    arrivals: list[float] = []
    started = time.monotonic()
    with (
        httpx.Client(timeout=120) as client,
        client.stream(
            "POST",
            f"{mode_a.stack.base_url}/v1/messages",
            headers=mode_a.headers(),
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": f"escalate to {CANARY}"}],
            },
        ) as response,
    ):
        assert response.status_code == 200
        for chunk in response.iter_raw():
            if chunk.strip():
                arrivals.append(time.monotonic() - started)

    assert len(arrivals) >= 2, arrivals
    # Stub delay is 0.5s per event; anything buffered arrives inside one tick.
    assert arrivals[-1] - arrivals[0] > 0.4, arrivals


PASSTHROUGH_GETS = (
    "/health/liveliness",
    "/v1/models",
    "/healthz/live",
    "/healthz/ready",
)


@pytest.mark.parametrize("path", PASSTHROUGH_GETS)
def test_the_passthrough_routes_answer(mode_a: Mode, path: str) -> None:
    assert mode_a.get(path).status_code == 200


def test_head_follows_get_on_a_gateway_owned_route(mode_a: Mode) -> None:
    response = httpx.head(
        f"{mode_a.stack.base_url}/healthz/live", headers=mode_a.headers(), timeout=60
    )

    assert response.status_code == 200
    assert response.content == b""


def test_head_reaches_litellm_on_a_passthrough_route(mode_a: Mode) -> None:
    # The gate's rule is that HEAD inherits its path's GET verdict. litellm then
    # answers 405, because FastAPI's APIRoute — unlike a plain Starlette Route —
    # does NOT add HEAD to a GET route. That is litellm's answer, not a refusal.
    response = httpx.head(
        f"{mode_a.stack.base_url}/health/liveliness", headers=mode_a.headers(), timeout=60
    )

    assert response.status_code == 405
    assert "E_ROUTE_BLOCKED" not in response.text


def test_metrics_is_scrapable_without_a_litellm_credential(mode_a: Mode) -> None:
    # litellm's PrometheusAuthMiddleware 401s any path containing /metrics when a
    # master key is set, and it wraps the router — so the gateway-owned route has
    # to sit above it, or Helm's ServiceMonitor could never scrape.
    response = httpx.get(f"{mode_a.stack.base_url}/metrics", timeout=60)

    assert response.status_code == 200, response.text
    assert "corp_llm_gateway_blocked_requests_total" in response.text


def test_the_litellm_admin_surface_is_untouched(mode_a: Mode) -> None:
    response = httpx.get(
        f"{mode_a.stack.base_url}/key/info",
        params={"key": mode_a.key},
        headers={"Authorization": f"Bearer {MASTER_KEY}"},
        timeout=60,
    )

    assert response.status_code == 200
    assert "E_ROUTE_BLOCKED" not in response.text


def test_nothing_reached_a_provider_the_gate_refused(mode_a: Mode, mode_b: Mode) -> None:
    # The whole point of the egress-blocked network: the stub is the only thing
    # reachable, so this is the complete list of what left the gateway.
    for stack in (mode_a.stack, mode_b.stack):
        for capture in stack.stub_captures():
            assert CANARY not in json.dumps(capture), capture["path"]
            assert "/count_tokens" not in capture["path"]
            assert "/input_tokens" not in capture["path"]


# ── the Task 3 note: background responses ────────────────────────────────────


def test_background_responses_are_sanitized_but_their_round_trip_is_unsupported(
    mode_a: Mode,
) -> None:
    """``background: true`` re-runs under a different request id.

    What this pins: the gate admits the route, the request that leaves is
    sanitized, and neither the create nor the poll returns an original. What it
    does NOT pin is desanitization of the polled result — Cache B is keyed per
    conversation and ``conversation_id == request_id``, so a result fetched under
    a new id has no mapping to undo. Background responses are therefore recorded
    as unsupported (the Task 3 note's second option), not as a leak.
    """
    mode_a.stack.mark()

    created = mode_a.post(
        "/v1/responses",
        {"model": OPENAI_MODEL, "input": f"escalate to {CANARY}", "background": True},
    )

    assert created.status_code in (200, 400, 500), created.text
    assert "E_ROUTE_BLOCKED" not in created.text
    for capture in mode_a.stack.new_captures():
        assert CANARY not in json.dumps(capture["body"])

    if created.status_code != 200:
        # skip_or_fail, not pytest.skip: on a machine where this harness must
        # run, a silently skipped half of the round trip is not a passed gate.
        skip_or_fail(f"litellm refused the background request itself: {created.text[:200]}")
    polled = mode_a.get(f"/v1/responses/{created.json()['id']}")
    assert "E_ROUTE_BLOCKED" not in polled.text
    assert CANARY not in polled.text
