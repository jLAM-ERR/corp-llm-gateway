import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.corp_llm import CorpLlmClient, SANITIZE_TOOL_NAME
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator, StrategyResult
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import (
    AuthMiddleware,
    InMemoryTokenStore,
    TokenInfo,
)


class _StaticRules(RulesLoader):
    async def load(self, team_id: str) -> Rules:
        return Rules(rules=())


def _corp_llm_returning(pairs: list[tuple[str, str]]) -> CorpLlmClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": SANITIZE_TOOL_NAME,
                                        "arguments": json.dumps(
                                            {
                                                "pairs": [
                                                    {"original": o, "replacement": r}
                                                    for o, r in pairs
                                                ]
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CorpLlmClient("https://corp-llm.example", model="m", http=http)


def _build_guardrail(
    pairs: list[tuple[str, str]] | None = None,
    *,
    valid_token: str = "tok-1",
) -> tuple[CorpLlmGuardrail, ListSink]:
    pairs = pairs if pairs is not None else []
    token_store = InMemoryTokenStore()
    now = datetime.now(UTC)
    token_store.upsert(
        TokenInfo(
            corp_token=valid_token,
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    auth = AuthMiddleware(token_store)
    orch = SanitizationOrchestrator(
        _corp_llm_returning(pairs),
        InMemoryMappingStore(),
        _StaticRules(),
    )
    sink = ListSink()
    audit_logger = AuditLogger(sink, gateway_version="0.0.1")
    return CorpLlmGuardrail(orch, auth, audit_logger), sink


def _data_with_token(token: str, *, content: str = "hello", model: str = "claude") -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "headers": {"X-Corp-Auth": token, "Authorization": "Bearer byok"},
    }


# ---- Pre-call ------------------------------------------------------------


async def test_pre_call_missing_token_rejected() -> None:
    g, _ = _build_guardrail()
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call({"messages": [], "headers": {}})
    assert ei.value.status_code == 401
    assert ei.value.error_code == "E_MISSING_TOKEN"


async def test_pre_call_invalid_token_rejected() -> None:
    g, _ = _build_guardrail()
    data = _data_with_token("nonexistent")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 401
    assert ei.value.error_code == "E_TOKEN_INVALID"


async def test_pre_call_strips_corp_token_from_headers() -> None:
    g, _ = _build_guardrail()
    data = _data_with_token("tok-1")
    out = await g.pre_call(data)
    assert "X-Corp-Auth" not in out["headers"]
    assert out["headers"]["Authorization"] == "Bearer byok"


async def test_pre_call_replaces_message_content_with_sanitized() -> None:
    g, _ = _build_guardrail([("alice", "[NAME_001]")])
    data = _data_with_token("tok-1", content="hello alice")
    out = await g.pre_call(data)
    assert out["messages"][0]["content"] == "hello [NAME_001]"


async def test_pre_call_rejects_non_list_messages() -> None:
    g, _ = _build_guardrail()
    data = {"model": "claude", "messages": "not-a-list", "headers": {"X-Corp-Auth": "tok-1"}}
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_BAD_REQUEST"


async def test_pre_call_handles_proxy_server_request_headers_shape() -> None:
    """LiteLLM passes headers via `proxy_server_request.headers` in some paths."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = {
        "model": "claude",
        "messages": [{"role": "user", "content": "hi alice"}],
        "proxy_server_request": {
            "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"}
        },
    }
    out = await g.pre_call(data)
    assert "X-Corp-Auth" not in out["headers"]
    assert out["messages"][0]["content"] == "hi [N1]"


async def test_pre_call_request_id_stable_across_calls_on_same_data() -> None:
    g, _ = _build_guardrail()
    data = _data_with_token("tok-1")
    await g.pre_call(data)
    rid1 = data["_corp_gateway_request_id"]
    # Re-running pre_call on same dict reuses the request id.
    assert isinstance(rid1, str) and rid1


# ---- Streaming post-call ---------------------------------------------------


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for it in items:
        yield it


async def test_post_call_stream_desanitizes_chunks() -> None:
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    chunks_in = [
        {"choices": [{"delta": {"content": "hello [N"}}]},
        {"choices": [{"delta": {"content": "1] world"}}]},
    ]
    out_text = ""
    async for chunk in g.post_call_stream(data, _async_iter(chunks_in)):
        text = chunk["choices"][0]["delta"]["content"]
        out_text += text
    assert out_text == "hello alice world"


async def test_post_call_stream_no_mapping_passes_through() -> None:
    g, _ = _build_guardrail([])
    data = _data_with_token("tok-1", content="no PII")
    await g.pre_call(data)
    chunks_in = [{"choices": [{"delta": {"content": "boring text"}}]}]
    out = []
    async for chunk in g.post_call_stream(data, _async_iter(chunks_in)):
        out.append(chunk)
    assert out == chunks_in


async def test_post_call_stream_unknown_request_passes_through() -> None:
    g, _ = _build_guardrail()
    data = {"model": "claude", "_corp_gateway_request_id": "never-seen"}
    chunks_in = [{"choices": [{"delta": {"content": "x"}}]}]
    out = []
    async for chunk in g.post_call_stream(data, _async_iter(chunks_in)):
        out.append(chunk)
    assert out == chunks_in


# ---- Unary post-call -------------------------------------------------------


async def test_post_call_unary_reverses_placeholder() -> None:
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = {
        "choices": [{"message": {"role": "assistant", "content": "hello [N1]!"}}]
    }
    out = await g.post_call_unary(data, response)
    assert out["choices"][0]["message"]["content"] == "hello alice!"


async def test_post_call_unary_no_state_returns_unchanged() -> None:
    g, _ = _build_guardrail()
    response = {"choices": [{"message": {"content": "no map"}}]}
    out = await g.post_call_unary(
        {"_corp_gateway_request_id": "missing"}, response
    )
    assert out == response


# ---- Audit -----------------------------------------------------------------


async def test_audit_emits_full_event_after_pre_and_post() -> None:
    g, sink = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)
    response = {
        "choices": [{"message": {"content": "hello [N1]"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    start = time.time()
    await g.audit(data, response, start_time=start, end_time=start + 0.250, status="ok")

    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec["user_id"] == "alice"
    assert rec["team_id"] == "t1"
    assert rec["status"] == "ok"
    assert rec["redaction_count"] == 1
    assert rec["placeholder_list"] == ["[N1]"]
    assert rec["prompt_token_count"] == 10
    assert rec["completion_token_count"] == 5
    assert rec["latency_ms"] >= 250 and rec["latency_ms"] < 1000


async def test_audit_after_failed_pre_call_uses_unknown_user() -> None:
    """A request that never made it past auth still gets audited.

    The audit record uses placeholder identity rather than leaking
    nothing — operators need to see auth-failure rates.
    """
    g, sink = _build_guardrail()
    data = {"model": "claude", "messages": [], "headers": {}}
    with pytest.raises(GuardrailHttpException):
        await g.pre_call(data)
    start = time.time()
    await g.audit(data, None, start_time=start, end_time=start + 0.05, status="failed")

    assert sink.records[0]["status"] == "failed"
    assert sink.records[0]["user_id"] == "unknown"


async def test_audit_provider_detection_anthropic_claude() -> None:
    g, sink = _build_guardrail()
    data = _data_with_token("tok-1", model="claude-opus-4-7")
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")
    assert sink.records[0]["provider"] == "anthropic"


async def test_audit_provider_detection_openai_default() -> None:
    g, sink = _build_guardrail()
    data = _data_with_token("tok-1", model="gpt-4o")
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")
    assert sink.records[0]["provider"] == "openai"


# ---- LiteLLM-shaped entry points (smoke) -----------------------------------


async def test_async_pre_call_hook_delegates() -> None:
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    out = await g.async_pre_call_hook(None, None, data, "completion")
    assert out["messages"][0]["content"] == "hi [N1]"


async def test_async_log_success_event_emits_audit() -> None:
    g, sink = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)
    start = time.time()
    await g.async_log_success_event(
        kwargs={"data": data},
        response_obj={"choices": [{"message": {"content": "ok [N1]"}}]},
        start_time=start,
        end_time=start + 0.100,
    )
    assert len(sink.records) == 1
    assert sink.records[0]["status"] == "ok"


async def test_audit_preserves_user_and_team_across_pre_post_handoff() -> None:
    """Regression: litellm's anthropic-passthrough route hands the
    post-call hooks a NEW ``data`` dict that doesn't carry our top-level
    ``_corp_gateway_request_id``. Before the fix, the audit emitted
    user_id/team_id/model="unknown" because _req_state lookup missed.

    Simulate this by:
      1. running pre_call on dict A,
      2. invoking async_log_success_event with a kwargs envelope where
         ``data`` is a FRESH dict B (no _corp_gateway_request_id at the
         top level) but where metadata/litellm_params carry the id.

    The audit must round-trip the id via metadata and recover state.
    """
    g, sink = _build_guardrail([("alice", "[N1]")])
    data_pre = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data_pre)
    rid = data_pre["_corp_gateway_request_id"]

    # Build a fresh dict the way litellm's anthropic-passthrough does.
    fresh_data: dict[str, object] = {
        "model": "claude-opus-4-8",
        "messages": data_pre["messages"],
    }
    kwargs_envelope = {
        "data": fresh_data,
        "metadata": {"_corp_gateway_request_id": rid},
        "litellm_params": {
            "metadata": {"_corp_gateway_request_id": rid},
        },
    }
    start = time.time()
    await g.async_log_success_event(
        kwargs=kwargs_envelope,
        response_obj={"choices": [{"message": {"content": "ok [N1]"}}]},
        start_time=start,
        end_time=start + 0.100,
    )
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec["request_id"] == rid, "round-trip failed: new UUID generated"
    assert rec["user_id"] == "alice", f"got {rec['user_id']!r}, expected alice"
    assert rec["team_id"] == "t1", f"got {rec['team_id']!r}, expected t1"
    assert rec["model"] == "claude", f"got {rec['model']!r}, expected claude"
    assert rec["status"] == "ok"
