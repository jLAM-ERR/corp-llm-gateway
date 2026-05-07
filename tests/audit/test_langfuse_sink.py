import base64
import json
from datetime import UTC, datetime

import httpx
import pytest

from corp_llm_gateway.audit import (
    AuditEvent,
    LangfuseIngestionError,
    LangfuseSink,
)


def _event(**overrides: object) -> AuditEvent:
    base: dict[str, object] = {
        "timestamp": datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
        "request_id": "req-1",
        "user_id": "alice",
        "team_id": "t1",
        "provider": "anthropic",
        "model": "claude-opus-4-7",
        "latency_ms": 1234,
        "prompt_token_count": 100,
        "completion_token_count": 50,
        "redaction_count": 2,
        "finding_label_counts": {"EMAIL": 2},
        "cache_a_hit": False,
        "status": "ok",
    }
    base.update(overrides)
    return AuditEvent(**base)  # type: ignore[arg-type]


def _capturing_sink(public: str = "pk", secret: str = "sk") -> tuple[LangfuseSink, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(207, json={"successes": 2, "errors": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = LangfuseSink(
        "https://langfuse.example", public_key=public, secret_key=secret, http=http
    )
    return sink, captured


# Endpoint, headers, auth ---------------------------------------------------


async def test_posts_to_ingestion_endpoint() -> None:
    sink, captured = _capturing_sink()
    await sink.write_event(_event())
    assert len(captured) == 1
    assert str(captured[0].url) == "https://langfuse.example/api/public/ingestion"
    assert captured[0].method == "POST"


async def test_basic_auth_header_correct() -> None:
    sink, captured = _capturing_sink(public="pk-1", secret="sk-1")
    await sink.write_event(_event())
    auth = captured[0].headers["authorization"]
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
    assert decoded == "pk-1:sk-1"


async def test_strips_trailing_slash_in_base_url() -> None:
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(207, json={})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = LangfuseSink(
        "https://langfuse.example/", public_key="pk", secret_key="sk", http=http
    )
    await sink.write_event(_event())
    assert str(captured[0].url) == "https://langfuse.example/api/public/ingestion"


# Batch shape ---------------------------------------------------------------


async def test_emits_one_trace_and_one_generation_per_event() -> None:
    sink, captured = _capturing_sink()
    await sink.write_event(_event())
    body = json.loads(captured[0].content)
    types = [e["type"] for e in body["batch"]]
    assert types == ["trace-create", "generation-create"]


async def test_trace_carries_user_team_metadata() -> None:
    sink, captured = _capturing_sink()
    await sink.write_event(_event())
    body = json.loads(captured[0].content)
    trace = next(e for e in body["batch"] if e["type"] == "trace-create")["body"]
    assert trace["userId"] == "alice"
    assert trace["metadata"]["team_id"] == "t1"
    assert trace["metadata"]["redaction_count"] == 2
    assert "team:t1" in trace["tags"]
    assert "provider:anthropic" in trace["tags"]


async def test_generation_carries_model_and_usage() -> None:
    sink, captured = _capturing_sink()
    await sink.write_event(_event())
    body = json.loads(captured[0].content)
    gen = next(e for e in body["batch"] if e["type"] == "generation-create")["body"]
    assert gen["model"] == "claude-opus-4-7"
    assert gen["usage"] == {"input": 100, "output": 50, "total": 150, "unit": "TOKENS"}


async def test_generation_links_to_trace_id() -> None:
    sink, captured = _capturing_sink()
    await sink.write_event(_event(request_id="req-XYZ"))
    body = json.loads(captured[0].content)
    trace = next(e for e in body["batch"] if e["type"] == "trace-create")["body"]
    gen = next(e for e in body["batch"] if e["type"] == "generation-create")["body"]
    assert trace["id"] == "req-XYZ"
    assert gen["traceId"] == "req-XYZ"


# Conditional fields --------------------------------------------------------


async def test_error_code_in_trace_metadata_when_failed() -> None:
    sink, captured = _capturing_sink()
    await sink.write_event(_event(status="failed", error_code="E_CORP_LLM_DOWN"))
    body = json.loads(captured[0].content)
    trace = next(e for e in body["batch"] if e["type"] == "trace-create")["body"]
    assert trace["metadata"]["error_code"] == "E_CORP_LLM_DOWN"
    assert trace["metadata"]["status"] == "failed"


# write() generic Sink interface --------------------------------------------


async def test_write_record_passes_never_field_gate() -> None:
    sink, captured = _capturing_sink()
    await sink.write({"timestamp": "2026-05-07T12:00:00Z", "user_id": "alice", "request_id": "r1"})
    assert len(captured) == 1


async def test_write_record_rejects_never_fields() -> None:
    sink, _ = _capturing_sink()
    from corp_llm_gateway.audit import NeverFieldPresentError
    with pytest.raises(NeverFieldPresentError):
        await sink.write({"mapping": [["alice", "[N1]"]]})


# Error handling ------------------------------------------------------------


async def test_4xx_raises_ingestion_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad keys")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = LangfuseSink("https://x", public_key="pk", secret_key="sk", http=http)
    with pytest.raises(LangfuseIngestionError, match="401"):
        await sink.write_event(_event())


async def test_5xx_raises_ingestion_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = LangfuseSink("https://x", public_key="pk", secret_key="sk", http=http)
    with pytest.raises(LangfuseIngestionError, match="503"):
        await sink.write_event(_event())


async def test_transport_error_raises_ingestion_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = LangfuseSink("https://x", public_key="pk", secret_key="sk", http=http)
    with pytest.raises(LangfuseIngestionError, match="transport error"):
        await sink.write_event(_event())
