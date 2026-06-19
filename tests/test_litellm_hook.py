import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import (
    AuthMiddleware,
    InMemoryTokenStore,
    TokenInfo,
)
from tests.sanitizer.test_streaming import (
    _MSG_DELTA,
    _MSG_START,
    _MSG_STOP,
    _PING,
    _cb_start,
    _cb_stop,
    _delta,
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


def _corp_llm_unreachable() -> CorpLlmClient:
    """A corp LLM whose transport always times out.

    Simulates the corp sanitization LLM being unreachable — the 30s
    ConnectTimeout from the debug-log incident that surfaced to the dev
    as an empty ``500 {"message":"corp-llm transport error: "}``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CorpLlmClient("https://corp-llm.example", model="m", http=http)


def _build_guardrail(
    pairs: list[tuple[str, str]] | None = None,
    *,
    valid_token: str = "tok-1",
    corp_llm: CorpLlmClient | None = None,
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
        corp_llm if corp_llm is not None else _corp_llm_returning(pairs),
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


async def test_pre_call_corp_llm_down_fails_closed_503() -> None:
    """Fail-policy matrix (M4): a corp-LLM sanitization failure must fail
    CLOSED with 503 E_CORP_LLM_DOWN — not leak as a generic 500.

    Regression for the field incident where a 30s corp-LLM timeout
    surfaced to Claude Code as ``500 {"message":"corp-llm transport
    error: "}`` (empty — httpx timeouts stringify to '') because
    pre_call let the raw CorpLlmHttpError escape.
    """
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    data = _data_with_token("tok-1", content="hello alice")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 503
    assert ei.value.error_code == "E_CORP_LLM_DOWN"


async def test_pre_call_corp_llm_down_does_not_forward_content() -> None:
    """Belt-and-braces on the fail-closed posture: when sanitization
    can't run, the request must be rejected — never forwarded with the
    original (un-sanitized) content."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    data = _data_with_token("tok-1", content="my secret is hunter2")
    with pytest.raises(GuardrailHttpException):
        await g.pre_call(data)
    # The raise short-circuits the upstream call; the original content
    # was never handed back as a sanitized payload.
    assert data["messages"][0]["content"] == "my secret is hunter2"


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


async def test_post_call_stream_openai_dict_gpt4o_contract() -> None:
    """Lock the dict contract for gpt-4o model — must desanitize choices delta content."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice", model="gpt-4o")
    await g.pre_call(data)

    chunks_in = [
        {"choices": [{"delta": {"content": "hello [N"}}]},
        {"choices": [{"delta": {"content": "1] world"}}]},
    ]
    out_text = ""
    async for chunk in g.post_call_stream(data, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    assert out_text == "hello alice world"


async def test_post_call_stream_anthropic_sse_bytes_placeholder_restored() -> None:
    """Anthropic SSE bytes: placeholder split across deltas is restored, framing intact."""
    g, _ = _build_guardrail([("user@example.com", "[EMAIL_001]")])
    data = _data_with_token("tok-1", content="email is user@example.com")
    await g.pre_call(data)

    # Matches the verified wire format: bytes SSE events from litellm's Anthropic passthrough.
    # [EMAIL_001] arrives as 5 separate text_delta events (the captured live split).
    sse_events: list[bytes] = [
        _MSG_START,
        _cb_start(0),
        _PING,
        _delta(" ["),
        _delta("EMAIL"),
        _delta("_"),
        _delta("001"),
        _delta("]"),
        _cb_stop(0),
        _MSG_DELTA,
        _MSG_STOP,
    ]

    out_chunks: list[bytes] = []
    async for chunk in g.post_call_stream(data, _async_iter(sse_events)):
        assert isinstance(chunk, bytes), f"expected bytes, got {type(chunk)}"
        out_chunks.append(chunk)

    # Collect all text from content_block_delta chunks.
    text_parts: list[str] = []
    for chunk in out_chunks:
        for line in chunk.decode().splitlines():
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].lstrip())
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "content_block_delta":
                    delta = obj.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text_parts.append(delta["text"])

    full_text = "".join(text_parts)
    assert "user@example.com" in full_text, f"original not restored: {full_text!r}"
    assert "[EMAIL_001]" not in full_text

    # Verify message_stop and content_block_stop framing are intact.
    all_types = set()
    for chunk in out_chunks:
        for line in chunk.decode().splitlines():
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].lstrip())
                    all_types.add(obj.get("type"))
                except json.JSONDecodeError:
                    pass
    assert "message_stop" in all_types
    assert "content_block_stop" in all_types


# ---- Unary post-call -------------------------------------------------------


async def test_post_call_unary_reverses_placeholder() -> None:
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = {"choices": [{"message": {"role": "assistant", "content": "hello [N1]!"}}]}
    out = await g.post_call_unary(data, response)
    assert out["choices"][0]["message"]["content"] == "hello alice!"


async def test_post_call_unary_no_state_returns_unchanged() -> None:
    g, _ = _build_guardrail()
    response = {"choices": [{"message": {"content": "no map"}}]}
    out = await g.post_call_unary({"_corp_gateway_request_id": "missing"}, response)
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

    NOTE: this covers the legacy metadata-scatter FALLBACK (no
    ``litellm_call_id`` in the envelope). The primary path — litellm's own
    ``litellm_call_id`` carried across the boundary — is covered by
    ``test_audit_recovers_state_via_litellm_call_id``.
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


async def test_audit_recovers_state_via_litellm_call_id() -> None:
    """Regression: litellm v1.85 carries ``litellm_call_id`` (NOT our scattered
    ``_corp_gateway_request_id``, and no top-level ``metadata``) across the
    pre_call → log-event boundary. Per-request state must be keyed on
    ``litellm_call_id`` so the audit keeps the real identity + redaction count.

    Before the fix the audit fell back to a fresh UUID → user/team/model
    "unknown" and redaction_count 0 (seen live as audit records whose
    request_id differed from pre_call's).
    """
    g, sink = _build_guardrail([("alice", "[N1]")])
    call_id = "litellm-call-abc123"
    data = _data_with_token("tok-1", content="hi alice")
    data["litellm_call_id"] = call_id
    await g.pre_call(data)
    assert data["_corp_gateway_request_id"] == call_id  # state keyed on litellm id

    # litellm's log-event envelope, matching the live v1.85 shape: carries
    # litellm_call_id but NOT our id, and no top-level metadata dict.
    kwargs = {
        "litellm_call_id": call_id,
        "optional_params": {"model": "claude"},
        "litellm_params": {"litellm_call_id": call_id, "metadata": {}},
    }
    start = time.time()
    await g.async_log_success_event(
        kwargs=kwargs,
        response_obj={
            "choices": [{"message": {"content": "ok [N1]"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        },
        start_time=start,
        end_time=start + 0.1,
    )
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec["request_id"] == call_id, f"got {rec['request_id']!r}"
    assert rec["user_id"] == "alice"
    assert rec["team_id"] == "t1"
    assert rec["model"] == "claude"
    assert rec["redaction_count"] == 1
    assert rec["status"] == "ok"


async def test_audit_extracts_tokens_from_response_object_usage() -> None:
    """litellm hands async_log_*_event a ModelResponse OBJECT whose ``.usage``
    is a Usage object (attribute access), not a dict. The audit must still
    capture token counts — regression: the dict-only path logged 0/0.
    """
    g, sink = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    class _Usage:
        prompt_tokens = 13
        completion_tokens = 48

    class _ModelResponse:
        usage = _Usage()

    start = time.time()
    await g.audit(data, _ModelResponse(), start_time=start, end_time=start + 0.1, status="ok")
    rec = sink.records[0]
    assert rec["prompt_token_count"] == 13
    assert rec["completion_token_count"] == 48


async def test_audit_extracts_tokens_anthropic_usage_shape() -> None:
    """Anthropic-style usage (input_tokens/output_tokens) is captured too."""
    g, sink = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)
    response = {"usage": {"input_tokens": 20, "output_tokens": 7}}
    start = time.time()
    await g.audit(data, response, start_time=start, end_time=start + 0.1, status="ok")
    rec = sink.records[0]
    assert rec["prompt_token_count"] == 20
    assert rec["completion_token_count"] == 7
