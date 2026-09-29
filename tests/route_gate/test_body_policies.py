"""The gate refuses an admitted rewritten body that names litellm policies, or that is
not JSON.

litellm pops a top-level ``policies`` key off the request body and applies those policies
to the request (``litellm_pre_call_utils.py``, hazard 14b of plan 20260926); the gate
refuses it once the in-flight limiter has read the body, before any slot is taken and
before litellm parses it. litellm also reads a form body (``request.form()``), where a
``policies`` field reaches the same place: the rewritten routes take JSON only, so any
other ``Content-Type`` (or none, on a non-empty body) is refused the same way. The
``policies`` check reads bytes, which proves a key absent only for UTF-8: a ``charset``
other than UTF-8, a BOM, UTF-16/32 or bytes that do not decode as UTF-8 are refused too.
Content-free: the refusal and its log name no body byte.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.metrics import MetricsExporter
from corp_llm_gateway.route_gate import (
    BLOCK_REASONS,
    ROUTE_GATE_BODY_NOT_JSON,
    ROUTE_GATE_BODY_POLICIES,
    RouteGateMiddleware,
)
from corp_llm_gateway.route_gate.inflight import InflightLimiter
from corp_llm_gateway.route_gate.middleware import _body_problem

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


_JSON = b"application/json"


async def _post(
    body_chunks: list[bytes],
    *,
    path: str = "/v1/chat/completions",
    content_type: bytes | None = _JSON,
    method: str = "POST",
    extra_headers: list[tuple[bytes, bytes]] | None = None,
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
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "headers": ([] if content_type is None else [(b"content-type", content_type)])
        + (extra_headers or []),
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


# ── rewritten routes take JSON only ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        (b"application/x-www-form-urlencoded", f"model=corp-chat&policies={CANARY}".encode()),
        (
            b"multipart/form-data; boundary=b",
            b'--b\r\nContent-Disposition: form-data; name="policies"\r\n\r\n'
            + CANARY.encode()
            + b"\r\n--b--\r\n",
        ),
        (b"text/plain", _body(policies=[CANARY])),
        (b"application/json-patch+json", _body()),
        (b"application/jsonx", _body()),
        (None, _body(policies=[CANARY])),
        (None, _body()),
    ],
    ids=["form", "multipart", "text", "json-patch", "jsonx", "missing-policies", "missing"],
)
async def test_a_body_that_is_not_json_is_refused_before_litellm_or_a_slot(
    content_type: bytes | None, body: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        status, payload, downstream, metrics, sink, limiter = await _post(
            [body], content_type=content_type
        )

    assert status == 415
    assert isinstance(payload, dict)
    assert payload["error"]["code"] == "E_ROUTE_BLOCKED"
    assert payload["error"]["reason"] == ROUTE_GATE_BODY_NOT_JSON
    assert downstream.bodies == []
    assert metrics.inflight == [] and limiter.inflight == 0
    assert limiter.buffered_bytes == 0
    assert metrics.blocks == [ROUTE_GATE_BODY_NOT_JSON]
    (record,) = sink.records
    assert (record["status"], record["block_reason"]) == ("failed", ROUTE_GATE_BODY_NOT_JSON)
    assert CANARY not in caplog.text and CANARY not in json.dumps(payload)
    assert CANARY not in json.dumps(record)


@pytest.mark.parametrize(
    "content_type",
    [
        b"application/json; charset=utf-8",
        b"Application/JSON",
        b"application/json ;charset=UTF-8",
        b"application/json; charset=UTF-8",
        b'application/json; charset="utf-8"',
        b"application/json; charset=utf8",
        b"application/json; charset=utf-8 ; foo=bar",
        b"application/json; Charset = utf-8",
    ],
)
async def test_json_with_parameters_is_served(content_type: bytes) -> None:
    body = _body()

    status, payload, downstream, metrics, *_ = await _post([body], content_type=content_type)

    assert status == 200 and payload == b"served"
    assert downstream.bodies == [body]
    assert metrics.blocks == []


async def test_two_content_types_are_not_json() -> None:
    """Which one litellm would read is its business; the gate reads neither."""
    status, payload, downstream, *_ = await _post(
        [_body()], extra_headers=[(b"Content-Type", b"application/x-www-form-urlencoded")]
    )

    assert status == 415 and downstream.bodies == []
    assert isinstance(payload, dict) and payload["error"]["reason"] == ROUTE_GATE_BODY_NOT_JSON


async def test_an_empty_body_without_a_content_type_is_litellms_to_answer() -> None:
    status, _, downstream, metrics, *_ = await _post([b""], content_type=None)

    assert status == 200 and downstream.bodies == [b""]
    assert metrics.blocks == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_a_passthrough_read_is_not_asked_for_a_content_type(method: str) -> None:
    status, _, downstream, metrics, *_ = await _post(
        [b""], path="/v1/models", content_type=None, method=method
    )

    assert status == 200 and downstream.bodies == [b""]
    assert metrics.blocks == []


def test_the_not_json_reason_is_one_of_the_gates_own() -> None:
    assert ROUTE_GATE_BODY_NOT_JSON in BLOCK_REASONS


# ── rewritten routes take UTF-8 JSON only ────────────────────────────────────


def _assert_not_json_before_litellm_or_a_slot(
    status: int,
    payload: dict[str, Any] | bytes,
    downstream: _Downstream,
    metrics: _Metrics,
    sink: ListSink,
    limiter: InflightLimiter,
) -> None:
    assert status == 415
    assert isinstance(payload, dict)
    assert payload["error"]["reason"] == ROUTE_GATE_BODY_NOT_JSON
    assert downstream.bodies == []
    assert metrics.inflight == [] and limiter.inflight == 0
    assert limiter.buffered_bytes == 0
    assert metrics.blocks == [ROUTE_GATE_BODY_NOT_JSON]
    (record,) = sink.records
    assert (record["status"], record["block_reason"]) == ("failed", ROUTE_GATE_BODY_NOT_JSON)


def _policies_text() -> str:
    return json.dumps({"model": "corp-chat", "messages": [], "policies": [CANARY]})


@pytest.mark.parametrize(
    "encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-8-sig"]
)
async def test_a_body_that_is_not_utf8_is_refused_before_litellm_or_a_slot(
    encoding: str, caplog: pytest.LogCaptureFixture
) -> None:
    body = _policies_text().encode(encoding)
    assert b"policies" not in body or encoding == "utf-8-sig"
    assert json.loads(body)["policies"] == [CANARY]

    with caplog.at_level(logging.DEBUG):
        status, payload, downstream, metrics, sink, limiter = await _post([body])

    _assert_not_json_before_litellm_or_a_slot(status, payload, downstream, metrics, sink, limiter)
    assert CANARY not in caplog.text and CANARY not in json.dumps(payload)


async def test_bytes_that_do_not_decode_as_utf8_are_refused() -> None:
    body = b'{"model": "corp-chat", "messages": [], "poli\xffcies": ["' + CANARY.encode() + b'"]}'
    assert json.loads(body.decode("utf-8", errors="ignore"))["policies"] == [CANARY]

    result = await _post([body])

    _assert_not_json_before_litellm_or_a_slot(*result)


@pytest.mark.parametrize(
    "content_type",
    [
        b"application/json; charset=utf-16",
        b"application/json; charset=latin-1",
        b"application/json; charset=utf-7",
        b"application/json; charset=utf-8; charset=utf-16",
        b"application/json; charset=utf-8; charset=utf-8",
        b"application/json; charset",
        b"application/json; charset=",
        b'application/json; charset="utf-8',
        b'application/json; charset=""',
        b"application/json; charset*=utf-8''",
        b"application/json; charset=utf-8 x",
    ],
    ids=[
        "utf-16",
        "latin-1",
        "utf-7",
        "repeated-other",
        "repeated-same",
        "no-value",
        "empty",
        "unbalanced-quote",
        "empty-quoted",
        "rfc2231",
        "trailing-token",
    ],
)
async def test_a_charset_other_than_utf8_is_refused_before_litellm_or_a_slot(
    content_type: bytes,
) -> None:
    result = await _post([_body()], content_type=content_type)

    _assert_not_json_before_litellm_or_a_slot(*result)


async def test_a_charset_other_than_utf8_is_refused_without_a_body() -> None:
    result = await _post([b""], content_type=b"application/json; charset=utf-16")

    _assert_not_json_before_litellm_or_a_slot(*result)


_STDLIB_ENCODINGS = [
    "utf-8",
    "utf-8-sig",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "utf-32",
    "utf-32-le",
    "utf-32-be",
]


@pytest.mark.parametrize("escaped", [False, True], ids=["plain-key", "escaped-key"])
@pytest.mark.parametrize("declared", [False, True], ids=["no-charset", "charset"])
@pytest.mark.parametrize("encoding", _STDLIB_ENCODINGS)
def test_no_encoding_stdlib_json_decodes_carries_a_policies_key_past_the_gate(
    encoding: str, declared: bool, escaped: bool
) -> None:
    """Whatever parser litellm moves to, the most permissive one (``json.loads(bytes)``)
    finding a top-level ``policies`` key means the gate refused the body."""
    text = _policies_text()
    if escaped:
        text = text.replace('"policies"', '"\\u0070olicies"')
    body = text.encode(encoding)
    assert "policies" in json.loads(body)
    content_type = b"application/json"
    if declared:
        content_type += b"; charset=" + encoding.encode()

    assert _body_problem([body], content_type) in {
        ROUTE_GATE_BODY_NOT_JSON,
        ROUTE_GATE_BODY_POLICIES,
    }
