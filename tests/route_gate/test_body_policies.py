"""The gate refuses an admitted rewritten body that names litellm policies.

litellm pops a top-level ``policies`` key off the request body and applies those policies
to the request (``litellm_pre_call_utils.py``, hazard 14b of plan 20260926); the gate
refuses it once the in-flight limiter has read the body, before any slot is taken and
before litellm parses it. Content-free: the refusal and its log name no body byte.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.metrics import MetricsExporter
from corp_llm_gateway.route_gate import BLOCK_REASONS, ROUTE_GATE_BODY_POLICIES, RouteGateMiddleware
from corp_llm_gateway.route_gate.inflight import InflightLimiter

CANARY = "corp-policy-7f2a"


class _Metrics(MetricsExporter):
    def __init__(self) -> None:
        self.blocks: list[str] = []
        self.inflight: list[int] = []
        self.draining_bytes: list[int] = []

    def record_block(self, block_reason: str) -> None:
        self.blocks.append(block_reason)

    def set_inflight(self, count: int) -> None:
        self.inflight.append(count)

    def set_draining_bytes(self, count: int) -> None:
        self.draining_bytes.append(count)

    def record_failure(self, component: str) -> None:
        return None

    def observe_request_latency(self, seconds: float, *, status: str) -> None:
        return None


class _Downstream:
    def __init__(self) -> None:
        self.bodies: list[bytes] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        self.bodies.append(body)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"served"})


async def _post(
    body_chunks: list[bytes], *, path: str = "/v1/chat/completions"
) -> tuple[int, dict[str, Any] | bytes, _Downstream, _Metrics, ListSink, InflightLimiter]:
    downstream = _Downstream()
    metrics = _Metrics()
    sink = ListSink()
    limiter = InflightLimiter(1, metrics=metrics)
    gate = RouteGateMiddleware(
        downstream, metrics=metrics, audit_logger=AuditLogger(sink, "test"), limiter=limiter
    )
    gate.arm()
    incoming = [
        {"type": "http.request", "body": chunk, "more_body": i < len(body_chunks) - 1}
        for i, chunk in enumerate(body_chunks)
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "headers": [(b"content-type", b"application/json")],
    }
    await gate(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        payload: dict[str, Any] | bytes = json.loads(raw)
    except ValueError:
        payload = raw
    return status, payload, downstream, metrics, sink, limiter


def _body(**extra: Any) -> bytes:
    return json.dumps(
        {"model": "corp-chat", "messages": [{"role": "user", "content": "hi"}], **extra}
    ).encode()


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/messages", "/v1/responses"])
async def test_a_top_level_policies_key_is_refused_before_litellm_or_a_slot(
    path: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        status, payload, downstream, metrics, sink, limiter = await _post(
            [_body(policies=[CANARY])], path=path
        )

    assert status == 403
    assert isinstance(payload, dict)
    assert payload["error"]["code"] == "E_ROUTE_BLOCKED"
    assert payload["error"]["reason"] == ROUTE_GATE_BODY_POLICIES
    assert downstream.bodies == []
    assert metrics.inflight == [] and limiter.inflight == 0
    assert limiter.buffered_bytes == 0
    assert metrics.blocks == [ROUTE_GATE_BODY_POLICIES]
    (record,) = sink.records
    assert (record["status"], record["block_reason"]) == ("failed", ROUTE_GATE_BODY_POLICIES)
    assert CANARY not in caplog.text and CANARY not in json.dumps(payload)
    assert CANARY not in json.dumps(record)


async def test_an_escaped_key_is_the_same_key() -> None:
    body = b'{"model": "corp-chat", "messages": [], "\\u0070olicies": ["' + CANARY.encode() + b'"]}'

    status, _, downstream, *_ = await _post([body])

    assert status == 403 and downstream.bodies == []


async def test_a_key_split_across_body_chunks_is_seen() -> None:
    body = _body(policies=[CANARY])
    cut = body.index(b"polic") + 3

    status, _, downstream, *_ = await _post([body[:cut], body[cut:]])

    assert status == 403 and downstream.bodies == []


@pytest.mark.parametrize(
    "body",
    [
        _body(metadata={"policies": [CANARY]}),
        json.dumps(
            {"model": "corp-chat", "messages": [{"role": "user", "content": "our policies"}]}
        ).encode(),
        b"policies: not json",
        b'[{"policies": 1}]',
        b"",
    ],
    ids=["nested", "in-content", "not-json", "top-level-array", "empty"],
)
async def test_anything_but_a_top_level_key_is_served(body: bytes) -> None:
    status, payload, downstream, metrics, *_ = await _post([body])

    assert status == 200 and payload == b"served"
    assert downstream.bodies == [body]
    assert metrics.blocks == []


def test_the_reason_is_one_of_the_gates_own() -> None:
    assert ROUTE_GATE_BODY_POLICIES in BLOCK_REASONS
