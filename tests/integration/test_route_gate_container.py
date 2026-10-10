"""The route gate on the real gateway image, once per provider.

Every other route-gate test reads the table, the classifier or the middleware in
process. This one runs the image the cluster runs — ``python -m
corp_llm_gateway.serve`` inside ``Dockerfile.gateway`` — on a docker network with
**no route off it**, with a stub provider (``route_gate_stub_provider.py``) as the
only reachable upstream. A refusal that leaked would show up as a capture in the
stub's log; there is nowhere else for it to go.

Three providers, the three the compose stack runs (``compose/litellm/config.*.yaml``),
each its own gateway with no ``LITELLM_MASTER_KEY`` and no litellm database:

* **anthropic** — Claude Code subscription passthrough: one native ``anthropic/``
  route and ``CORP_LLM_FORWARD_ANTHROPIC_AUTH=1``; the developer's own bearer is
  the upstream credential.
* **codex** — ChatGPT/Codex subscription passthrough: the ``"*"`` -> ``openai/*``
  catch-all and ``CORP_LLM_FORWARD_CHATGPT_AUTH=1``.
* **corp** — the corp vLLM: ``corp-*`` -> ``hosted_vllm/``, no bridge, the route's
  own ``CORP_LLM_AUTH_TOKEN`` (empty, as compose ships it).

All carry ``X-Corp-Auth``; the gate runs before litellm's auth, so a refusal here
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
import yaml

from tests.integration.conftest import (
    ROOT,
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
# What a client sends to litellm's admin surface. No gateway sets a master key,
# so litellm itself would accept any bearer — or none — there.
ADMIN_BEARER = "sk-corp-route-gate-admin-probe"
# Obvious fakes; the `sk-ant-oat` prefix is the only part litellm reads.
OAUTH_TOKEN = "sk-ant-oat01-fake-route-gate-container"
# Shaped like a ChatGPT OAuth access token (a JWT); the codex bridge takes any bearer.
CHATGPT_TOKEN = (
    "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiJyb3V0ZS1nYXRlLWNvZGV4In0."
    "ZmFrZS1yb3V0ZS1nYXRlLWNvZGV4LXNpZw"
)
# What the corp client files send: an explicit, non-subscription bearer.
CORP_CLIENT_BEARER = "corp-route-gate-client"

ANTHROPIC_MODEL = "claude-3-5-sonnet-20241022"
OPENAI_MODEL = "gpt-4o-mini"
CORP_MODEL = "corp-glm"

STUB_BASE = "http://stub:8000"

SHIPPED_CONFIGS = ROOT / "compose" / "litellm"


def _shipped_config(provider: str, api_base: str | None = None) -> str:
    """compose/litellm/config.<provider>.yaml as shipped, every route's ``api_base``
    pointed at the stub when one is given, so the suite cannot drift from the files."""
    config = yaml.safe_load((SHIPPED_CONFIGS / f"config.{provider}.yaml").read_text())
    if api_base is not None:
        for route in config["model_list"]:
            route["litellm_params"]["api_base"] = api_base
    return yaml.safe_dump(config, sort_keys=False)


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
    # What compose/.env.example sets: the wire headers go from litellm's own header
    # copies as well. What reaches the stub does not depend on it (the guardrail's
    # data["headers"] is an empty allowlist projection either way).
    "CORP_LLM_STRIP_INBOUND_HEADERS": "1",
}


@dataclass
class Provider:
    """A running stack plus the bearer and the generation route its client uses."""

    name: str
    stack: Stack
    key: str
    path: str
    payload: dict[str, Any]
    # A model name this provider's config routes, so a request the gate let
    # through would reach the stub instead of dying in litellm's router.
    model: str

    def routed(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {**payload, "model": self.model} if "model" in payload else payload

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
    provider: Provider, path: str, *, extra_headers: tuple[tuple[str, str], ...] = ()
) -> tuple[int, str]:
    """A GET written byte by byte, so the request line reaches uvicorn verbatim."""
    url = httpx.URL(provider.stack.base_url)
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {url.host}:{url.port}",
        "Connection: close",
        f"Authorization: Bearer {provider.key}",
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


def _messages(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": f"escalate to {CANARY}"}],
    }


def _chat(model: str) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": f"escalate to {CANARY}"}]}


@pytest.fixture(scope="module")
def provider_anthropic(
    gateway_image: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Provider]:
    config = _write(
        tmp_path_factory, "route-gate-anthropic", _shipped_config("anthropic", STUB_BASE)
    )
    env = {**BASE_ENV, "CORP_LLM_FORWARD_ANTHROPIC_AUTH": "1"}
    with running_stack(gateway_image, config, env) as stack:
        yield Provider(
            "anthropic",
            stack,
            OAUTH_TOKEN,
            "/v1/messages",
            _messages(ANTHROPIC_MODEL),
            ANTHROPIC_MODEL,
        )


@pytest.fixture(scope="module")
def provider_codex(
    gateway_image: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Provider]:
    config = _write(
        tmp_path_factory, "route-gate-codex", _shipped_config("codex", f"{STUB_BASE}/v1")
    )
    env = {**BASE_ENV, "CORP_LLM_FORWARD_CHATGPT_AUTH": "1"}
    with running_stack(gateway_image, config, env) as stack:
        yield Provider(
            "codex",
            stack,
            CHATGPT_TOKEN,
            "/v1/responses",
            {"model": OPENAI_MODEL, "input": f"escalate to {CANARY}"},
            OPENAI_MODEL,
        )


# The shipped corp config reads its api_base and api_key from the environment the
# compose service sets, so the stub goes in through that environment, not the file.
CORP_ENV = {
    **BASE_ENV,
    "CORP_LLM_ENDPOINT": f"{STUB_BASE}/v1",
    # What compose passes when .env leaves it blank: `${CORP_LLM_AUTH_TOKEN:-}`.
    "CORP_LLM_AUTH_TOKEN": "",
}
# A corp vLLM that does want a credential: the route's own, never the client's.
CORP_UPSTREAM_TOKEN = "corp-route-gate-upstream-token"


def _corp_stack(
    gateway_image: str, tmp_path_factory: pytest.TempPathFactory, env: dict[str, str]
) -> Iterator[Provider]:
    config = _write(tmp_path_factory, "route-gate-corp", _shipped_config("corp"))
    with running_stack(gateway_image, config, env) as stack:
        yield Provider(
            "corp",
            stack,
            CORP_CLIENT_BEARER,
            "/v1/chat/completions",
            _chat(CORP_MODEL),
            CORP_MODEL,
        )


@pytest.fixture(scope="module")
def provider_corp(
    gateway_image: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Provider]:
    yield from _corp_stack(gateway_image, tmp_path_factory, CORP_ENV)


@pytest.fixture(scope="module")
def provider_corp_with_token(
    gateway_image: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Provider]:
    env = {**CORP_ENV, "CORP_LLM_AUTH_TOKEN": CORP_UPSTREAM_TOKEN}
    yield from _corp_stack(gateway_image, tmp_path_factory, env)


PROVIDERS = ("anthropic", "codex", "corp")


@pytest.fixture(params=PROVIDERS)
def provider(request: pytest.FixtureRequest) -> Provider:
    return request.getfixturevalue(f"provider_{request.param}")


# ── the harness's own premise: the gateway has no route off the network ──────

_CONNECT_PROBE = "import socket; socket.create_connection(('1.1.1.1', 443), timeout=3)"
_DNS_PROBE = "import socket; socket.getaddrinfo('api.anthropic.com', 443)"


# What a blocked socket looks like from inside the container. A `docker exec`
# that cannot run at all also exits non-zero (126/127), so the failure has to be
# provably the network's, not the harness's.
_NETWORK_ERRORS = ("OSError", "gaierror", "timed out", "Network is unreachable")


@pytest.mark.parametrize("probe", [_CONNECT_PROBE, _DNS_PROBE], ids=["connect", "dns"])
def test_the_gateway_container_cannot_reach_the_internet(provider: Provider, probe: str) -> None:
    # Everything else in this file reads "the stub saw nothing" as "nothing
    # leaked". That only holds while the gateway's only network is `--internal`:
    # if docker ever attached a default bridge as well, a refusal that leaked
    # would reach the real provider and every assertion here would still pass.
    control = docker(
        "exec", provider.stack.gateway, "/app/.venv/bin/python", "-c", "print(1)", timeout=60
    )
    assert control.returncode == 0, (
        f"`docker exec` itself cannot run in this container, so the probe below "
        f"proves nothing\n{control.stdout}\n{control.stderr}"
    )
    assert control.stdout.strip() == "1"

    result = docker(
        "exec", provider.stack.gateway, "/app/.venv/bin/python", "-c", probe, timeout=60
    )

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


_ACCESS_LINE = re.compile(r'"(GET|POST|HEAD) \S+ HTTP/1\.1" \d{3}')


def test_uvicorns_own_lines_are_json_records_too(provider: Provider) -> None:
    # Vector parses this container's stdout record by record, so uvicorn's
    # access and error lines have to be JSON as well — that is what `serve.py`
    # passes litellm's JSON `log_config` for. A plain access line would be an
    # unparseable record between parseable ones.
    probe = httpx.get(f"{provider.stack.base_url}/healthz/live", timeout=60)
    assert probe.status_code == 200

    logs = provider.stack.gateway_logs()
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


# ── who may use the gateway at all ───────────────────────────────────────────


def test_an_unknown_corp_token_is_refused_and_reaches_no_provider(provider: Provider) -> None:
    # No provider has litellm key auth any more: X-Corp-Auth is the only thing
    # that admits a request, on every provider's own generation route.
    unknown = "route-gate-unknown-team-token"
    provider.stack.mark()

    response = httpx.post(
        f"{provider.stack.base_url}{provider.path}",
        json=provider.payload,
        headers=provider.headers(**{"X-Corp-Auth": unknown}),
        timeout=90,
    )

    assert response.status_code == 401, response.text
    assert "E_TOKEN_INVALID" in response.text
    assert unknown not in response.text
    assert CANARY not in response.text
    assert provider.stack.new_captures() == []


# ── the bypass routes: refused, on every provider ────────────────────────────

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
    provider: Provider, path: str, payload: dict[str, Any]
) -> None:
    provider.stack.mark()
    before = provider.stack.block_count("route_gate_listed")

    response = provider.post(path, provider.routed(payload))

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == "E_ROUTE_BLOCKED"
    assert body["error"]["reason"] == "route_gate_listed"
    assert CANARY not in response.text
    assert provider.stack.new_captures() == []
    assert provider.stack.block_count("route_gate_listed") > before


def test_a_refusal_is_audited_without_a_never_field(provider_anthropic: Provider) -> None:
    provider_anthropic.post("/v1/messages/count_tokens", {"model": ANTHROPIC_MODEL, "messages": []})

    blocked = [
        record
        for record in provider_anthropic.stack.audit_records()
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
def test_a_websocket_handshake_is_refused(provider: Provider, path: str) -> None:
    # Every `response.create` frame after a successful handshake would never
    # reach the hook, so the handshake itself is where this has to end.
    # A raw socket, not httpx: httpx rewrites `Connection`, so uvicorn would
    # never build a websocket scope and the request would arrive as plain HTTP.
    provider.stack.mark()
    key = base64.b64encode(uuid.uuid4().bytes).decode()

    status, body = _raw_get(
        provider,
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
    assert provider.stack.new_captures() == []


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
    provider: Provider, path: str, payload: dict[str, Any], status: int, reason: str
) -> None:
    provider.stack.mark()

    response = provider.post(path, provider.routed(payload))

    assert response.status_code == status, response.text
    assert response.json()["error"]["reason"] == reason
    assert CANARY not in response.text
    assert provider.stack.new_captures() == []


MALFORMED_PATHS = (
    "/v1/messages/..%2f..%2fkey/generate",
    "//v1//messages",
    "/v1/../key/generate",
)


@pytest.mark.parametrize("path", MALFORMED_PATHS)
def test_a_malformed_path_is_refused_with_its_own_reason(
    provider_anthropic: Provider, path: str
) -> None:
    # Raw again: httpx normalises `..` out of the path before it leaves, so the
    # request line uvicorn parses has to be written by hand — that is also what
    # an attacker sends.
    provider_anthropic.stack.mark()

    status, body = _raw_get(provider_anthropic, path)

    assert status == 403, body
    assert json.loads(body)["error"]["reason"] == "route_gate_malformed"
    assert provider_anthropic.stack.new_captures() == []


# ── the routes that must keep working ────────────────────────────────────────


# The stub path each provider's upstream call lands on, per client route.
UPSTREAM_PATHS = {
    ("anthropic", "/v1/messages"): "/v1/messages",
    ("anthropic", "/v1/chat/completions"): "/v1/messages",
    ("codex", "/v1/responses"): "/v1/responses",
    ("codex", "/v1/chat/completions"): "/v1/chat/completions",
    ("corp", "/v1/chat/completions"): "/v1/chat/completions",
    ("corp", "/v1/messages"): "/v1/chat/completions",
}


def test_every_generation_route_is_rewritten(
    provider_anthropic: Provider, provider_codex: Provider, provider_corp: Provider
) -> None:
    # Each provider's own client routes: Claude Code's messages (and the chat
    # adapter) on anthropic, Codex's responses and chat completions (the
    # `openai/` chat upstream) on codex, and both shapes on corp.
    calls = (
        (provider_anthropic, "/v1/messages", _messages(ANTHROPIC_MODEL)),
        (provider_anthropic, "/v1/chat/completions", _chat(ANTHROPIC_MODEL)),
        (
            provider_codex,
            "/v1/responses",
            {"model": OPENAI_MODEL, "input": f"escalate to {CANARY}"},
        ),
        (provider_codex, "/v1/chat/completions", _chat(OPENAI_MODEL)),
        (provider_corp, "/v1/chat/completions", _chat(CORP_MODEL)),
        (provider_corp, "/v1/messages", _messages(CORP_MODEL)),
    )
    for provider, path, payload in calls:
        where = f"{provider.name} {path}"
        provider.stack.mark()

        response = provider.post(path, payload)

        assert response.status_code == 200, f"{where}: {response.text}"
        captured = provider.stack.new_captures()
        assert len(captured) == 1, f"{where}: {captured}"
        sent = json.dumps(captured[0]["body"])
        assert CANARY not in sent, f"{where} put the original on the wire"
        assert "EMAIL" in sent, f"{where} sent no placeholder: {sent[:400]}"
        upstream_path = captured[0]["path"]
        assert upstream_path.endswith(UPSTREAM_PATHS[(provider.name, path)]), (
            f"{where} reached {upstream_path}"
        )


def test_anthropic_rewrites_messages_with_the_developers_own_token(
    provider_anthropic: Provider,
) -> None:
    provider_anthropic.stack.mark()

    response = provider_anthropic.post("/v1/messages", _messages(ANTHROPIC_MODEL))

    assert response.status_code == 200, response.text
    captured = provider_anthropic.stack.new_captures()
    assert len(captured) == 1, captured
    assert CANARY not in json.dumps(captured[0]["body"])
    assert "EMAIL" in json.dumps(captured[0]["body"])
    # The bridge: the developer's bearer is the upstream credential, and the
    # corp identity header never egresses.
    headers = captured[0]["headers"]
    assert OAUTH_TOKEN in json.dumps(headers)
    assert "x-corp-auth" not in headers


# A subscription bearer sent to the corp gateway by a misconfigured client. The
# corp provider has no bridge, so nothing may lift it into the upstream call.
CORP_PROBE_BEARERS = {
    "anthropic-oauth": "sk-ant-oat01-fake-corp-bearer-probe",
    "chatgpt-jwt": (
        "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiJjb3JwLWJlYXJlci1wcm9iZSJ9."
        "ZmFrZS1jb3JwLWJlYXJlci1wcm9iZS1zaWc"
    ),
}
CORP_PROBE_LITELLM_KEY = "sk-corp-route-gate-litellm-key-probe"
CORP_PROBE_X_API_KEY = "sk-corp-route-gate-x-api-key-probe"
# The credential headers a client may send next to its bearer. `x-litellm-api-key`
# moves litellm's idea of which header authenticated the caller off
# `Authorization`, which is what makes its header cleaner keep an Anthropic OAuth
# bearer; `x-api-key` is the other provider-key spelling litellm knows.
CORP_CREDENTIAL_HEADERS: dict[str, dict[str, str]] = {
    "bearer-only": {},
    "with-x-litellm-api-key": {"x-litellm-api-key": CORP_PROBE_LITELLM_KEY},
    "with-x-api-key": {"x-api-key": CORP_PROBE_X_API_KEY},
}
CORP_ROUTES = (("/v1/chat/completions", _chat(CORP_MODEL)), ("/v1/messages", _messages(CORP_MODEL)))
# What hosted_vllm sends when the route's api_key resolves to an empty string.
LITELLM_EMPTY_KEY_BEARER = "Bearer fake-api-key"


def _corp_upstream_authorization(
    provider_corp: Provider, path: str, payload: dict[str, Any], bearer: str, credential: str
) -> list[str]:
    """Send one corp request; every Authorization value the stub got."""
    headers = provider_corp.headers(
        Authorization=f"Bearer {bearer}", **CORP_CREDENTIAL_HEADERS[credential]
    )
    provider_corp.stack.mark()

    response = httpx.post(
        f"{provider_corp.stack.base_url}{path}", json=payload, headers=headers, timeout=90
    )

    assert response.status_code == 200, response.text
    captured = provider_corp.stack.new_captures()
    assert len(captured) == 1, captured
    capture = captured[0]
    wire = json.dumps(capture)
    assert bearer not in wire
    assert CORP_PROBE_LITELLM_KEY not in wire
    assert CORP_PROBE_X_API_KEY not in wire
    assert "REDACTED" not in wire, "litellm's logging copy of a header went upstream"
    assert "x-corp-auth" not in capture["headers"]
    assert "x-api-key" not in capture["headers"]
    return [value for name, value in capture["raw_headers"] if name.lower() == "authorization"]


@pytest.mark.parametrize(
    "credential", list(CORP_CREDENTIAL_HEADERS), ids=list(CORP_CREDENTIAL_HEADERS)
)
@pytest.mark.parametrize("bearer", list(CORP_PROBE_BEARERS), ids=list(CORP_PROBE_BEARERS))
@pytest.mark.parametrize("path,payload", CORP_ROUTES, ids=[row[0] for row in CORP_ROUTES])
def test_a_subscription_bearer_never_reaches_the_corp_upstream(
    provider_corp: Provider,
    path: str,
    payload: dict[str, Any],
    bearer: str,
    credential: str,
) -> None:
    # The exact value, not a count of Authorization lines: the transport folds two
    # same-named headers into one comma-joined value, so a client credential riding
    # next to the route's own still arrives as a single line.
    authorization = _corp_upstream_authorization(
        provider_corp, path, payload, CORP_PROBE_BEARERS[bearer], credential
    )

    assert authorization == [LITELLM_EMPTY_KEY_BEARER]


@pytest.mark.parametrize(
    "credential", list(CORP_CREDENTIAL_HEADERS), ids=list(CORP_CREDENTIAL_HEADERS)
)
@pytest.mark.parametrize("path,payload", CORP_ROUTES, ids=[row[0] for row in CORP_ROUTES])
def test_the_corp_upstream_gets_exactly_its_configured_token(
    provider_corp_with_token: Provider, path: str, payload: dict[str, Any], credential: str
) -> None:
    authorization = _corp_upstream_authorization(
        provider_corp_with_token,
        path,
        payload,
        CORP_PROBE_BEARERS["anthropic-oauth"],
        credential,
    )

    assert authorization == [f"Bearer {CORP_UPSTREAM_TOKEN}"]


# Client headers no upstream needs. Each value is unique, so finding it anywhere in
# what the stub received is a header that went through unsanitized.
PROBE_HEADERS = {
    "x-probe-secret": "alice.probe@corp.lan",
    "cookie": "session=route-gate-cookie-probe",
    "x-api-key": "sk-route-gate-x-api-key-probe",
    "x-litellm-api-key": "sk-route-gate-litellm-key-probe",
    "x-forwarded-for": "198.51.100.23",
}
CLIENT_USER_AGENT = "route-gate-client/1.0 (probe)"
# The client headers each provider's bridge forwards (the allowlists in
# litellm_hook.py), sent with values the upstream could not have made up.
ALLOWLISTED_CLIENT_HEADERS: dict[str, dict[str, str]] = {
    "anthropic": {
        # Not litellm's own default (2023-06-01), so arriving proves it is the client's.
        "anthropic-version": "2023-01-01",
        # A beta litellm knows: it drops the ones it does not, on 1.0.0 as well.
        "anthropic-beta": "interleaved-thinking-2025-05-14",
        "user-agent": CLIENT_USER_AGENT,
    },
    "codex": {
        "chatgpt-account-id": "route-gate-account",
        "originator": "codex_cli_rs",
        "session-id": "0199a0f2-6d1c-7c43-9a6e-2f4b8c1d5e60",
        "thread-id": "0199a0f2-6d1c-7c43-9a6e-2f4b8c1d5e61",
        "x-codex-beta-features": "route-gate-feature",
        "x-openai-internal-codex-responses-lite": "true",
        "user-agent": CLIENT_USER_AGENT,
    },
    "corp": {},
}
# Every header line that reaches the stub, per provider and client route: the
# allowlisted client headers above, the upstream credential, and what litellm and
# its HTTP transport add on their own.
TRANSPORT_HEADERS = frozenset(
    {"accept", "accept-encoding", "connection", "content-length", "content-type", "host"}
)
ANTHROPIC_UPSTREAM_HEADERS = TRANSPORT_HEADERS | {
    "authorization",
    "anthropic-beta",
    "anthropic-version",
    "user-agent",
    # litellm's own OAuth branch adds it.
    "anthropic-dangerous-direct-browser-access",
}
CODEX_UPSTREAM_HEADERS = TRANSPORT_HEADERS | {
    "authorization",
    "chatgpt-account-id",
    "originator",
    "session-id",
    "thread-id",
    "x-codex-beta-features",
    "x-openai-internal-codex-responses-lite",
    "user-agent",
}
# The OpenAI SDK's own telemetry on litellm's `openai/` chat path.
OPENAI_SDK_HEADERS = frozenset(
    {
        "x-stainless-arch",
        "x-stainless-async",
        "x-stainless-lang",
        "x-stainless-os",
        "x-stainless-package-version",
        "x-stainless-raw-response",
        "x-stainless-read-timeout",
        "x-stainless-retry-count",
        "x-stainless-runtime",
        "x-stainless-runtime-version",
    }
)
# `user-agent` here is litellm's own, never the client's (checked below).
CORP_UPSTREAM_HEADERS = TRANSPORT_HEADERS | {"authorization", "user-agent"}
EXPECTED_UPSTREAM_HEADERS: dict[tuple[str, str], frozenset[str]] = {
    ("anthropic", "/v1/messages"): ANTHROPIC_UPSTREAM_HEADERS,
    ("anthropic", "/v1/chat/completions"): ANTHROPIC_UPSTREAM_HEADERS,
    ("codex", "/v1/responses"): CODEX_UPSTREAM_HEADERS,
    ("codex", "/v1/chat/completions"): CODEX_UPSTREAM_HEADERS | OPENAI_SDK_HEADERS,
    ("corp", "/v1/chat/completions"): CORP_UPSTREAM_HEADERS,
    ("corp", "/v1/messages"): CORP_UPSTREAM_HEADERS,
}
HEADER_CASES = (
    ("anthropic", "/v1/messages"),
    ("anthropic", "/v1/chat/completions"),
    ("codex", "/v1/responses"),
    ("codex", "/v1/chat/completions"),
    ("corp", "/v1/chat/completions"),
    ("corp", "/v1/messages"),
)
# Allowlisted client headers litellm itself overwrites on a route: on the chat
# adapter it always sends its own anthropic-version.
LITELLM_REPLACES: dict[tuple[str, str], dict[str, str]] = {
    ("anthropic", "/v1/chat/completions"): {"anthropic-version": "2023-06-01"},
}
GENERATION_PAYLOADS = {
    "/v1/messages": _messages,
    "/v1/chat/completions": _chat,
    "/v1/responses": lambda model: {"model": model, "input": f"escalate to {CANARY}"},
}


@pytest.mark.parametrize(
    "provider_name,path", HEADER_CASES, ids=[f"{name}-{path}" for name, path in HEADER_CASES]
)
def test_only_allowlisted_client_headers_reach_the_upstream(
    request: pytest.FixtureRequest, provider_name: str, path: str
) -> None:
    provider: Provider = request.getfixturevalue(f"provider_{provider_name}")
    allowlisted = ALLOWLISTED_CLIENT_HEADERS[provider_name]
    sent = {**provider.headers(), **PROBE_HEADERS, "user-agent": CLIENT_USER_AGENT, **allowlisted}
    provider.stack.mark()

    response = httpx.post(
        f"{provider.stack.base_url}{path}",
        json=GENERATION_PAYLOADS[path](provider.model),
        headers=sent,
        timeout=90,
    )

    assert response.status_code == 200, response.text
    captured = provider.stack.new_captures()
    assert len(captured) == 1, captured
    raw = captured[0]["raw_headers"]
    received = {name.lower(): value for name, value in raw}
    names = sorted(name.lower() for name, _ in raw)
    assert len(names) == len(set(names)), f"a header arrived twice: {names}"
    assert frozenset(names) == EXPECTED_UPSTREAM_HEADERS[(provider_name, path)], " ".join(names)

    replaced = LITELLM_REPLACES.get((provider_name, path), {})
    for name, value in allowlisted.items():
        expected = replaced.get(name, value)
        assert expected in received.get(name, ""), f"allowlisted {name} did not arrive: {received}"
    # Every other client header value: nowhere on the wire, body included.
    wire = json.dumps(captured[0])
    forwarded = {name.lower() for name in allowlisted}
    for name, value in sent.items():
        if name.lower() in forwarded or name.lower() in ("authorization", "content-type"):
            continue
        assert value not in wire, f"client header {name} reached the upstream"
    assert "REDACTED" not in wire, "litellm's logging copy of a header went upstream"
    assert CORP_TOKEN not in wire

    if provider_name == "corp":
        assert received["authorization"] == LITELLM_EMPTY_KEY_BEARER
        assert received["user-agent"].startswith("litellm/"), received["user-agent"]
    else:
        assert received["authorization"] == f"Bearer {provider.key}"


def test_streaming_is_not_buffered_by_the_gate(provider_anthropic: Provider) -> None:
    # The stub waits between SSE events; a middleware that buffered the body
    # would deliver them all at once at the end.
    arrivals: list[float] = []
    started = time.monotonic()
    with (
        httpx.Client(timeout=120) as client,
        client.stream(
            "POST",
            f"{provider_anthropic.stack.base_url}/v1/messages",
            headers=provider_anthropic.headers(),
            json={**_messages(ANTHROPIC_MODEL), "stream": True},
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
    "/health/readiness",
    "/v1/models",
    "/healthz/live",
    "/healthz/ready",
)


@pytest.mark.parametrize("path", PASSTHROUGH_GETS)
def test_the_passthrough_routes_answer(provider_anthropic: Provider, path: str) -> None:
    assert provider_anthropic.get(path).status_code == 200


def test_head_follows_get_on_a_gateway_owned_route(provider_anthropic: Provider) -> None:
    response = httpx.head(
        f"{provider_anthropic.stack.base_url}/healthz/live",
        headers=provider_anthropic.headers(),
        timeout=60,
    )

    assert response.status_code == 200
    assert response.content == b""


def test_head_reaches_litellm_on_a_passthrough_route(provider_anthropic: Provider) -> None:
    # The gate's rule is that HEAD inherits its path's GET verdict. litellm then
    # answers 405, because FastAPI's APIRoute — unlike a plain Starlette Route —
    # does NOT add HEAD to a GET route. That is litellm's answer, not a refusal.
    response = httpx.head(
        f"{provider_anthropic.stack.base_url}/health/liveliness",
        headers=provider_anthropic.headers(),
        timeout=60,
    )

    assert response.status_code == 405
    assert "E_ROUTE_BLOCKED" not in response.text


def test_metrics_is_scrapable_without_a_litellm_credential(provider_anthropic: Provider) -> None:
    # /metrics is gateway-owned and answered above litellm's app, so Helm's
    # ServiceMonitor scrapes it with no credential at all. litellm's own
    # exporter (its `litellm_*` series, behind PrometheusAuthMiddleware) must
    # never be what answers here.
    response = httpx.get(f"{provider_anthropic.stack.base_url}/metrics", timeout=60)

    assert response.status_code == 200, response.text
    assert "corp_llm_gateway_blocked_requests_total" in response.text
    series = [line for line in response.text.splitlines() if line and not line.startswith("#")]
    assert series
    assert not [line for line in series if line.startswith("litellm_")], series[:5]


ADMIN_SURFACE: tuple[tuple[str, str], ...] = (
    ("GET", "/key/info"),
    ("GET", "/key/list"),
    ("POST", "/key/generate"),
    ("POST", "/policies"),
    ("PUT", "/guardrails/g1"),
    ("POST", "/login"),
    ("GET", "/sso/key/generate"),
    ("GET", "/"),
    ("GET", "/health"),
    ("GET", "/health/drain"),
)


@pytest.mark.parametrize("method,path", ADMIN_SURFACE, ids=[f"{m} {p}" for m, p in ADMIN_SURFACE])
def test_the_litellm_admin_surface_is_refused(provider: Provider, method: str, path: str) -> None:
    # With no master key litellm accepts an empty identity on its management
    # routes, so the gate is the only thing between any bearer and them.
    provider.stack.mark()

    response = httpx.request(
        method,
        f"{provider.stack.base_url}{path}",
        json={"models": ["claude-*"], "text": CANARY} if method != "GET" else None,
        headers={
            "Authorization": f"Bearer {ADMIN_BEARER}",
            "X-Corp-Auth": CORP_TOKEN,
            "content-type": "application/json",
        },
        timeout=60,
    )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "E_ROUTE_BLOCKED"
    assert response.json()["error"]["reason"] == "route_gate_listed"
    assert CANARY not in response.text
    assert provider.stack.new_captures() == []


def test_nothing_reached_a_provider_the_gate_refused(
    provider_anthropic: Provider, provider_codex: Provider, provider_corp: Provider
) -> None:
    # The whole point of the egress-blocked network: the stub is the only thing
    # reachable, so this is the complete list of what left each gateway. One
    # admitted request each first, so the list is never empty whatever ran before:
    # an empty list would pass the loop below without reading anything.
    for provider in (provider_anthropic, provider_codex, provider_corp):
        assert provider.post(provider.path, provider.payload).status_code == 200
        captures = provider.stack.stub_captures()
        assert captures, f"{provider.name}: the stub saw nothing, so this proves nothing"
        for capture in captures:
            assert CANARY not in json.dumps(capture), (provider.name, capture["path"])
            assert "/count_tokens" not in capture["path"]
            assert "/input_tokens" not in capture["path"]


# ── the Task 3 note: background responses ────────────────────────────────────


def test_background_responses_are_sanitized_but_their_round_trip_is_unsupported(
    provider_codex: Provider,
) -> None:
    """``background: true`` re-runs under a different request id.

    What this pins: the gate admits the route, the request that leaves is
    sanitized, and neither the create nor the poll returns an original. What it
    does NOT pin is desanitization of the polled result — Cache B is keyed per
    conversation and ``conversation_id == request_id``, so a result fetched under
    a new id has no mapping to undo. Background responses are therefore recorded
    as unsupported (the Task 3 note's second option), not as a leak.
    """
    provider_codex.stack.mark()

    created = provider_codex.post(
        "/v1/responses",
        {"model": OPENAI_MODEL, "input": f"escalate to {CANARY}", "background": True},
    )

    assert created.status_code in (200, 400, 500), created.text
    assert "E_ROUTE_BLOCKED" not in created.text
    for capture in provider_codex.stack.new_captures():
        assert CANARY not in json.dumps(capture["body"])

    if created.status_code != 200:
        # skip_or_fail, not pytest.skip: on a machine where this harness must
        # run, a silently skipped half of the round trip is not a passed gate.
        skip_or_fail(f"litellm refused the background request itself: {created.text[:200]}")
    polled = provider_codex.get(f"/v1/responses/{created.json()['id']}")
    assert "E_ROUTE_BLOCKED" not in polled.text
    assert CANARY not in polled.text


BODY_SECRET = "alice.bodykey@corp.lan"
LITELLM_PROVIDER = {"anthropic": "anthropic", "codex": "openai", "corp": "hosted_vllm"}


def _control_body_value(key: str, provider_name: str) -> Any:
    if key == "extra_headers":
        return {"x-body-probe": BODY_SECRET}
    if key == "provider_specific_header":
        return {
            "custom_llm_provider": LITELLM_PROVIDER[provider_name],
            "extra_headers": {"x-psh-probe": BODY_SECRET},
        }
    return BODY_SECRET


def _upstream_authorization(provider: Provider) -> str:
    return LITELLM_EMPTY_KEY_BEARER if provider.name == "corp" else f"Bearer {provider.key}"


CONTROL_BODY_KEYS = ("extra_headers", "provider_specific_header", "api_key", "organization")


def _wire(provider: Provider, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    provider.stack.mark()
    response = provider.post(path, payload)
    assert response.status_code == 200, response.text
    (captured,) = provider.stack.new_captures()
    return captured


@pytest.mark.parametrize("key", CONTROL_BODY_KEYS)
@pytest.mark.parametrize(
    "provider_name,path", HEADER_CASES, ids=[f"{name}-{path}" for name, path in HEADER_CASES]
)
def test_a_client_control_body_key_never_reaches_the_upstream(
    request: pytest.FixtureRequest, provider_name: str, path: str, key: str
) -> None:
    # litellm turns each of these request-body keys into an upstream header or the
    # upstream credential. The gateway drops them silently: the request succeeds,
    # and the stub sees the same header lines it sees without the key. No
    # anthropic-* client header is sent: litellm rebuilds provider_specific_header
    # from those, which would hide the client's own.
    provider: Provider = request.getfixturevalue(f"provider_{provider_name}")
    baseline = _wire(provider, path, GENERATION_PAYLOADS[path](provider.model))
    payload = GENERATION_PAYLOADS[path](provider.model)
    payload[key] = _control_body_value(key, provider_name)

    keyed = _wire(provider, path, payload)

    assert BODY_SECRET not in json.dumps(keyed)
    sent = sorted(name.lower() for name, _ in keyed["raw_headers"])
    assert sent == sorted(name.lower() for name, _ in baseline["raw_headers"]), " ".join(sent)
    authorization = [v for n, v in keyed["raw_headers"] if n.lower() == "authorization"]
    assert authorization == [_upstream_authorization(provider)]
    # litellm keeps the deployment a client-side api_key builds: the next request
    # must not go out with it either.
    after = _wire(provider, path, GENERATION_PAYLOADS[path](provider.model))
    assert BODY_SECRET not in json.dumps(after)


FREE_TEXT = "alice.freetext@corp.lan"
# Bridge headers with a value format, plus a name the codex bridge no longer
# matches by prefix. Each carries an email: dropped, and the request goes on.
FREE_TEXT_HEADERS = (
    ("anthropic", "anthropic-version"),
    ("anthropic", "anthropic-beta"),
    ("anthropic", "user-agent"),
    ("codex", "user-agent"),
    ("codex", "originator"),
    ("codex", "session-id"),
    ("codex", "thread-id"),
    ("codex", "x-openai-internal-codex-responses-lite"),
    ("codex", "x-codex-anything"),
)


@pytest.mark.parametrize(
    "provider_name,header", FREE_TEXT_HEADERS, ids=[f"{p}-{h}" for p, h in FREE_TEXT_HEADERS]
)
def test_a_bridge_drops_a_free_text_header_value(
    request: pytest.FixtureRequest, provider_name: str, header: str
) -> None:
    provider: Provider = request.getfixturevalue(f"provider_{provider_name}")
    provider.stack.mark()

    response = httpx.post(
        f"{provider.stack.base_url}{provider.path}",
        json=provider.payload,
        headers=provider.headers(**{header: FREE_TEXT}),
        timeout=90,
    )

    assert response.status_code == 200, response.text
    captured = provider.stack.new_captures()
    assert len(captured) == 1, captured
    assert FREE_TEXT not in json.dumps(captured[0])
