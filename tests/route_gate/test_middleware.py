from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.metrics import MetricsExporter
from corp_llm_gateway.route_gate import (
    BLOCK_REASONS,
    COMPONENT,
    ROUTE_GATE_ERROR,
    ROUTE_GATE_LISTED,
    ROUTE_GATE_MALFORMED,
    ROUTE_GATE_UNARMED,
    ROUTE_GATE_UNLISTED,
    ROUTE_GATE_WEBSOCKET,
    RouteGateMiddleware,
    parse_extras,
)

CANARY = "AKIAIOSFODNN7EXAMPLE"


class _RecordingMetrics(MetricsExporter):
    def __init__(self) -> None:
        self.blocks: list[str] = []
        self.failures: list[str] = []
        self.latencies: list[tuple[float, str]] = []

    def record_block(self, block_reason: str) -> None:
        self.blocks.append(block_reason)

    def record_failure(self, component: str) -> None:
        self.failures.append(component)

    def observe_request_latency(self, seconds: float, *, status: str) -> None:
        self.latencies.append((seconds, status))


class _Stub:
    """ASGI app that records everything it was handed."""

    def __init__(self) -> None:
        self.scopes: list[dict[str, Any]] = []
        self.bodies: list[bytes] = []
        self.lifespan_events: list[str] = []

    @property
    def called(self) -> bool:
        return bool(self.scopes)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.scopes.append(dict(scope))
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                self.lifespan_events.append(message["type"])
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = b""
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                break
            body += message.get("body", b"")
            more_body = message.get("more_body", False)
        self.bodies.append(body)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"x-stub", b"1")]})
        await send({"type": "http.response.body", "body": b"upstream"})


def _gate(
    stub: Any,
    *,
    metrics: _RecordingMetrics | None = None,
    sink: ListSink | None = None,
    armed: bool = False,
    extras: Any = None,
) -> RouteGateMiddleware:
    gate = RouteGateMiddleware(
        stub,
        metrics=metrics if metrics is not None else _RecordingMetrics(),
        audit_logger=AuditLogger(sink if sink is not None else ListSink(), "test"),
        extras=extras,
    )
    if armed:
        gate.arm()
    return gate


def _http_scope(
    method: str = "POST",
    path: str = "/v1/messages",
    *,
    raw_path: bytes | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode() if raw_path is None else raw_path,
        "headers": [(b"content-type", b"application/json")] if headers is None else headers,
    }


async def _drive(
    app: Any, scope: dict[str, Any], *, body: bytes = b"", incoming: list[Any] | None = None
) -> list[dict[str, Any]]:
    pending = (
        list(incoming)
        if incoming is not None
        else [{"type": "http.request", "body": body, "more_body": False}]
    )
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _status(sent: list[dict[str, Any]]) -> int:
    return int(sent[0]["status"])


def _body(sent: list[dict[str, Any]]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"].endswith("response.body"))


def _json(sent: list[dict[str, Any]]) -> dict[str, Any]:
    return json.loads(_body(sent))


# ── lifespan ────────────────────────────────────────────────────────────────


async def test_lifespan_startup_and_shutdown_reach_the_app() -> None:
    stub = _Stub()
    sent = await _drive(
        _gate(stub),
        {"type": "lifespan"},
        incoming=[{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}],
    )
    assert stub.lifespan_events == ["lifespan.startup", "lifespan.shutdown"]
    assert [m["type"] for m in sent] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


# ── refusals ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "raw_path", "status", "reason", "code"),
    [
        ("POST", "/v1/some/future/route", None, 404, ROUTE_GATE_UNLISTED, "E_ROUTE_BLOCKED"),
        ("POST", "/v1/messages/count_tokens", None, 403, ROUTE_GATE_LISTED, "E_ROUTE_BLOCKED"),
        (
            "POST",
            "/v1/messages/../../key/generate",
            b"/v1/messages/..%2f..%2fkey/generate",
            403,
            ROUTE_GATE_MALFORMED,
            "E_ROUTE_BLOCKED",
        ),
        ("POST", "/v1/messages", None, 503, ROUTE_GATE_UNARMED, "E_ROUTE_GATE_UNARMED"),
    ],
)
async def test_a_refusal_returns_the_documented_shape(
    method: str, path: str, raw_path: bytes | None, status: int, reason: str, code: str
) -> None:
    stub = _Stub()
    metrics = _RecordingMetrics()
    sent = await _drive(
        _gate(stub, metrics=metrics),
        _http_scope(method, path, raw_path=raw_path),
        body=CANARY.encode(),
    )
    assert _status(sent) == status
    assert _json(sent) == {
        "error": {
            "type": "route_blocked" if code == "E_ROUTE_BLOCKED" else reason,
            "code": code,
            "route": f"{method} {path}",
            "reason": reason,
        }
    }
    assert metrics.blocks == [reason]
    assert not stub.called


async def test_a_refusal_echoes_no_byte_of_the_request_body() -> None:
    stub = _Stub()
    sent = await _drive(
        _gate(stub),
        _http_scope("POST", "/v1/embeddings"),
        body=json.dumps({"input": CANARY}).encode(),
    )
    assert CANARY.encode() not in b"".join(json.dumps(m, default=str).encode() for m in sent)
    assert not stub.called


async def test_a_refusal_answers_with_json_content_type_and_length() -> None:
    sent = await _drive(_gate(_Stub()), _http_scope("POST", "/v1/completions"))
    headers = dict(sent[0]["headers"])
    assert headers[b"content-type"] == b"application/json"
    assert int(headers[b"content-length"]) == len(_body(sent))


async def test_an_unarmed_gate_records_a_component_failure() -> None:
    metrics = _RecordingMetrics()
    await _drive(_gate(_Stub(), metrics=metrics), _http_scope("POST", "/v1/messages"))
    assert metrics.blocks == [ROUTE_GATE_UNARMED]
    assert metrics.failures == [COMPONENT]


async def test_every_documented_block_reason_is_reachable() -> None:
    metrics = _RecordingMetrics()
    stub = _Stub()
    gate = _gate(stub, metrics=metrics)
    await _drive(gate, _http_scope("POST", "/v1/some/future/route"))
    await _drive(gate, _http_scope("POST", "/v1/messages/count_tokens"))
    await _drive(gate, _http_scope("POST", "/v1/messages"))
    await _drive(
        gate,
        _http_scope("POST", "/v1/messages/../key/generate", raw_path=b"/v1/messages/..%2fkey"),
    )
    await _drive(
        gate,
        {"type": "websocket", "path": "/v1/responses", "headers": []},
        incoming=[{"type": "websocket.connect"}],
    )
    await _drive(gate, _http_scope("POST", "/v1/messages", headers=[(b"upgrade", b"websocket")]))
    assert set(metrics.blocks) <= BLOCK_REASONS
    assert set(metrics.blocks) == {
        ROUTE_GATE_UNLISTED,
        ROUTE_GATE_LISTED,
        ROUTE_GATE_UNARMED,
        ROUTE_GATE_MALFORMED,
        ROUTE_GATE_WEBSOCKET,
    }
    assert not stub.called


# ── passthrough / rewritten ─────────────────────────────────────────────────


async def test_passthrough_forwards_headers_and_body_byte_identically() -> None:
    stub = _Stub()
    headers = [(b"content-type", b"application/json"), (b"x-corp-auth", b"ct_token")]
    scope = _http_scope("GET", "/v1/models", headers=headers)
    sent = await _drive(_gate(stub), scope, body=b'{"hello":"world"}')
    assert stub.scopes[0]["headers"] == headers
    assert stub.scopes[0]["path"] == "/v1/models"
    assert stub.bodies == [b'{"hello":"world"}']
    assert _status(sent) == 200
    assert _body(sent) == b"upstream"


async def test_a_rewritten_route_is_forwarded_once_armed() -> None:
    stub = _Stub()
    sent = await _drive(_gate(stub, armed=True), _http_scope("POST", "/v1/messages"))
    assert stub.called
    assert _status(sent) == 200


async def test_an_extra_passthrough_route_is_forwarded() -> None:
    stub = _Stub()
    gate = _gate(stub, extras=parse_extras("GET /internal/ops-status"))
    sent = await _drive(gate, _http_scope("GET", "/internal/ops-status"))
    assert stub.called
    assert _status(sent) == 200


async def test_a_streamed_response_is_not_buffered() -> None:
    released = asyncio.Event()
    finished = False

    async def streaming_stub(scope: Any, receive: Any, send: Any) -> None:
        nonlocal finished
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: one\n\n", "more_body": True})
        await released.wait()
        await send({"type": "http.response.body", "body": b"data: two\n\n", "more_body": False})
        finished = True

    seen: list[bytes] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            seen.append(message["body"])
            # The first chunk must reach the client before the app is done.
            assert not finished
            released.set()

    gate = _gate(streaming_stub, armed=True)
    await asyncio.wait_for(gate(_http_scope("POST", "/v1/messages"), receive, send), timeout=2)
    assert seen == [b"data: one\n\n", b"data: two\n\n"]


# ── websocket ───────────────────────────────────────────────────────────────


async def test_a_websocket_handshake_is_refused_with_a_denial_response() -> None:
    stub = _Stub()
    metrics = _RecordingMetrics()
    scope = {
        "type": "websocket",
        "path": "/v1/responses",
        "raw_path": b"/v1/responses",
        "headers": [],
        "extensions": {"websocket.http.response": {}},
    }
    sent = await _drive(
        _gate(stub, metrics=metrics), scope, incoming=[{"type": "websocket.connect"}]
    )
    assert [m["type"] for m in sent] == [
        "websocket.http.response.start",
        "websocket.http.response.body",
    ]
    assert _status(sent) == 403
    assert _json(sent)["error"]["reason"] == ROUTE_GATE_WEBSOCKET
    assert metrics.blocks == [ROUTE_GATE_WEBSOCKET]
    assert not stub.called


async def test_a_websocket_handshake_closes_when_the_server_has_no_denial_extension() -> None:
    stub = _Stub()
    scope = {"type": "websocket", "path": "/v1/responses", "headers": []}
    sent = await _drive(_gate(stub), scope, incoming=[{"type": "websocket.connect"}])
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert not stub.called


async def test_the_connect_event_is_received_before_anything_is_sent() -> None:
    order: list[str] = []

    async def receive() -> dict[str, Any]:
        order.append("receive")
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        order.append("send")

    scope = {"type": "websocket", "path": "/v1/responses", "headers": []}
    await _gate(_Stub())(scope, receive, send)
    assert order[0] == "receive"


async def test_a_disconnect_before_connect_sends_nothing() -> None:
    stub = _Stub()
    scope = {"type": "websocket", "path": "/v1/responses", "headers": []}
    sent = await _drive(_gate(stub), scope, incoming=[{"type": "websocket.disconnect"}])
    assert sent == []
    assert not stub.called


# ── classifier failure ──────────────────────────────────────────────────────


async def test_a_classifier_exception_is_a_500_and_never_forwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("table is broken")

    monkeypatch.setattr("corp_llm_gateway.route_gate.middleware.classify", boom)
    stub = _Stub()
    metrics = _RecordingMetrics()
    sent = await _drive(
        _gate(stub, metrics=metrics, armed=True), _http_scope("POST", "/v1/messages")
    )
    assert _status(sent) == 500
    assert _json(sent)["error"]["code"] == "E_ROUTE_GATE_ERROR"
    assert metrics.blocks == [ROUTE_GATE_ERROR]
    assert metrics.failures == [COMPONENT]
    assert not stub.called


async def test_an_unknown_scope_type_is_not_forwarded() -> None:
    stub = _Stub()
    metrics = _RecordingMetrics()
    sent = await _drive(_gate(stub, metrics=metrics, armed=True), {"type": "quic", "headers": []})
    assert sent == []
    assert metrics.blocks == [ROUTE_GATE_ERROR]
    assert metrics.failures == [COMPONENT]
    assert not stub.called


# ── logging ─────────────────────────────────────────────────────────────────


def _log_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records) + caplog.text


async def test_an_unlisted_refusal_logs_no_path(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="corp_llm_gateway.route_gate.middleware")
    await _drive(_gate(_Stub()), _http_scope("POST", f"/v1/{CANARY}/route"))
    text = _log_text(caplog)
    assert ROUTE_GATE_UNLISTED in text
    assert CANARY not in text


async def test_a_malformed_refusal_logs_no_path(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="corp_llm_gateway.route_gate.middleware")
    path = f"/v1/messages/../{CANARY}"
    await _drive(
        _gate(_Stub()),
        _http_scope("POST", path, raw_path=f"/v1/messages/..%2f{CANARY}".encode()),
    )
    text = _log_text(caplog)
    assert ROUTE_GATE_MALFORMED in text
    assert CANARY not in text


async def test_a_classifier_failure_logs_neither_path_nor_exception_message(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(f"cannot parse {CANARY}")

    monkeypatch.setattr("corp_llm_gateway.route_gate.middleware.classify", boom)
    caplog.set_level(logging.DEBUG, logger="corp_llm_gateway.route_gate.middleware")
    await _drive(_gate(_Stub()), _http_scope("POST", f"/v1/{CANARY}"))
    text = _log_text(caplog)
    assert "route_gate_classify_failed" in text
    assert "RuntimeError" in text
    assert CANARY not in text


async def test_a_refusal_logs_only_a_known_method(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="corp_llm_gateway.route_gate.middleware")
    # An HTTP method is a caller-chosen token, so it is narrowed before logging.
    await _drive(_gate(_Stub()), _http_scope(CANARY, "/v1/models"))
    text = _log_text(caplog)
    assert "method=other" in text
    assert CANARY not in text


# ── audit ───────────────────────────────────────────────────────────────────


async def test_a_refusal_writes_one_audit_record_with_the_block_reason() -> None:
    sink = ListSink()
    await _drive(
        _gate(_Stub(), sink=sink),
        _http_scope("POST", "/v1/messages/count_tokens"),
        body=CANARY.encode(),
    )
    assert len(sink.records) == 1
    record = sink.records[0]
    assert record["block_reason"] == ROUTE_GATE_LISTED
    assert record["error_code"] == "E_ROUTE_BLOCKED"
    assert record["status"] == "failed"
    assert CANARY not in json.dumps(record)
    assert "/v1/messages/count_tokens" not in json.dumps(record)


def test_every_block_reason_has_a_status_a_code_and_a_type() -> None:
    from corp_llm_gateway.route_gate import middleware

    assert set(middleware._STATUS) == BLOCK_REASONS
    assert set(middleware._ERROR_CODE) == BLOCK_REASONS
    assert set(middleware._ERROR_TYPE) == BLOCK_REASONS


async def test_a_passthrough_writes_no_audit_record() -> None:
    sink = ListSink()
    await _drive(_gate(_Stub(), sink=sink), _http_scope("GET", "/v1/models"))
    assert sink.records == []


async def test_a_failing_audit_sink_does_not_unblock_the_refusal() -> None:
    class _BrokenSink(ListSink):
        async def write(self, record: dict[str, Any]) -> None:
            raise RuntimeError("sink down")

    metrics = _RecordingMetrics()
    gate = RouteGateMiddleware(
        _Stub(),
        metrics=metrics,
        audit_logger=AuditLogger(_BrokenSink(), "test"),
    )
    sent = await _drive(gate, _http_scope("POST", "/v1/messages/count_tokens"))
    assert _status(sent) == 403
    assert metrics.blocks == [ROUTE_GATE_LISTED]
    assert metrics.failures == [COMPONENT]
