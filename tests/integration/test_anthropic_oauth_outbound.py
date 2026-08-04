"""What the Anthropic OAuth bridge actually puts on the wire.

Every other test of this bridge asserts on the ``data`` dict ``pre_call``
returns. That proves intent, not egress. This file runs the real proxy and reads
the request that arrives at the upstream: headers, body, the router deployments
the request leaves behind, and the proxy process's own stdout/stderr.

Harness: the pinned litellm image (the ``FROM`` tag of
``docker/anthropic-oauth/Dockerfile``) is started with the shipped
``docker/anthropic-oauth/litellm-config.yaml`` mounted verbatim, the gateway
source on ``PYTHONPATH``, and ``ANTHROPIC_API_BASE`` pointed at a capturing HTTP
server in this process. Not litellm's ``Router`` in-process: ``pyproject.toml``
pins only ``litellm>=1.40,<2.0``, so an in-process run would exercise whatever
release this venv resolved instead of the release the overlay ships.

Deliberately out of this harness's reach:

* Detector quality. The base image carries no NER extras, so the sanitizer runs
  degraded here. The identity-preamble carve-out is pinned at detector level by
  ``tests/sanitizer/test_oauth_system_preamble.py`` (3.12 + NER); what the
  ``/v1/messages`` assertion below adds is that nothing *downstream* of the
  gateway — litellm's pass-through transformer and its billing-header filter —
  rewrites or drops that block on the way out.
* In-process leak surfaces (caplog, audit records, exception traces, metric
  labels). Those belong to ``tests/invariants/test_no_originals_leak.py``, which
  can see them; this harness sees only what crosses a process boundary.

The token's ONE authorized destination is the upstream ``Authorization`` header.
A leak is the token appearing anywhere else — any other header, the body, the
router's model list, or the proxy's log stream.

Every environment dependency below (docker daemon, the pinned image, container →
host reachability) skips on a laptop that lacks it. Skipping is exactly how this
suite could report success while verifying nothing, so CI sets
``CORP_REQUIRE_PROXY_CAPTURE=1``, which turns each of those skips into a
failure. ``_skip_or_fail`` is the ONLY place any of them is raised.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
import yaml

from corp_llm_gateway.sanitizer.identity_preamble import CLAUDE_CODE_IDENTITY_PREAMBLES
from corp_llm_gateway.settings import parse_flag

REQUIRE_ENV_VAR = "CORP_REQUIRE_PROXY_CAPTURE"


def capture_is_required() -> bool:
    return parse_flag(os.environ.get(REQUIRE_ENV_VAR))


def _skip_or_fail(reason: str) -> NoReturn:
    """Skip on a machine that cannot run this, fail where it must run."""
    if capture_is_required():
        pytest.fail(f"{REQUIRE_ENV_VAR} is set but this harness cannot run: {reason}")
    pytest.skip(reason)


ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE_PATH = ROOT / "docker/anthropic-oauth/Dockerfile"
CONFIG_PATH = ROOT / "docker/anthropic-oauth/litellm-config.yaml"
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"

# Seeded by _demo_guardrail's in-memory token store, which the shipped config's
# callback builds.
CORP_TOKEN = "demo-team-token"
MODEL = "claude-3-5-sonnet-20241022"

IDENTITY_PREAMBLE = "You are Claude Code, Anthropic's official CLI for Claude."
BILLING_BLOCK = "x-anthropic-billing-header: cost-center=demo"
SYSTEM_EMAIL = "bob.jones@corp.lan"
METADATA_CANARY = "metadata-user-id-canary-a41f"
USER_CANARY = "top-level-user-canary-a41f"

# Obvious fakes. The prefix is the only part litellm reads.
TOKENS = {
    "messages": "sk-ant-oat01-fake-messages-unary",
    "messages_stream": "sk-ant-oat01-fake-messages-stream",
    "chat": "sk-ant-oat01-fake-chat-unary",
    "chat_stream": "sk-ant-oat01-fake-chat-stream",
    "logs": "sk-ant-oat01-fake-log-surface",
    "control": "sk-ant-oat01-fake-bridge-off",
    "retain-a": "sk-ant-oat01-fake-retention-a",
    "retain-b": "sk-ant-oat01-fake-retention-b",
    "retain-c": "sk-ant-oat01-fake-retention-c",
}
REJECTED_KEY = "sk-ant-api03-fake-plain-api-key"

MESSAGE_RESPONSE: dict[str, Any] = {
    "id": "msg_capture_1",
    "type": "message",
    "role": "assistant",
    "model": MODEL,
    "content": [{"type": "text", "text": "mock reply"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 5, "output_tokens": 3},
}

SSE_RESPONSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_capture_1",'
    f'"type":"message","role":"assistant","model":"{MODEL}","content":[],"stop_reason":null,'
    '"stop_sequence":null,"usage":{"input_tokens":5,"output_tokens":1}}}\n\n'
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"mock reply"}}\n\n'
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
    '"stop_sequence":null},"usage":{"output_tokens":3}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def deployment_placeholder_key() -> str:
    return yaml.safe_load(CONFIG_PATH.read_text())["model_list"][0]["litellm_params"]["api_key"]


# ---- capturing upstream ----------------------------------------------------


class _CaptureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.captured: list[dict[str, Any]] = []
        super().__init__(*args, **kwargs)


class _UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        # Reachability probe only; never recorded.
        self._respond(204, b"", "text/plain")

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        body: Any = None
        with contextlib.suppress(ValueError):
            body = json.loads(raw.decode("utf-8")) if raw else None
        self.server.captured.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body,
            }
        )
        if isinstance(body, dict) and body.get("stream"):
            self._respond(200, SSE_RESPONSE.encode("utf-8"), "text/event-stream")
            return
        self._respond(200, json.dumps(MESSAGE_RESPONSE).encode("utf-8"), "application/json")

    def _respond(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


# ---- docker plumbing -------------------------------------------------------


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _docker_daemon_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _docker("version", "--format", "{{.Server.Version}}", timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pinned_base_image() -> str:
    for line in DOCKERFILE_PATH.read_text().splitlines():
        if line.startswith("FROM "):
            return line.split()[1]
    raise AssertionError(f"no FROM line in {DOCKERFILE_PATH}")


@dataclass
class _Stack:
    name: str
    base_url: str
    upstream: _CaptureServer

    def logs(self) -> str:
        result = _docker("logs", self.name)
        return result.stdout + result.stderr

    def model_info(self) -> list[dict[str, Any]]:
        response = httpx.get(f"{self.base_url}/model/info", timeout=30)
        response.raise_for_status()
        return response.json()["data"]

    def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        token: str | None,
        corp_token: str | None = CORP_TOKEN,
    ) -> httpx.Response:
        headers = {"content-type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if corp_token is not None:
            headers["X-Corp-Auth"] = corp_token
        return httpx.post(f"{self.base_url}{path}", json=payload, headers=headers, timeout=60)


def _wait_until_serving(name: str) -> str:
    ports = _docker("port", name, "4000").stdout.strip().splitlines()
    if not ports:
        pytest.fail(f"no published port for container {name}:\n{_docker('logs', name).stderr}")
    base_url = f"http://127.0.0.1:{ports[0].rsplit(':', 1)[1]}"
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        running = _docker("inspect", "-f", "{{.State.Running}}", name).stdout.strip()
        if running != "true":
            result = _docker("logs", name)
            pytest.fail(f"litellm container exited during boot:\n{result.stdout}{result.stderr}")
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(f"{base_url}/health/liveliness", timeout=3).status_code == 200:
                return base_url
        time.sleep(1)
    result = _docker("logs", name)
    pytest.fail(f"litellm container never became ready:\n{result.stdout}{result.stderr}")


def _require_upstream_reachable(name: str, upstream_port: int) -> None:
    # Container -> host connectivity is an environment property, not a product
    # one. Probing it separately keeps a sandboxed network from being reported
    # as a broken bridge.
    probe = _docker(
        "exec",
        name,
        "python3",
        "-c",
        "import urllib.request;"
        f"urllib.request.urlopen('http://host.docker.internal:{upstream_port}/probe', timeout=5)",
        timeout=60,
    )
    if probe.returncode != 0:
        _skip_or_fail(
            "container cannot reach the in-test capture server on host.docker.internal: "
            f"{probe.stderr.strip()[-300:]}"
        )


@contextlib.contextmanager
def _running_stack(image: str, upstream: _CaptureServer, *, bridge: bool) -> Iterator[_Stack]:
    name = f"corp-oauth-capture-{uuid.uuid4().hex[:10]}"
    upstream_port = upstream.server_address[1]
    run = _docker(
        "run",
        "-d",
        "--name",
        name,
        "-p",
        "127.0.0.1:0:4000",
        "--add-host",
        "host.docker.internal:host-gateway",
        "-v",
        f"{ROOT / 'src'}:/pkg/src:ro",
        "-v",
        f"{CONFIG_PATH}:/etc/litellm/config.yaml:ro",
        "-e",
        "PYTHONPATH=/pkg/src",
        "-e",
        f"ANTHROPIC_API_BASE=http://host.docker.internal:{upstream_port}",
        # The oracle is a second upstream this harness does not mock, and the
        # local-first cascade is what the captured body depends on anyway.
        "-e",
        "CORP_LLM_ORACLE_ENABLED=0",
        "-e",
        f"CORP_LLM_FORWARD_ANTHROPIC_AUTH={'1' if bridge else '0'}",
        image,
        "--config",
        "/etc/litellm/config.yaml",
        "--port",
        "4000",
        timeout=180,
    )
    if run.returncode != 0:
        pytest.fail(f"docker run failed:\n{run.stderr}")
    try:
        base_url = _wait_until_serving(name)
        _require_upstream_reachable(name, upstream_port)
        yield _Stack(name=name, base_url=base_url, upstream=upstream)
    finally:
        _docker("rm", "-f", name, timeout=120)


@pytest.fixture(scope="module")
def upstream() -> Iterator[_CaptureServer]:
    # 0.0.0.0, not loopback: the container reaches this through host.docker.internal.
    server = _CaptureServer(("0.0.0.0", 0), _UpstreamHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def image() -> str:
    if not _docker_daemon_ready():
        _skip_or_fail("docker daemon not reachable — capture needs the pinned litellm image")
    tag = pinned_base_image()
    if _docker("image", "inspect", tag, timeout=60).returncode != 0:
        pull = _docker("pull", tag, timeout=900)
        if pull.returncode != 0:
            _skip_or_fail(f"cannot obtain {tag}: {pull.stderr.strip()[-300:]}")
    return tag


@pytest.fixture(scope="module")
def bridge_stack(image: str, upstream: _CaptureServer) -> Iterator[_Stack]:
    with _running_stack(image, upstream, bridge=True) as stack:
        yield stack


@pytest.fixture(scope="module")
def bridge_off_stack(image: str, upstream: _CaptureServer) -> Iterator[_Stack]:
    with _running_stack(image, upstream, bridge=False) as stack:
        yield stack


# ---- request shapes + shared assertions ------------------------------------


def _messages_payload(*, stream: bool) -> dict[str, Any]:
    return {
        "model": MODEL,
        "max_tokens": 64,
        "stream": stream,
        "system": [
            # The one block Claude Code emits ahead of its identity block, and
            # the only thing litellm's pass-through transformer filters out of
            # `system`. Present so the identity assertion covers that filter.
            {"type": "text", "text": BILLING_BLOCK},
            {"type": "text", "text": IDENTITY_PREAMBLE},
            {"type": "text", "text": f"Escalate to {SYSTEM_EMAIL} when the deploy fails."},
        ],
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {"user_id": METADATA_CANARY},
        "user": USER_CANARY,
    }


def _chat_payload(*, stream: bool) -> dict[str, Any]:
    return {
        "model": MODEL,
        "max_tokens": 64,
        "stream": stream,
        "messages": [
            {"role": "system", "content": "Answer briefly."},
            {"role": "user", "content": f"Escalate to {SYSTEM_EMAIL} when the deploy fails."},
        ],
        "metadata": {"user_id": METADATA_CANARY},
        "user": USER_CANARY,
    }


def _capture_one(
    stack: _Stack, path: str, payload: dict[str, Any], *, token: str
) -> dict[str, Any]:
    stack.upstream.captured.clear()
    response = stack.post(path, payload, token=token)
    assert response.status_code == 200, response.text
    assert len(stack.upstream.captured) == 1, stack.upstream.captured
    if payload.get("stream"):
        assert "message_start" in response.text or "chat.completion.chunk" in response.text
    return stack.upstream.captured[0]


def _assert_oauth_headers(record: dict[str, Any], token: str) -> None:
    headers = record["headers"]

    assert headers.get("authorization") == f"Bearer {token}"
    assert "x-api-key" not in headers, "an api-key auth scheme rode along with the bearer"
    assert "oauth-2025-04-20" in (headers.get("anthropic-beta") or "")
    assert "x-corp-auth" not in headers, "corp identity header egressed (invariant 4)"
    # The upstream Authorization header is the token's one authorized
    # destination; any other header carrying it is a leak.
    assert not [
        name for name, value in headers.items() if name != "authorization" and token in value
    ]
    assert CORP_TOKEN not in json.dumps(headers)
    assert deployment_placeholder_key() not in json.dumps(headers), (
        "the config's placeholder credential reached the upstream"
    )


def _assert_body_carries_no_identity_fields(body: dict[str, Any]) -> None:
    blob = json.dumps(body, ensure_ascii=False)

    assert "metadata" not in body, "caller metadata egressed unsanitized"
    assert "user" not in body
    assert METADATA_CANARY not in blob
    assert USER_CANARY not in blob


def _assert_prompt_was_sanitized(body: dict[str, Any]) -> None:
    # Guards the byte-identical assertions below from going vacuous: if the
    # sanitizer stopped running entirely, this email would still be here.
    blob = json.dumps(body, ensure_ascii=False)
    assert SYSTEM_EMAIL not in blob
    assert re.search(r"\[EMAIL_\d+\]", blob), blob


# ---- tests -----------------------------------------------------------------


def test_identity_preamble_literal_matches_the_shipped_constant() -> None:
    assert IDENTITY_PREAMBLE in CLAUDE_CODE_IDENTITY_PREAMBLES


def test_every_skip_path_in_this_module_goes_through_the_guard() -> None:
    """One guarded fixture and one unguarded one is the same trap, one level down."""
    tree = ast.parse(Path(__file__).read_text())
    holders = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "skip"
            for call in ast.walk(node)
        )
    }
    # The other ways a test here could stop asserting without failing.
    bypasses = sorted(
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in {"skipif", "xfail", "importorskip"}
    )

    assert holders == {"_skip_or_fail"}, holders
    assert not bypasses, bypasses


def test_ci_runs_this_suite_with_the_skip_guard_armed() -> None:
    """The guard is worth only as much as its wiring.

    Every assertion in this file sits behind a fixture that can skip on an
    environment fault, and this test is one of the two that cannot — so it is the
    one that has to catch a CI job which dropped the env var and went back to
    reporting green over nothing.
    """
    job = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["test"]
    steps = [step for step in job.get("steps", []) if "pytest" in (step.get("run") or "")]

    assert steps, f"{CI_WORKFLOW.name}'s test job no longer runs pytest"
    for step in steps:
        env = {**(job.get("env") or {}), **(step.get("env") or {})}
        assert parse_flag(str(env.get(REQUIRE_ENV_VAR, ""))), (
            f"{CI_WORKFLOW.name} runs {step['run']!r} without {REQUIRE_ENV_VAR}: "
            "the outbound capture assertions can all skip and still report green"
        )


def test_messages_route_sends_the_subscription_token_and_nothing_else(bridge_stack: _Stack) -> None:
    """/v1/messages is the route Claude Code uses (route_type="anthropic_messages")."""
    token = TOKENS["messages"]

    record = _capture_one(
        bridge_stack, "/v1/messages", _messages_payload(stream=False), token=token
    )

    assert record["path"] == "/v1/messages"
    _assert_oauth_headers(record, token)
    _assert_body_carries_no_identity_fields(record["body"])
    _assert_prompt_was_sanitized(record["body"])
    system_texts = [block["text"] for block in record["body"]["system"]]
    assert IDENTITY_PREAMBLE in system_texts, system_texts
    assert BILLING_BLOCK in system_texts, "litellm dropped the billing block on this route"
    assert system_texts.index(IDENTITY_PREAMBLE) == 1


def test_messages_route_streaming_sends_the_subscription_token(bridge_stack: _Stack) -> None:
    token = TOKENS["messages_stream"]

    record = _capture_one(bridge_stack, "/v1/messages", _messages_payload(stream=True), token=token)

    assert record["body"]["stream"] is True
    _assert_oauth_headers(record, token)
    _assert_body_carries_no_identity_fields(record["body"])
    _assert_prompt_was_sanitized(record["body"])
    assert IDENTITY_PREAMBLE in [block["text"] for block in record["body"]["system"]]


def test_chat_completions_route_sends_the_subscription_token(bridge_stack: _Stack) -> None:
    """The bridge is gated by provider, not call_type, so this adapter is reachable too.

    It is the one that maps a top-level ``user`` onto ``metadata.user_id`` and
    copies ``metadata.user_id`` into the outbound body.
    """
    token = TOKENS["chat"]

    record = _capture_one(
        bridge_stack, "/v1/chat/completions", _chat_payload(stream=False), token=token
    )

    # The adapter still speaks Anthropic Messages upstream.
    assert record["path"] == "/v1/messages"
    _assert_oauth_headers(record, token)
    _assert_body_carries_no_identity_fields(record["body"])
    _assert_prompt_was_sanitized(record["body"])


def test_chat_completions_route_streaming_sends_the_subscription_token(
    bridge_stack: _Stack,
) -> None:
    token = TOKENS["chat_stream"]

    record = _capture_one(
        bridge_stack, "/v1/chat/completions", _chat_payload(stream=True), token=token
    )

    assert record["body"]["stream"] is True
    _assert_oauth_headers(record, token)
    _assert_body_carries_no_identity_fields(record["body"])
    _assert_prompt_was_sanitized(record["body"])


def test_a_rejected_bearer_never_reaches_the_upstream(bridge_stack: _Stack) -> None:
    bridge_stack.upstream.captured.clear()

    rejected = bridge_stack.post(
        "/v1/messages", _messages_payload(stream=False), token=REJECTED_KEY
    )
    missing = bridge_stack.post("/v1/messages", _messages_payload(stream=False), token=None)

    for response in (rejected, missing):
        assert response.status_code == 401, response.text
        assert "E_PROVIDER_AUTH" in response.text
        assert REJECTED_KEY not in response.text
    assert bridge_stack.upstream.captured == []


def test_each_distinct_token_retains_exactly_one_router_deployment(bridge_stack: _Stack) -> None:
    """litellm keeps a credential-bearing deployment per distinct per-request key.

    ``_handle_clientside_credential`` upserts one keyed on a hash of the dynamic
    params, and nothing evicts it for the process lifetime. Pinned as observed
    rather than as a bound, so a litellm change either way trips this.
    """
    tokens = [TOKENS["retain-a"], TOKENS["retain-b"], TOKENS["retain-c"]]
    before = _dynamic_deployment_ids(bridge_stack)

    for token in tokens:
        assert (
            bridge_stack.post(
                "/v1/messages", _messages_payload(stream=False), token=token
            ).status_code
            == 200
        )
    after_distinct = _dynamic_deployment_ids(bridge_stack)

    assert len(after_distinct - before) == len(tokens)

    assert (
        bridge_stack.post(
            "/v1/messages", _messages_payload(stream=False), token=tokens[0]
        ).status_code
        == 200
    )
    assert _dynamic_deployment_ids(bridge_stack) == after_distinct, "a reused token added one"
    # The retained deployments hold the raw key in memory; the admin surface
    # must not hand it back out.
    served = json.dumps(bridge_stack.model_info())
    assert not [token for token in tokens if token in served]


def _dynamic_deployment_ids(stack: _Stack) -> set[str]:
    # `original_model_id` is written only by the clientside-credential path, so
    # it separates the per-request deployments from the config's own routes.
    return {
        model["model_info"]["id"]
        for model in stack.model_info()
        if (model.get("model_info") or {}).get("original_model_id")
    }


def test_litellm_process_logs_never_carry_the_subscription_token(bridge_stack: _Stack) -> None:
    """litellm keeps the raw ``api_key`` in ``model_call_details`` / ``error_logs``
    and hands both to its logging callbacks. Only an out-of-process run sees the
    stream those callbacks write to."""
    token = TOKENS["logs"]
    _capture_one(bridge_stack, "/v1/messages", _messages_payload(stream=False), token=token)
    bridge_stack.post("/v1/messages", _messages_payload(stream=False), token=REJECTED_KEY)
    # Logging callbacks run after the response is returned.
    time.sleep(2)

    logs = bridge_stack.logs()

    assert logs.strip(), "no container output captured — the assertions below would be vacuous"
    assert not [name for name, value in TOKENS.items() if value in logs]
    assert REJECTED_KEY not in logs
    assert CORP_TOKEN not in logs
    assert METADATA_CANARY not in logs
    assert USER_CANARY not in logs
    assert SYSTEM_EMAIL not in logs


def test_without_the_bridge_the_configured_placeholder_egresses_instead(
    bridge_off_stack: _Stack,
) -> None:
    """The negative control: the OAuth headers above are the bridge's doing.

    With the flag off the same request carries the config's placeholder as
    ``x-api-key``, no ``Authorization`` and no oauth beta header — so a green
    run above cannot be litellm doing it on its own.
    """
    token = TOKENS["control"]
    # No metadata/user here: with the bridge off nothing scrubs them, and this
    # control is about which auth scheme litellm picks.
    payload = {
        "model": MODEL,
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hello"}],
    }

    record = _capture_one(bridge_off_stack, "/v1/messages", payload, token=token)

    headers = record["headers"]
    assert headers.get("x-api-key") == deployment_placeholder_key()
    assert "authorization" not in headers
    assert "anthropic-beta" not in headers
    assert token not in json.dumps(record)
