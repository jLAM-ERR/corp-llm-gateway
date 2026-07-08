import json
import logging
import re
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.detectors import DualNerDetector, RegexChecksumDetector
from corp_llm_gateway.detectors.base import Finding, PIIDetector
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


class _RaisingNerEngine(PIIDetector):
    """A NER engine whose model/deps are absent — raises like the real ones do."""

    async def detect(self, text: str) -> list[Finding]:
        raise RuntimeError("ner deps absent")


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


def _corp_llm_email_per_segment() -> CorpLlmClient:
    """A corp LLM that redacts whatever single email it finds in the segment
    to ``[EMAIL_001]`` — modelling the real per-call numbering that makes two
    different emails in two segments collide on the same token."""
    email_re = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seg_text = body["messages"][-1]["content"]
        m = email_re.search(seg_text)
        pairs = [{"original": m.group(0), "replacement": "[EMAIL_001]"}] if m else []
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
                                        "arguments": json.dumps({"pairs": pairs}),
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


def _build_guardrail_oversize(
    *,
    threshold: int,
    policy: str = "fail-closed",
    deliver_teams: frozenset[str] = frozenset(),
    local_detectors: list[Any] | None = None,
) -> tuple[CorpLlmGuardrail, ListSink]:
    """A guardrail whose orchestrator trips the size threshold at *threshold* bytes."""
    token_store = InMemoryTokenStore()
    now = datetime.now(UTC)
    token_store.upsert(
        TokenInfo(
            corp_token="tok-1",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    orch = SanitizationOrchestrator(
        _corp_llm_returning([]),
        InMemoryMappingStore(),
        _StaticRules(),
        size_threshold_bytes=threshold,
        oversize_policy=policy,
        oversize_deliver_teams=deliver_teams,
        local_detectors=local_detectors,
    )
    sink = ListSink()
    return (
        CorpLlmGuardrail(
            orch, AuthMiddleware(token_store), AuditLogger(sink, gateway_version="0.0.1")
        ),
        sink,
    )


def _data_with_token(
    token: str,
    *,
    content: str | list[Any] = "hello",
    model: str = "claude",
    system: str | list[Any] | None = None,
) -> dict:
    data = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "headers": {"X-Corp-Auth": token, "Authorization": "Bearer byok"},
    }
    if system is not None:
        data["system"] = system
    return data


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


async def test_pre_call_cross_segment_email_collision_split_and_restored() -> None:
    """Regression for the cross-segment placeholder collision: a system-blob
    email and a user-message email both get [EMAIL_001] from their independent
    per-segment corp-LLM calls. The per-request allocator must split them so
    (a) egress uses two distinct tokens and (b) BOTH restore on the reverse."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content="contact customer b@corp.example",
        system="admin is a@corp.example",
    )
    out = await g.pre_call(data)

    msg_text = out["messages"][0]["content"]
    sys_text = out["system"]
    # Both originals are redacted out...
    assert "a@corp.example" not in sys_text
    assert "b@corp.example" not in msg_text
    # ...to DIFFERENT tokens (the collision is resolved).
    msg_ph = re.search(r"\[EMAIL_\d+\]", msg_text).group(0)
    sys_ph = re.search(r"\[EMAIL_\d+\]", sys_text).group(0)
    assert msg_ph != sys_ph, (msg_ph, sys_ph)

    # The reverse path restores EACH token to its own original.
    chunks_in = [{"choices": [{"delta": {"content": f"msg={msg_ph} sys={sys_ph}"}}]}]
    out_text = ""
    async for chunk in g.post_call_stream(data, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    assert out_text == "msg=b@corp.example sys=a@corp.example"


async def test_pre_call_same_email_two_segments_reuses_one_token() -> None:
    """The other half of the bijection: the SAME original in two segments must
    reuse ONE token (not be split into two), so the model sees it consistently."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content="email a@corp.example",
        system="a@corp.example is admin",
    )
    out = await g.pre_call(data)
    msg_ph = re.search(r"\[EMAIL_\d+\]", out["messages"][0]["content"]).group(0)
    sys_ph = re.search(r"\[EMAIL_\d+\]", out["system"]).group(0)
    assert msg_ph == sys_ph == "[EMAIL_001]"


async def test_pre_call_collision_across_blocks_in_one_message() -> None:
    """Collision is not only system-vs-message: two text blocks in the SAME
    message carry different emails that both come back [EMAIL_001]."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content=[
            {"type": "text", "text": "first a@corp.example"},
            {"type": "text", "text": "second b@corp.example"},
        ],
    )
    out = await g.pre_call(data)
    blocks = out["messages"][0]["content"]
    ph0 = re.search(r"\[EMAIL_\d+\]", blocks[0]["text"]).group(0)
    ph1 = re.search(r"\[EMAIL_\d+\]", blocks[1]["text"]).group(0)
    assert ph0 != ph1, (ph0, ph1)


async def test_pre_call_collision_message_vs_tool_result() -> None:
    """tool_result content is sanitized via the walker recursion; an email
    there must not collide with a different email in a sibling text block."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content=[
            {"type": "text", "text": "user a@corp.example"},
            {"type": "tool_result", "content": "tool saw b@corp.example"},
        ],
    )
    out = await g.pre_call(data)
    blocks = out["messages"][0]["content"]
    text_ph = re.search(r"\[EMAIL_\d+\]", blocks[0]["text"]).group(0)
    tr_ph = re.search(r"\[EMAIL_\d+\]", blocks[1]["content"]).group(0)
    assert text_ph != tr_ph, (text_ph, tr_ph)


async def test_post_call_stream_unmapped_placeholder_passes_through() -> None:
    """A placeholder the model invented (never in the request mapping) must
    pass through untouched — only known tokens are reversed."""
    g, _ = _build_guardrail([("a@x", "[EMAIL_001]")])
    data = _data_with_token("tok-1", content="a@x")
    await g.pre_call(data)
    chunks_in = [
        {"choices": [{"delta": {"content": "known [EMAIL_001] hallucinated [EMAIL_999]"}}]}
    ]
    out_text = ""
    async for chunk in g.post_call_stream(data, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    assert out_text == "known a@x hallucinated [EMAIL_999]"


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


def _guardrail_with_ner(dual: DualNerDetector) -> CorpLlmGuardrail:
    # Reuse the oversize helper with a huge threshold (no leaf is oversize) to
    # get an orchestrator whose local cascade includes the injected NER.
    g, _ = _build_guardrail_oversize(threshold=10_000_000, local_detectors=[dual])
    return g


async def test_pre_call_ner_required_but_absent_returns_503_not_500() -> None:
    """F2 fail-closed (M4): when CORP_LLM_REQUIRE_NER is on and a configured NER
    engine's model is absent, pre_call must map the NerUnavailableError to a
    503 E_NER_UNAVAILABLE — NOT let it escape as a generic 500."""
    dual = DualNerDetector(require_ner=True, engines=[_RaisingNerEngine(), _RaisingNerEngine()])
    g = _guardrail_with_ner(dual)
    data = _data_with_token("tok-1", content="ping John Smith")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 503
    assert ei.value.error_code == "E_NER_UNAVAILABLE"


async def test_pre_call_ner_required_but_absent_on_system_returns_503() -> None:
    """Same fail-closed path on the system field (empty message → system scan)."""
    dual = DualNerDetector(require_ner=True, engines=[_RaisingNerEngine(), _RaisingNerEngine()])
    g = _guardrail_with_ner(dual)
    data = _data_with_token("tok-1", content="", system="owner John Smith")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 503
    assert ei.value.error_code == "E_NER_UNAVAILABLE"


async def test_pre_call_ner_required_but_absent_does_not_forward_content() -> None:
    dual = DualNerDetector(require_ner=True, engines=[_RaisingNerEngine(), _RaisingNerEngine()])
    g = _guardrail_with_ner(dual)
    data = _data_with_token("tok-1", content="ping John Smith")
    with pytest.raises(GuardrailHttpException):
        await g.pre_call(data)
    # Fail-closed: the request is rejected, never forwarded with the original.
    assert data["messages"][0]["content"] == "ping John Smith"


async def test_pre_call_ner_not_required_stays_on_dev_graceful_path() -> None:
    """require-ner OFF: absent NER degrades to [] (the documented F2 fail-open,
    intentional only for dev / Python 3.14). Request proceeds — no 503."""
    dual = DualNerDetector(require_ner=False, engines=[_RaisingNerEngine(), _RaisingNerEngine()])
    g = _guardrail_with_ner(dual)
    data = _data_with_token("tok-1", content="ping John Smith")
    result = await g.pre_call(data)  # no exception
    # No rule/regex/gazetteer hit and NER degraded → content unchanged (egresses).
    assert result["messages"][0]["content"] == "ping John Smith"


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


# ---- New task tests (tasks 2 & 4) -------------------------------------------


async def test_pre_call_block_list_message_content_sanitized() -> None:
    """Task 2: messages with list-of-blocks content are sanitized (Anthropic shape)."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    content = [
        {"type": "text", "text": "hello alice"},
        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    # Text block is sanitized, image block is untouched.
    assert len(out["messages"][0]["content"]) == 2
    assert out["messages"][0]["content"][0]["type"] == "text"
    assert out["messages"][0]["content"][0]["text"] == "hello [N1]"
    assert out["messages"][0]["content"][1] == content[1]


async def test_pre_call_tool_result_block_sanitized() -> None:
    """Task 2: tool_result blocks with str content are sanitized."""
    g, _ = _build_guardrail([("secret", "[SECRET_001]")])
    content = [
        {
            "type": "tool_result",
            "content": "the secret is revealed",
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    assert out["messages"][0]["content"][0]["type"] == "tool_result"
    assert out["messages"][0]["content"][0]["content"] == "the [SECRET_001] is revealed"


async def test_pre_call_system_str_sanitized() -> None:
    """Task 3: system field as str is sanitized."""
    g, _ = _build_guardrail([("SecretEnv", "[ENV_001]")])
    data = _data_with_token("tok-1", content="hello", system="SecretEnv=/prod")
    out = await g.pre_call(data)

    assert out["system"] == "[ENV_001]=/prod"


async def test_pre_call_system_list_sanitized() -> None:
    """Task 3: system field as list of blocks is sanitized."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    system = [{"type": "text", "text": "Context: alice"}]
    data = _data_with_token("tok-1", content="hello", system=system)
    out = await g.pre_call(data)

    assert isinstance(out["system"], list)
    assert out["system"][0]["text"] == "Context: [N1]"


async def test_pre_call_no_system_no_op() -> None:
    """Task 3: messages without system field are unaffected."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hello alice")
    out = await g.pre_call(data)

    assert "system" not in out
    assert out["messages"][0]["content"] == "hello [N1]"


async def test_pre_call_str_message_regression() -> None:
    """Task 2: plain-string message content still works (OpenAI-compatible regression)."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    out = await g.pre_call(data)

    assert out["messages"][0]["content"] == "hi [N1]"


async def test_post_call_unary_anthropic_native_block_response() -> None:
    """Task 4: Anthropic-native response with top-level content list is desanitized."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    # Anthropic native response shape: {"type":"message","content":[...]}
    response = {
        "type": "message",
        "content": [
            {"type": "text", "text": "hello [N1]!"},
            {"type": "image_url", "image_url": {"url": "https://..."}},
        ],
    }
    out = await g.post_call_unary(data, response)

    assert out["content"][0]["type"] == "text"
    assert out["content"][0]["text"] == "hello alice!"
    assert out["content"][1] == response["content"][1]


async def test_post_call_unary_choices_list_content() -> None:
    """Task 4: choices with list-of-blocks message.content are desanitized."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    # OpenAI-compatible choices format with list content (gpt-4o multimodal).
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "response [N1] text"},
                        {"type": "image_url", "image_url": {"url": "https://..."}},
                    ]
                }
            }
        ]
    }
    out = await g.post_call_unary(data, response)

    assert out["choices"][0]["message"]["content"][0]["type"] == "text"
    assert out["choices"][0]["message"]["content"][0]["text"] == "response alice text"
    original_image = response["choices"][0]["message"]["content"][1]
    assert out["choices"][0]["message"]["content"][1] == original_image


async def test_post_call_unary_choices_str_content_regression() -> None:
    """Task 4: choices with str message.content still work (OpenAI str regression)."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = {"choices": [{"message": {"content": "hello [N1]!"}}]}
    out = await g.post_call_unary(data, response)

    assert out["choices"][0]["message"]["content"] == "hello alice!"


async def test_post_call_unary_gpt4o_multimodal_content_parts() -> None:
    """Task 4: OpenAI gpt-4o multimodal content-parts are handled (image untouched)."""
    g, _ = _build_guardrail([("user@example.com", "[EMAIL_001]")])
    data = _data_with_token("tok-1", content="contact user@example.com", model="gpt-4o")
    await g.pre_call(data)

    # gpt-4o-style response with mixed content types.
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "Email is [EMAIL_001]"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "https://example.com/image.png",
                                "detail": "high",
                            },
                        },
                    ]
                }
            }
        ]
    }
    out = await g.post_call_unary(data, response)

    # Text part reversed, image part untouched.
    assert out["choices"][0]["message"]["content"][0]["text"] == "Email is user@example.com"
    assert (
        out["choices"][0]["message"]["content"][1]
        == response["choices"][0]["message"]["content"][1]
    )


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
    """A request that never made it past auth still gets audited — INLINE.

    litellm does NOT fire async_log_failure_event for a pre_call rejection, so the
    auth-failure audit is emitted inline (operators need auth-failure rates). The
    record uses placeholder identity ("unknown") since no state was created.
    """
    g, sink = _build_guardrail()
    data = {"model": "claude", "messages": [], "headers": {}}
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_MISSING_TOKEN"
    # Emitted inline by pre_call — no manual failure-event call needed.
    assert len(sink.records) == 1
    assert sink.records[0]["status"] == "failed"
    assert sink.records[0]["user_id"] == "unknown"
    assert sink.records[0]["error_code"] == "E_MISSING_TOKEN"
    # Idempotency: a follow-up failure event adds no second record.
    start = time.time()
    await g.audit(data, None, start_time=start, end_time=start + 0.05, status="failed")
    assert len(sink.records) == 1


async def test_pre_call_bad_request_audits_inline() -> None:
    """A malformed request (messages not a list) audits inline as E_BAD_REQUEST."""
    g, sink = _build_guardrail()
    data = _data_with_token("tok-1", content="hi")
    data["messages"] = "not-a-list"
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_BAD_REQUEST"
    assert len(sink.records) == 1
    assert sink.records[0]["status"] == "failed"
    assert sink.records[0]["error_code"] == "E_BAD_REQUEST"


# ---- Document block integration tests (A) -----------------------------------


async def test_pre_call_document_block_title_and_text_source_redacted() -> None:
    """document block: title and source.data (text) are redacted; no original in egress."""
    g, _ = _build_guardrail(
        [
            ("alice@corp.example", "[E1]"),
            ("bob@corp.example", "[E2]"),
        ]
    )
    content = [
        {
            "type": "document",
            "title": "Report for alice@corp.example",
            "source": {"type": "text", "data": "Authored by bob@corp.example"},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    serialized = json.dumps(out["messages"][0]["content"])
    assert "alice@corp.example" not in serialized
    assert "bob@corp.example" not in serialized
    assert "[E1]" in serialized or "[E2]" in serialized


async def test_pre_call_document_block_base64_source_untouched() -> None:
    """document block with base64 source must pass through unchanged."""
    b64_data = "SGVsbG8gV29ybGQ="
    g, _ = _build_guardrail([("alice@corp.example", "[E1]")])
    content = [
        {
            "type": "document",
            "title": "alice@corp.example",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64_data},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    blk = out["messages"][0]["content"][0]
    assert blk["source"]["data"] == b64_data
    assert "alice@corp.example" not in blk["title"]


# ---- Deep nesting 400 integration test (C) ----------------------------------


async def test_pre_call_deep_nesting_returns_400_e_bad_request() -> None:
    """A tool_use input nested > _MAX_JSON_DEPTH must return 400 E_BAD_REQUEST."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_returning([]))
    # Build a dict nested 66 levels deep (exceeds _MAX_JSON_DEPTH=64).
    deep: dict = {"v": "leaf"}
    for _ in range(66):
        deep = {"k": deep}
    content = [{"type": "tool_use", "id": "t1", "name": "fn", "input": deep}]
    data = _data_with_token("tok-1", content=content)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 400
    assert ei.value.error_code == "E_BAD_REQUEST"


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


# ---- Adversarial / hardening tests ------------------------------------------


async def test_pre_call_no_leak_original_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """M1-14: original PII must never leak into logs.

    Feed a known secret through the sanitization pipeline and assert
    that the secret string never appears in any log record emitted,
    only redaction counts and byte sizes.
    """
    secret = "alice.smith@corp.internal"
    g, _ = _build_guardrail([(secret, "[EMAIL_001]")])
    data = _data_with_token("tok-1", content=f"Contact: {secret}")

    with caplog.at_level(logging.INFO):
        await g.pre_call(data)

    # Assert the original secret does NOT appear anywhere in captured logs.
    for record in caplog.records:
        msg_text = record.getMessage()
        assert secret not in msg_text, f"LEAK DETECTED: secret '{secret}' found in log: {msg_text}"
    # Verify logs DO contain redaction metadata (byte size, count).
    log_text = caplog.text
    assert "litellm_pre_call_message_sanitize_start" in log_text
    assert "content_bytes=" in log_text
    assert "[EMAIL_001]" not in log_text


async def test_pre_call_no_leak_original_in_system_logs(caplog: pytest.LogCaptureFixture) -> None:
    """M1-14: system field sanitization logs never leak the original."""
    secret_env = "DB_PASSWORD=hunter2secret"
    g, _ = _build_guardrail([(secret_env, "[SECRET_001]")])
    data = _data_with_token("tok-1", content="hello", system=secret_env)

    with caplog.at_level(logging.INFO):
        await g.pre_call(data)

    for record in caplog.records:
        msg = record.getMessage()
        assert secret_env not in msg, f"LEAK in system logs: {msg}"
        assert "hunter2secret" not in msg


async def test_pre_call_empty_list_content_no_crash() -> None:
    """Edge: empty list content (no blocks) should not crash and return empty list."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    content: list[Any] = []
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    assert out["messages"][0]["content"] == []


async def test_pre_call_content_with_only_non_text_blocks() -> None:
    """Edge: list with only non-text blocks (image, tool_use) should not call corp-LLM."""
    # Build a guardrail that would raise if corp-LLM is called.
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    content = [
        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
        {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {}},
    ]
    data = _data_with_token("tok-1", content=content)

    # This should NOT fail because no corp-LLM call is made for non-text blocks.
    out = await g.pre_call(data)
    assert len(out["messages"][0]["content"]) == 2
    assert out["messages"][0]["content"][0]["type"] == "image_url"
    assert out["messages"][0]["content"][1]["type"] == "tool_use"


async def test_pre_call_missing_content_field_no_crash() -> None:
    """Edge: message without content field should not crash."""
    g, _ = _build_guardrail()
    data = {
        "model": "claude",
        "messages": [{"role": "user"}],  # No content field
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data)

    # Should pass through unchanged.
    assert out["messages"][0] == {"role": "user"}


async def test_pre_call_system_empty_str() -> None:
    """Edge: empty system string is skipped (truthy guard) and passed through unchanged."""
    g, _ = _build_guardrail()
    data = _data_with_token("tok-1", content="hello", system="")
    out = await g.pre_call(data)

    # Empty string is falsy → skipped entirely; value is preserved as-is in data.
    assert out.get("system") == ""


async def test_pre_call_system_empty_list() -> None:
    """Edge: empty system list is skipped (truthy guard) and passed through unchanged."""
    g, _ = _build_guardrail()
    data = _data_with_token("tok-1", content="hello", system=[])
    out = await g.pre_call(data)

    # Empty list is falsy → skipped entirely; value is preserved as-is in data.
    assert out.get("system") == []


async def test_pre_call_corp_llm_down_on_system_fails_closed_503(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail-closed (M4): corp-LLM error on system field also raises 503 E_CORP_LLM_DOWN."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    data = _data_with_token("tok-1", content="hello", system="SecretEnv=/prod")

    with caplog.at_level(logging.WARNING), pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)

    assert ei.value.status_code == 503
    assert ei.value.error_code == "E_CORP_LLM_DOWN"
    # Verify the failure was logged.
    assert "litellm_pre_call_corp_llm_failed" in caplog.text


async def test_pre_call_multiple_text_blocks_in_content_all_sanitized() -> None:
    """Multiple text blocks in same message are all sanitized."""
    g, _ = _build_guardrail([("alice", "[N1]"), ("bob", "[N2]")])
    content = [
        {"type": "text", "text": "hello alice"},
        {"type": "image_url", "image_url": {"url": "https://..."}},
        {"type": "text", "text": "goodbye bob"},
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    assert out["messages"][0]["content"][0]["text"] == "hello [N1]"
    assert out["messages"][0]["content"][1]["type"] == "image_url"
    assert out["messages"][0]["content"][2]["text"] == "goodbye [N2]"


async def test_pre_call_deeply_nested_tool_result_blocks_sanitized() -> None:
    """tool_result with nested list content: all text blocks are sanitized recursively."""
    g, _ = _build_guardrail([("secret", "[SECRET_001]")])
    content = [
        {
            "type": "tool_result",
            "content": [
                {"type": "text", "text": "part one: secret"},
                {
                    "type": "tool_result",
                    "content": {"type": "text", "text": "nested: secret"},
                },
            ],
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    # Top-level tool_result content list
    tool_result = out["messages"][0]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert isinstance(tool_result["content"], list)
    # First text block in the list is sanitized
    assert tool_result["content"][0]["text"] == "part one: [SECRET_001]"
    # Nested tool_result (inside the list) — its content is a bare dict text block.
    # Fix A: bare dict is now routed through _sanitize_block, so it IS redacted.
    nested_tool_result = tool_result["content"][1]
    assert nested_tool_result["type"] == "tool_result"
    assert nested_tool_result["content"]["type"] == "text"
    nested_text = nested_tool_result["content"]["text"]
    assert "[SECRET_001]" in nested_text
    assert "secret" not in nested_text


async def test_post_call_unary_both_choices_and_content_ignores_content() -> None:
    """If response has BOTH choices AND top-level content, only choices path runs."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    # Malformed response with both choices and content.
    response = {
        "choices": [{"message": {"content": [{"type": "text", "text": "from choices [N1]"}]}}],
        "content": [{"type": "text", "text": "from top-level [N1]"}],
    }
    out = await g.post_call_unary(data, response)

    # Only choices path is executed (per the if/elif in code).
    assert out["choices"][0]["message"]["content"][0]["text"] == "from choices alice"
    # Top-level content is untouched (may or may not be present in response).


async def test_post_call_unary_non_text_blocks_byte_identical() -> None:
    """Non-text blocks in post_call response are byte-identical to input."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    image_block = {
        "type": "image_url",
        "image_url": {"url": "https://example.com/image.png", "detail": "high"},
    }
    tool_use_block = {
        "type": "tool_use",
        "id": "t1",
        "name": "get_weather",
        "input": {"location": "NYC"},
    }
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "hello [N1]"},
                        image_block,
                        tool_use_block,
                    ]
                }
            }
        ]
    }
    out = await g.post_call_unary(data, response)

    # Non-text blocks must be dict-equal (byte-identical).
    assert out["choices"][0]["message"]["content"][1] == image_block
    assert out["choices"][0]["message"]["content"][2] == tool_use_block


async def test_post_call_unary_placeholder_in_one_block_not_another() -> None:
    """Placeholder that appears in one text block is only reversed in that block."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "first block: [N1]"},
                        {"type": "text", "text": "second block: no placeholder"},
                    ]
                }
            }
        ]
    }
    out = await g.post_call_unary(data, response)

    assert out["choices"][0]["message"]["content"][0]["text"] == "first block: alice"
    assert out["choices"][0]["message"]["content"][1]["text"] == "second block: no placeholder"


async def test_round_trip_list_content_preserves_structure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sanitize list-content message (pre_call), then desanitize response (post_call).

    Original structure must be preserved and content restored exactly.
    """
    g, _ = _build_guardrail([("alice", "[N1]"), ("bob", "[N2]")])
    content = [
        {"type": "text", "text": "hello alice"},
        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
        {"type": "text", "text": "goodbye bob"},
    ]
    data = _data_with_token("tok-1", content=content)

    with caplog.at_level(logging.INFO):
        await g.pre_call(data)

    # Simulate response from upstream.
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "hi [N1]"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
                        {"type": "text", "text": "bye [N2]"},
                    ]
                }
            }
        ]
    }

    out = await g.post_call_unary(data, response)

    # Verify round-trip: structure preserved, originals restored.
    out_content = out["choices"][0]["message"]["content"]
    assert len(out_content) == 3
    assert out_content[0]["type"] == "text"
    assert out_content[0]["text"] == "hi alice"
    assert out_content[1]["type"] == "image_url"
    assert out_content[1] == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/img.png"},
    }
    assert out_content[2]["type"] == "text"
    assert out_content[2]["text"] == "bye bob"


async def test_length_descending_placeholder_substitution_prevents_shadowing() -> None:
    """M1-9: longer placeholders must be reversed before shorter ones.

    If we have [EMAIL_1] and [EMAIL_12], reversing [EMAIL_1] first would
    shadow [EMAIL_12] and produce [EMAIL_12] → incorrect.
    Sort longest first to avoid this.
    """
    g, _ = _build_guardrail(
        [
            ("alice@corp.com", "[EMAIL_1]"),
            ("alice.smith@corp.com", "[EMAIL_12]"),
        ]
    )
    data = _data_with_token("tok-1", content="emails alice@corp.com and alice.smith@corp.com")
    await g.pre_call(data)

    response = {
        "choices": [
            {
                "message": {
                    "content": "[EMAIL_12] and [EMAIL_1]",
                }
            }
        ]
    }
    out = await g.post_call_unary(data, response)

    # Verify BOTH are reversed correctly (not one shadows the other).
    result_text = out["choices"][0]["message"]["content"]
    assert "alice.smith@corp.com" in result_text
    assert "alice@corp.com" in result_text


async def test_pre_call_non_dict_message_items_skipped() -> None:
    """Non-dict items in messages list should be skipped gracefully."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = {
        "model": "claude",
        "messages": [
            {"role": "user", "content": "hi alice"},
            "not a dict",  # Invalid, should be skipped.
            123,
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data)

    # First message sanitized, others untouched.
    assert out["messages"][0]["content"] == "hi [N1]"
    assert out["messages"][1] == "not a dict"
    assert out["messages"][2] == 123


async def test_post_call_unary_anthropic_native_with_multiple_content_types() -> None:
    """Anthropic native response: top-level content list with mixed types."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = {
        "type": "message",
        "content": [
            {"type": "text", "text": "hello [N1]"},
            {"type": "image_url", "image_url": {"url": "https://..."}},
            {"type": "text", "text": "footer [N1]"},
        ],
    }
    out = await g.post_call_unary(data, response)

    assert out["content"][0]["text"] == "hello alice"
    assert out["content"][1] == response["content"][1]
    assert out["content"][2]["text"] == "footer alice"


async def test_pre_call_tool_result_bare_dict_content_sanitized() -> None:
    """Fix A: tool_result whose content is a bare dict text block is redacted.

    The bare-dict path previously fell through to the 'pass through unchanged'
    branch, leaking the original text. The _sanitize_block helper now handles
    it identically to a list-item dict.
    """
    g, _ = _build_guardrail([("secret", "[SECRET_001]")])
    content = [
        {
            "type": "tool_result",
            # content is a bare dict, not a list — the pre-fix blind spot.
            "content": {"type": "text", "text": "the secret value"},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    nested = out["messages"][0]["content"][0]["content"]
    assert nested["type"] == "text"
    assert "[SECRET_001]" in nested["text"]
    assert "secret" not in nested["text"]


async def test_post_call_unary_anthropic_native_top_level_content_str_reversed() -> None:
    """Fix B: Anthropic-native response with top-level content as a STR is reversed.

    Before fix B, the elif branch only matched list; a str top-level content
    with a placeholder would egress un-reversed.
    """
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    # Anthropic-native shape with str content (edge case but valid).
    response = {"type": "message", "content": "Hello [N1], your request was processed."}
    out = await g.post_call_unary(data, response)

    assert out["content"] == "Hello alice, your request was processed."
    assert "[N1]" not in out["content"]


# ---- Placeholder-allocator hardening tests ------------------------------------


async def test_cache_a_hit_segment_plus_fresh_segment_no_collision() -> None:
    """Cache-A hit for one segment + fresh corp-LLM call for another must not collide.

    Scenario (Prompt-4 path):
    - Request 1: system='admin is a@corp.example' → warms Cache A for that text.
    - Request 2 (new request_id): same system (Cache-A hit, returns EMAIL_001)
      + message 'contact b@corp.example' (fresh, corp-LLM also returns EMAIL_001).
    The RequestPlaceholderAllocator must assign distinct canonical labels so both
    originals survive de-sanitization.
    """
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())

    # Warm Cache A for the system text.
    warm_data = _data_with_token(
        "tok-1",
        content="no email here",
        system="admin is a@corp.example",
    )
    await g.pre_call(warm_data)

    # Second request: same system (Cache-A hit) + fresh message email.
    data2 = _data_with_token(
        "tok-1",
        content="contact b@corp.example",
        system="admin is a@corp.example",
    )
    out = await g.pre_call(data2)

    msg_text = out["messages"][0]["content"]
    sys_text = out["system"]

    # Both originals must be redacted.
    assert "a@corp.example" not in sys_text, f"system leaked: {sys_text!r}"
    assert "b@corp.example" not in msg_text, f"message leaked: {msg_text!r}"

    msg_ph = re.search(r"\[EMAIL_\d+\]", msg_text).group(0)
    sys_ph = re.search(r"\[EMAIL_\d+\]", sys_text).group(0)
    assert msg_ph != sys_ph, f"collision: both resolved to {msg_ph!r}"

    # Round-trip: post_call_stream must restore EACH placeholder to its own original.
    chunks_in = [{"choices": [{"delta": {"content": f"msg={msg_ph} sys={sys_ph}"}}]}]
    out_text = ""
    async for chunk in g.post_call_stream(data2, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    assert out_text == "msg=b@corp.example sys=a@corp.example", f"round-trip failed: {out_text!r}"


async def test_oversize_message_leaf_fails_closed_not_leaked() -> None:
    """F1: an oversize message leaf is refused (fail-closed), never forwarded verbatim.

    Replaces the old M1-11 deliver-and-flag behaviour, which egressed the email
    inside an oversize message UNREDACTED — the exact leak F1 closes.
    """
    from corp_llm_gateway.payload import DEFAULT_THRESHOLD_BYTES

    email_in_big_msg = "overflow@corp.example"
    padding = "x" * (DEFAULT_THRESHOLD_BYTES + 1)
    big_content = f"{padding} {email_in_big_msg}"

    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token("tok-1", content=big_content, system="admin is a@corp.example")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_OVERSIZE_BLOCKED"


async def test_pre_call_oversize_message_leaf_repro_blocked() -> None:
    """F1 repro (message leaf): an oversize leaf carrying an sk- key must not egress."""
    secret = "sk-" + "a" * 40
    g, _ = _build_guardrail_oversize(threshold=64)
    big = f"{secret} " + "x" * 200
    data = _data_with_token("tok-1", content=big)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_OVERSIZE_BLOCKED"


async def test_pre_call_oversize_document_source_data_repro_blocked() -> None:
    """F1 repro (document.source.data leaf): oversize document data must not egress."""
    secret = "sk-" + "b" * 40
    g, _ = _build_guardrail_oversize(threshold=64)
    big = f"{secret} " + "y" * 200
    doc = [{"type": "document", "source": {"type": "text", "data": big}}]
    data = _data_with_token("tok-1", content=doc)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_OVERSIZE_BLOCKED"


async def test_pre_call_oversize_message_chunk_policy_sanitizes() -> None:
    """chunk policy at the hook level: an oversize leaf's email is redacted, not leaked."""
    email = "chunky@corp.example"
    g, _ = _build_guardrail_oversize(
        threshold=64, policy="chunk", local_detectors=[RegexChecksumDetector()]
    )
    big = "prefix " + email + " " + "x" * 200
    data = _data_with_token("tok-1", content=big)
    out = await g.pre_call(data)
    body = out["messages"][0]["content"]
    assert email not in body, "chunk policy leaked the email"
    assert re.search(r"\[EMAIL_\d+\]", body), f"email not redacted: {body[:80]!r}"


async def test_oversize_deliver_flag_marks_audit_block_reason() -> None:
    """M1: a deliver-flag egress is marked oversize:delivered in the audit record."""
    g, sink = _build_guardrail_oversize(
        threshold=64, policy="deliver-flag", deliver_teams=frozenset({"t1"})
    )
    clean = "the quick brown fox jumps over the lazy dog and keeps running along here"
    assert len(clean.encode("utf-8")) > 64
    data = _data_with_token("tok-1", content=clean)
    out = await g.pre_call(data)
    assert out["messages"][0]["content"] == clean, "clean oversize leaf must be delivered"
    start = time.time()
    await g.async_log_success_event(
        kwargs={"data": data},
        response_obj={"choices": [{"message": {"content": "ok"}}]},
        start_time=start,
        end_time=start + 0.100,
    )
    assert len(sink.records) == 1
    assert sink.records[0]["block_reason"] == "oversize:delivered"


async def test_normal_request_audit_has_no_block_reason() -> None:
    """M1 control: a normal zero/non-zero-redaction request carries no marker."""
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
    assert "block_reason" not in sink.records[0]


async def test_corp_llm_fails_on_second_segment_fails_closed_503() -> None:
    """M4 fail-closed: if corp-LLM succeeds on segment 1 then dies on segment 2,
    pre_call must raise GuardrailHttpException 503 E_CORP_LLM_DOWN.

    Partially-sanitized content must never egress; the request is rejected
    before any mutated data reaches the upstream LLM.
    """
    call_count = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First segment succeeds.
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
                                                        {
                                                            "original": "a@corp.example",
                                                            "replacement": "[EMAIL_001]",
                                                        }
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
        # Second segment times out.
        raise httpx.ConnectTimeout("", request=request)

    from corp_llm_gateway.corp_llm import CorpLlmClient

    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    flaky_corp_llm = CorpLlmClient("https://corp-llm.example", model="m", http=http)

    g, _ = _build_guardrail(corp_llm=flaky_corp_llm)
    # Two distinct emails in two segments: message + system.
    data = _data_with_token(
        "tok-1",
        content="message from a@corp.example",
        system="system has b@corp.example",
    )

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)

    assert ei.value.status_code == 503, f"expected 503, got {ei.value.status_code}"
    assert ei.value.error_code == "E_CORP_LLM_DOWN", (
        f"expected E_CORP_LLM_DOWN, got {ei.value.error_code!r}"
    )


async def test_pre_call_logs_sanitize_done_per_block(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each text block sanitized logs a sanitize_done entry with redaction count."""
    g, _ = _build_guardrail([("alice", "[N1]"), ("bob", "[N2]")])
    content = [
        {"type": "text", "text": "hi alice"},
        {"type": "text", "text": "bye bob"},
    ]
    data = _data_with_token("tok-1", content=content)

    with caplog.at_level(logging.INFO):
        await g.pre_call(data)

    # Verify the request completes and sanitization logs are emitted.
    assert data["messages"][0]["content"][0]["text"] == "hi [N1]"
    assert data["messages"][0]["content"][1]["text"] == "bye [N2]"
    # Verify log output contains sanitization info
    assert "litellm_pre_call_message_sanitize_done" in caplog.text
    assert "redaction_count" in caplog.text


# ---- New adversarial tests (tasks 1-4) ----------------------------------------


async def test_nested_tool_result_list_collision_split_and_restored() -> None:
    """Task 1: two text blocks inside a tool_result's list content carry
    different emails; both come back [EMAIL_001] from per-segment corp-LLM
    calls. The allocator must split them to distinct tokens, and
    post_call_stream must restore both originals."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    content = [
        {
            "type": "tool_result",
            "content": [
                {"type": "text", "text": "first a@corp.example"},
                {"type": "text", "text": "second b@corp.example"},
            ],
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    nested = out["messages"][0]["content"][0]["content"]
    ph0 = re.search(r"\[EMAIL_\d+\]", nested[0]["text"]).group(0)
    ph1 = re.search(r"\[EMAIL_\d+\]", nested[1]["text"]).group(0)
    assert ph0 != ph1, f"collision: both got {ph0!r}"

    # Originals must be redacted from each block.
    assert "a@corp.example" not in nested[0]["text"]
    assert "b@corp.example" not in nested[1]["text"]

    # post_call_stream must restore both.
    chunks_in = [{"choices": [{"delta": {"content": f"first={ph0} second={ph1}"}}]}]
    out_text = ""
    async for chunk in g.post_call_stream(data, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    assert out_text == "first=a@corp.example second=b@corp.example", (
        f"round-trip failed: {out_text!r}"
    )


async def test_openai_gpt4o_multimodal_text_parts_collision_split_image_passthrough() -> None:
    """Task 2: gpt-4o multimodal content with two text parts and one image_url.
    The two text parts carry different emails that both come back [EMAIL_001];
    the allocator must split them. The image_url block must be byte-identical
    on egress. Both emails must restore via post_call_stream."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    image_block = {"type": "image_url", "image_url": {"url": "http://img.example/x.png"}}
    content = [
        {"type": "text", "text": "a@corp.example"},
        image_block,
        {"type": "text", "text": "b@corp.example"},
    ]
    data = _data_with_token("tok-1", content=content, model="gpt-4o")
    out = await g.pre_call(data)

    out_blocks = out["messages"][0]["content"]
    assert len(out_blocks) == 3

    ph0 = re.search(r"\[EMAIL_\d+\]", out_blocks[0]["text"]).group(0)
    ph1 = re.search(r"\[EMAIL_\d+\]", out_blocks[2]["text"]).group(0)
    assert ph0 != ph1, f"collision: both text parts got {ph0!r}"

    # image_url block must be byte-identical (unchanged dict).
    assert out_blocks[1] == image_block

    # Both emails must restore via post_call_stream.
    chunks_in = [{"choices": [{"delta": {"content": f"{ph0} / {ph1}"}}]}]
    out_text = ""
    async for chunk in g.post_call_stream(data, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    assert out_text == "a@corp.example / b@corp.example", f"round-trip failed: {out_text!r}"


async def test_substring_originals_longer_replaced_first_no_corruption() -> None:
    """Task 3 / M1-9: when one original is a substring of another, the longer
    one must be replaced first on the forward pass so the shorter one doesn't
    partially corrupt the longer original. Same guard applies to the reverse
    pass (length-descending sort on placeholders)."""
    g, _ = _build_guardrail(
        [
            ("john.doe@corp.example", "[EMAIL_001]"),
            ("john", "[NAME_001]"),
        ]
    )
    data = _data_with_token("tok-1", content="contact john.doe@corp.example or just john")
    out = await g.pre_call(data)

    sanitized = out["messages"][0]["content"]
    assert sanitized == "contact [EMAIL_001] or just [NAME_001]", (
        f"forward substitution corrupted: {sanitized!r}"
    )

    # Reverse: post_call_stream must restore both without one shadowing the other.
    chunks_in = [{"choices": [{"delta": {"content": "[EMAIL_001] / [NAME_001]"}}]}]
    out_text = ""
    async for chunk in g.post_call_stream(data, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    assert out_text == "john.doe@corp.example / john", (
        f"reverse substitution corrupted: {out_text!r}"
    )


async def test_user_typed_placeholder_literal_preserved_not_collided(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Security fix: a user-typed [FAMILY_NNN] literal must not collide with a real redaction.

    The real email a@corp.example must be assigned a DIFFERENT token (e.g.
    [EMAIL_002]), because [EMAIL_001] is already present verbatim in the input.
    On the reverse pass, [EMAIL_001] is NOT in the mapping, so it stays unchanged
    in the response (the user's literal is preserved). The real redaction token
    ([EMAIL_002]) IS in the mapping and restores correctly.
    """
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1", content="My email a@corp.example and the marker [EMAIL_001] in docs"
    )

    with caplog.at_level(logging.WARNING):
        out = await g.pre_call(data)

    sanitized = out["messages"][0]["content"]

    # The real email must be redacted to a token OTHER than [EMAIL_001].
    assert "a@corp.example" not in sanitized, f"email not redacted: {sanitized!r}"
    real_ph = re.search(r"\[EMAIL_\d+\]", sanitized.replace("[EMAIL_001]", ""))
    assert real_ph is not None, f"no redaction token found: {sanitized!r}"
    real_token = real_ph.group(0)
    assert real_token != "[EMAIL_001]", (
        "collision: real email got [EMAIL_001] despite user typing it verbatim"
    )

    # The user's literal "[EMAIL_001]" still appears verbatim in egress.
    assert "[EMAIL_001]" in sanitized, f"user literal was removed: {sanitized!r}"

    # On post_call_stream: a response with both tokens restores correctly.
    chunks_in = [{"choices": [{"delta": {"content": f"{real_token} and [EMAIL_001]"}}]}]
    out_text = ""
    async for chunk in g.post_call_stream(data, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    # The real token is restored; the user's literal is unchanged (not in mapping).
    assert "a@corp.example" in out_text, f"real email not restored: {out_text!r}"
    assert "[EMAIL_001]" in out_text, f"user literal was reversed: {out_text!r}"

    # Breadcrumb: warning was emitted with count=1 and contains NO email content.
    breadcrumb_lines = [
        r.getMessage()
        for r in caplog.records
        if "input_placeholder_literal_detected" in r.getMessage()
    ]
    assert breadcrumb_lines, "breadcrumb warning not emitted"
    assert "count=1" in breadcrumb_lines[0], f"expected count=1 in: {breadcrumb_lines[0]!r}"
    assert "a@corp.example" not in breadcrumb_lines[0], "email leaked into breadcrumb log"
    assert "[EMAIL_001]" not in breadcrumb_lines[0], "literal leaked into breadcrumb log"


# ---- Audit distinct-secret + finding_label_counts tests ----------------------


async def test_audit_same_email_two_segments_redaction_count_one() -> None:
    """Same email in system + message must produce redaction_count==1, one placeholder."""
    g, sink = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content="contact a@corp.example",
        system="a@corp.example is admin",
    )
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")

    rec = sink.records[0]
    assert rec["redaction_count"] == 1
    assert rec["placeholder_list"] == ["[EMAIL_001]"]
    assert rec["finding_label_counts"] == {"EMAIL": 1}


async def test_audit_two_different_emails_redaction_count_two() -> None:
    """Two DIFFERENT emails (system + message) -> redaction_count==2, two placeholders."""
    g, sink = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content="contact b@corp.example",
        system="a@corp.example is admin",
    )
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")

    rec = sink.records[0]
    assert rec["redaction_count"] == 2
    assert rec["finding_label_counts"] == {"EMAIL": 2}
    assert len(rec["placeholder_list"]) == 2
    assert len(set(rec["placeholder_list"])) == 2


async def test_audit_mixed_families_label_counts() -> None:
    """Mixed families: EMAIL + API_KEY -> finding_label_counts has both, redaction_count==2."""
    g, sink = _build_guardrail([("a@x.com", "[EMAIL_001]"), ("SEKRET", "[API_KEY_001]")])
    data = _data_with_token("tok-1", content="mail a@x.com key SEKRET")
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")

    rec = sink.records[0]
    assert rec["finding_label_counts"] == {"EMAIL": 1, "API_KEY": 1}
    assert rec["redaction_count"] == 2


async def test_audit_invariant_sum_equals_redaction_count_equals_placeholder_len() -> None:
    """sum(finding_label_counts.values()) == redaction_count == len(placeholder_list)."""
    g, sink = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content="contact b@corp.example",
        system="a@corp.example is admin",
    )
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")

    rec = sink.records[0]
    assert sum(rec["finding_label_counts"].values()) == rec["redaction_count"]
    assert rec["redaction_count"] == len(rec["placeholder_list"])


async def test_audit_finding_label_counts_keys_never_contain_originals() -> None:
    """M1-14: finding_label_counts keys must be category labels, never originals.

    Pins that _label_counts uses placeholder families (e.g. 'EMAIL'), not
    the original PII values ('bob@corp.example' or the local-part 'bob').
    """
    g, sink = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token("tok-1", content="mail bob@corp.example")
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")

    rec = sink.records[0]
    flc = rec["finding_label_counts"]

    # (a) keys are category labels, not originals
    assert "EMAIL" in flc, f"expected 'EMAIL' key, got {flc!r}"

    # (b) neither the full address nor the local-part appears anywhere in keys or repr
    flc_repr = repr(flc)
    assert "bob@corp.example" not in flc_repr, (
        f"original leaked into finding_label_counts: {flc_repr!r}"
    )
    assert "bob" not in flc_repr, f"local-part leaked into finding_label_counts: {flc_repr!r}"


# ---- tool_use.input sanitization (M2 fix) ------------------------------------


async def test_tool_use_input_round_trip_distinct_emails_restored() -> None:
    """M2: tool_use.input PII is redacted on egress and restored on unary reverse.

    A message with a tool_use block whose input carries two distinct emails.
    After pre_call: no original email appears in input values; the two emails
    get distinct tokens (allocator collision-split). After post_call_unary with
    a response whose tool_use input echoes those placeholders, both originals
    are restored.
    """
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "send",
            "input": {"to": "a@corp.example", "cc": ["b@corp.example"]},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    tool_block = out["messages"][0]["content"][0]
    assert tool_block["type"] == "tool_use"
    to_val = tool_block["input"]["to"]
    cc_val = tool_block["input"]["cc"][0]

    # Originals must not appear in egress.
    assert "a@corp.example" not in to_val, f"to leaked: {to_val!r}"
    assert "b@corp.example" not in cc_val, f"cc leaked: {cc_val!r}"

    # Two distinct emails must get distinct tokens.
    assert to_val != cc_val, f"collision: both got {to_val!r}"

    # post_call_unary must restore both placeholders in a tool_use response block.
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "send",
                            "input": {"to": to_val, "cc": [cc_val]},
                        }
                    ]
                }
            }
        ]
    }
    result = await g.post_call_unary(data, response)
    restored = result["choices"][0]["message"]["content"][0]["input"]
    assert restored["to"] == "a@corp.example", f"to not restored: {restored['to']!r}"
    assert restored["cc"] == ["b@corp.example"], f"cc not restored: {restored['cc']!r}"


async def test_tool_use_input_nested_dict_in_dict_sanitized() -> None:
    """Nested dict-in-dict: all string leaves are sanitized; non-str scalars unchanged."""
    g, _ = _build_guardrail([("secret", "[SEC_001]")])
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {
                "outer": {"inner": "secret"},
                "count": 42,
                "active": True,
                "note": None,
            },
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    inp = out["messages"][0]["content"][0]["input"]
    assert inp["outer"]["inner"] == "[SEC_001]"
    assert inp["count"] == 42
    assert inp["active"] is True
    assert inp["note"] is None


async def test_tool_use_input_list_of_strings_sanitized() -> None:
    """List of strings inside tool_use.input: every element is sanitized."""
    g, _ = _build_guardrail([("secret", "[SEC_001]")])
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"items": ["secret", "harmless", "secret"]},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    assert out["messages"][0]["content"][0]["input"]["items"] == [
        "[SEC_001]",
        "harmless",
        "[SEC_001]",
    ]


async def test_tool_use_input_dict_keys_not_altered() -> None:
    """Dict keys in tool_use.input must NOT be sanitized — only values.

    The key name "to" is a common English word; even if the corp-LLM were to
    redact it, we verify it stays unchanged. The value carries the PII email.
    """
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"to": "addr@corp.example"},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    inp = out["messages"][0]["content"][0]["input"]
    # Key "to" must survive unchanged.
    assert "to" in inp, f"key was altered: {inp!r}"
    # Value must be sanitized.
    assert "addr@corp.example" not in inp["to"], f"value not sanitized: {inp['to']!r}"


async def test_tool_use_input_no_pii_passes_through() -> None:
    """tool_use.input with no PII: block structure preserved, no corp-LLM call for values."""
    g, _ = _build_guardrail([("secret", "[SEC_001]")])
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "get_weather",
            "input": {"city": "Moscow", "units": "metric"},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    inp = out["messages"][0]["content"][0]["input"]
    assert inp["city"] == "Moscow"
    assert inp["units"] == "metric"


async def test_tool_use_input_image_block_still_passes_through() -> None:
    """image_url block alongside tool_use is still unchanged (regression)."""
    g, _ = _build_guardrail([("secret", "[SEC_001]")])
    image_block = {"type": "image_url", "image_url": {"url": "https://img.example/x.png"}}
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"note": "secret"},
        },
        image_block,
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    assert out["messages"][0]["content"][1] == image_block
    assert out["messages"][0]["content"][0]["input"]["note"] == "[SEC_001]"


async def test_tool_use_input_no_leak_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """M1-14: PII inside tool_use.input must not appear in any log record."""
    secret = "tool-secret@corp.internal"
    g, _ = _build_guardrail([(secret, "[EMAIL_001]")])
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"to": secret},
        }
    ]
    data = _data_with_token("tok-1", content=content)

    with caplog.at_level(logging.INFO):
        await g.pre_call(data)

    for record in caplog.records:
        msg = record.getMessage()
        assert secret not in msg, f"LEAK in logs: {msg!r}"


# ---- Stage-0 payload classifier (DP-6) ------------------------------------


def _build_guardrail_with_unreachable_upstream(
    valid_token: str = "tok-1",
) -> tuple[CorpLlmGuardrail, ListSink]:
    """Guardrail whose corp-LLM transport raises; any upstream call → immediate failure.

    Used to prove the orchestrator was NOT called for blocked requests.
    """
    return _build_guardrail(corp_llm=_corp_llm_unreachable(), valid_token=valid_token)


async def test_stage0_env_payload_raises_policy_blocked() -> None:
    """.env content is blocked before the sanitizer / upstream is called."""
    g, _ = _build_guardrail_with_unreachable_upstream()
    env_content = (
        "DATABASE_URL=postgres://admin:hunter2@db.corp.lan:5432/prod\n"
        "SECRET_KEY=supersecretvalue-abc123\n"
        "DEBUG=False\n"
        "REDIS_URL=redis://cache.corp.lan:6379/0\n"
        "ALLOWED_HOSTS=*.corp.lan\n"
    )
    data = _data_with_token("tok-1", content=env_content)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_POLICY_BLOCKED"


async def test_stage0_kube_payload_raises_policy_blocked() -> None:
    """Kubernetes manifest is blocked before egress."""
    g, _ = _build_guardrail_with_unreachable_upstream()
    kube_content = (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: my-app\n"
        "  namespace: production\n"
        "spec:\n"
        "  replicas: 3\n"
        "  selector:\n"
        "    matchLabels:\n"
        "      app: my-app\n"
    )
    data = _data_with_token("tok-1", content=kube_content)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_POLICY_BLOCKED"


async def test_stage0_log_dump_raises_policy_blocked() -> None:
    """Application log dump is blocked before egress."""
    g, _ = _build_guardrail_with_unreachable_upstream()
    log_lines = "\n".join(
        [
            "2024-01-15 10:00:01 INFO  Starting application server",
            "2024-01-15 10:00:02 INFO  Loaded configuration from /etc/app.conf",
            "2024-01-15 10:00:03 DEBUG Database pool initialized connections=10",
            "2024-01-15 10:00:15 INFO  Listening on 0.0.0.0:8080",
            "2024-01-15 10:01:22 ERROR Failed to connect to cache host=redis port=6379",
            "2024-01-15 10:01:23 WARN  Retrying connection attempt=1",
            "2024-01-15 10:01:25 WARN  Retrying connection attempt=2",
            "2024-01-15 10:01:30 ERROR Max retries exceeded",
        ]
    )
    data = _data_with_token("tok-1", content=log_lines)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_POLICY_BLOCKED"


async def test_stage0_upstream_not_called_for_blocked_request() -> None:
    """The orchestrator/upstream is NOT invoked for a Stage-0 blocked request.

    The guardrail uses _corp_llm_unreachable() — if sanitize() were called,
    the transport raises ConnectTimeout → E_CORP_LLM_DOWN, not E_POLICY_BLOCKED.
    Seeing E_POLICY_BLOCKED proves the upstream path was never reached.
    """
    g, _ = _build_guardrail_with_unreachable_upstream()
    env_content = (
        "DATABASE_URL=postgres://user:secret@db.lan/prod\n"
        "SECRET_KEY=abc123-secret-value\n"
        "DEBUG=0\n"
        "LOG_LEVEL=WARNING\n"
        "MAX_WORKERS=4\n"
    )
    data = _data_with_token("tok-1", content=env_content)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    # E_POLICY_BLOCKED proves we never reached the unreachable upstream.
    assert ei.value.error_code == "E_POLICY_BLOCKED"


async def test_stage0_audit_record_emitted_with_block_reason() -> None:
    """A Stage-0 block must produce an audit record carrying block_reason."""
    g, sink = _build_guardrail()
    env_content = (
        "DATABASE_URL=postgres://admin:pass@db.corp.lan/prod\n"
        "SECRET_KEY=supersecretvalue\n"
        "DEBUG=False\n"
        "REDIS_URL=redis://cache.corp.lan\n"
        "LOG_LEVEL=ERROR\n"
    )
    data = _data_with_token("tok-1", content=env_content)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_POLICY_BLOCKED"
    # litellm does NOT fire async_log_failure_event for a pre_call rejection, so
    # the block is audited INLINE — exactly one record right after pre_call.
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec.get("block_reason") == "config:env"
    assert rec.get("status") == "failed"
    assert rec.get("error_code") == "E_POLICY_BLOCKED"
    # Attribution fields must be real (not "unknown") because state was created before Stage 0.
    assert rec.get("user_id") == "alice"
    assert rec.get("team_id") == "t1"
    # Idempotency: if a future litellm ALSO fired the failure event, no 2nd record.
    _start = time.time()
    await g.audit(data, None, start_time=_start, end_time=_start + 0.05, status="failed")
    assert len(sink.records) == 1


async def test_stage0_clean_request_passes_through() -> None:
    """A clean prose message is NOT blocked by Stage 0."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="Please help me debug my Python function.")
    # Should not raise — corp LLM is a no-op mock returning no pairs.
    out = await g.pre_call(data)
    assert out["messages"][0]["content"] == "Please help me debug my Python function."


async def test_stage0_exception_message_is_generic() -> None:
    """The GuardrailHttpException message must NOT contain any raw payload content."""
    g, _ = _build_guardrail_with_unreachable_upstream()
    secret_content = "DATABASE_URL=postgres://admin:hunter2@db.corp.lan/prod\n" * 3 + (
        "SECRET_KEY=sk-very-secret\nDEBUG=0\nREDIS_URL=redis://cache\n"
    )
    data = _data_with_token("tok-1", content=secret_content)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    exc_msg = str(ei.value)
    assert "hunter2" not in exc_msg
    assert "sk-very-secret" not in exc_msg
    assert "admin" not in exc_msg


async def test_stage0_disabled_by_flag_allows_through() -> None:
    """When CORP_LLM_BLOCK_PAYLOADS=0 the classifier is skipped entirely."""
    import os

    from corp_llm_gateway import config as _cfg_module

    env_content = (
        "DATABASE_URL=postgres://admin:pass@db/prod\n"
        "SECRET_KEY=abc123\nDEBUG=False\nREDIS_URL=redis://cache\nLOG_LEVEL=ERROR\n"
    )
    os.environ["CORP_LLM_BLOCK_PAYLOADS"] = "0"
    _cfg_module.reset_cache()
    try:
        g, _ = _build_guardrail([])  # corp-LLM returns no pairs
        data = _data_with_token("tok-1", content=env_content)
        # Should NOT raise — flag disabled the classifier
        out = await g.pre_call(data)
        assert out["messages"][0]["content"] == env_content
    finally:
        del os.environ["CORP_LLM_BLOCK_PAYLOADS"]
        _cfg_module.reset_cache()


# ---- Stage 5 DLP egress guard -----------------------------------------------


def _build_guardrail_with_dlp(
    canary: str,
    *,
    corp_llm_pairs: list[tuple[str, str]] | None = None,
    valid_token: str = "tok-1",
) -> tuple[CorpLlmGuardrail, ListSink]:
    """Guardrail with a DLP guard seeded with *canary*; no secret_rescan."""
    from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard

    corp_llm_pairs = corp_llm_pairs if corp_llm_pairs is not None else []
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
        _corp_llm_returning(corp_llm_pairs),
        InMemoryMappingStore(),
        _StaticRules(),
    )
    sink = ListSink()
    audit_logger = AuditLogger(sink, gateway_version="0.0.1")
    dlp = DlpEgressGuard(canary_patterns=[canary], secret_rescan=False)
    return CorpLlmGuardrail(orch, auth, audit_logger, dlp_guard=dlp), sink


async def test_stage5_dlp_blocks_canary_survivor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stage 5 blocks a canary that the primary sanitizer did not redact."""
    canary = "DLP-CANARY-RAW-99999"
    g, _ = _build_guardrail_with_dlp(canary, corp_llm_pairs=[])
    data = _data_with_token("tok-1", content=f"here is {canary}")
    with caplog.at_level(logging.INFO), pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_DLP_BLOCKED"
    assert "litellm_egress_blocked" in caplog.text
    assert "dlp:canary" in caplog.text


async def test_stage5_dlp_clean_request_passes_through() -> None:
    """A request without the canary passes Stage 5 and returns data."""
    canary = "DLP-CANARY-RAW-99999"
    g, _ = _build_guardrail_with_dlp(canary, corp_llm_pairs=[])
    data = _data_with_token("tok-1", content="ordinary request without canary")
    out = await g.pre_call(data)
    assert out["messages"][0]["content"] == "ordinary request without canary"


async def test_stage5_dlp_audit_has_block_reason_dlp_canary() -> None:
    """The failure-event audit after Stage-5 block carries block_reason='dlp:canary'."""
    canary = "DLP-CANARY-RAW-99999"
    g, sink = _build_guardrail_with_dlp(canary, corp_llm_pairs=[])
    now = datetime.now(UTC)
    data = _data_with_token("tok-1", content=f"leaked {canary} here")
    with pytest.raises(GuardrailHttpException):
        await g.pre_call(data)
    # The Stage-5 block audits INLINE (litellm doesn't fire the failure event for
    # a pre_call rejection) — exactly one record right after pre_call.
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec.get("block_reason") == "dlp:canary"
    assert rec.get("error_code") == "E_DLP_BLOCKED"
    assert rec.get("status") == "failed"
    # Raw canary value must NOT appear in any audit field.
    assert canary not in json.dumps(rec)
    # Idempotency: a follow-up failure-event audit adds no second record.
    await g.audit(data, None, start_time=now, end_time=now, status="failed")
    assert len(sink.records) == 1


async def test_stage5_dlp_raw_secret_blocked_by_default_guard() -> None:
    """A raw OpenAI API key that survived the primary sanitizer is blocked by Stage 5."""
    raw_key = "sk-" + "a" * 48
    # Default DlpEgressGuard (secret_rescan=True) — no canaries needed.
    g, _ = _build_guardrail(pairs=[])
    data = _data_with_token("tok-1", content=f"my key is {raw_key}")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_DLP_BLOCKED"


async def test_stage5_dlp_disabled_by_flag_passes_through() -> None:
    """When CORP_LLM_DLP_GUARD=0 Stage 5 is skipped entirely."""
    import os

    from corp_llm_gateway import config as _cfg_module

    canary = "DLP-CANARY-RAW-99999"
    os.environ["CORP_LLM_DLP_GUARD"] = "0"
    _cfg_module.reset_cache()
    try:
        g, _ = _build_guardrail_with_dlp(canary, corp_llm_pairs=[])
        data = _data_with_token("tok-1", content=f"here is {canary}")
        out = await g.pre_call(data)
        assert canary in out["messages"][0]["content"]
    finally:
        del os.environ["CORP_LLM_DLP_GUARD"]
        _cfg_module.reset_cache()
