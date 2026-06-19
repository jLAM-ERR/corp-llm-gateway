"""Adversarial / edge-case tests for CorpLlmGuardrail.post_call_stream
with Anthropic SSE bytes.

Focus: the SSE bytes path (bytes|str chunks), split placeholders across
deltas, framing integrity, and passthrough correctness.  Dict-chunk path
is covered by test_litellm_hook.py; we only supplement here.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
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
    ANTHROPIC_SSE_FIXTURE,
    _cb_start,
    _cb_stop,
    _delta,
)

# ---------------------------------------------------------------------------
# harness (mirrors test_litellm_hook.py without re-importing private helpers)
# ---------------------------------------------------------------------------


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


def _build(pairs: list[tuple[str, str]] | None = None) -> CorpLlmGuardrail:
    pairs = pairs or []
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
    auth = AuthMiddleware(token_store)
    orch = SanitizationOrchestrator(
        _corp_llm_returning(pairs),
        InMemoryMappingStore(),
        _StaticRules(),
    )
    sink = ListSink()
    audit_logger = AuditLogger(sink, gateway_version="0.0.1")
    return CorpLlmGuardrail(orch, auth, audit_logger)


def _request(token: str = "tok-1", content: str = "hello") -> dict[str, Any]:
    return {
        "model": "claude",
        "messages": [{"role": "user", "content": content}],
        "headers": {"X-Corp-Auth": token, "Authorization": "Bearer byok"},
    }


async def _iter(items: list[Any]) -> AsyncIterator[Any]:
    for it in items:
        yield it


async def _collect(g: CorpLlmGuardrail, data: dict, chunks: list[Any]) -> list[Any]:
    out: list[Any] = []
    async for chunk in g.post_call_stream(data, _iter(chunks)):
        out.append(chunk)
    return out


# ---------------------------------------------------------------------------
# 9. post_call_stream level — Anthropic SSE bytes path
# ---------------------------------------------------------------------------


async def test_post_call_stream_sse_bytes_framing_intact_all_json() -> None:
    """Every data: line in the output parses as valid JSON (framing integrity)."""
    g = _build([("user@example.com", "[EMAIL_001]")])
    data = _request(content="send to user@example.com")
    await g.pre_call(data)

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
    out = await _collect(g, data, sse_events)
    for chunk in out:
        assert isinstance(chunk, bytes), f"expected bytes, got {type(chunk)}"
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].lstrip()
            if payload == "[DONE]":
                continue
            try:
                json.loads(payload)
            except json.JSONDecodeError as exc:
                pytest.fail(f"data: line not valid JSON: {payload!r} — {exc}")


async def test_post_call_stream_sse_bytes_placeholder_restored_and_no_leak() -> None:
    """Original is reconstructed from split deltas; placeholder must not appear in output."""
    g = _build([("user@example.com", "[EMAIL_001]")])
    data = _request(content="send to user@example.com")
    await g.pre_call(data)

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
    out = await _collect(g, data, sse_events)
    text_parts: list[str] = []
    for chunk in out:
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].lstrip())
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "content_block_delta":
                delta = obj.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_parts.append(delta["text"])
    full = "".join(text_parts)
    assert "user@example.com" in full, f"original not restored: {full!r}"
    assert "[EMAIL_001]" not in full, f"placeholder leaked: {full!r}"


async def test_post_call_stream_sse_bytes_message_stop_present() -> None:
    """message_stop must be present in the output."""
    g = _build([("user@example.com", "[EMAIL_001]")])
    data = _request(content="send to user@example.com")
    await g.pre_call(data)

    out = await _collect(g, data, list(ANTHROPIC_SSE_FIXTURE))
    types_seen: set[str] = set()
    for chunk in out:
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if line.startswith("data:"):
                try:
                    obj = json.loads(line[5:].lstrip())
                    t = obj.get("type")
                    if t:
                        types_seen.add(t)
                except json.JSONDecodeError:
                    pass
    assert "message_stop" in types_seen
    assert "content_block_stop" in types_seen


async def test_post_call_stream_empty_mapping_sse_bytes_passthrough() -> None:
    """With an empty mapping, SSE bytes pass through byte-identical."""
    g = _build([])
    data = _request(content="no PII here")
    await g.pre_call(data)

    events: list[bytes] = [_MSG_START, _cb_start(), _delta("hello world"), _cb_stop(), _MSG_STOP]
    out = await _collect(g, data, events)
    for ev in events:
        assert ev in out, f"event not byte-identical in output: {ev!r}"


async def test_post_call_stream_malformed_data_line_does_not_raise() -> None:
    """SSE bytes with a non-JSON data: line must not raise in post_call_stream."""
    g = _build([("alice", "[N1]")])
    data = _request(content="hi alice")
    await g.pre_call(data)

    bad_event = b"event: content_block_delta\ndata: NOT JSON\n\n"
    out = await _collect(g, data, [bad_event])
    # Must not raise; must produce output.
    assert isinstance(out, list)


async def test_post_call_stream_done_sentinel_passes_through() -> None:
    """data: [DONE] must appear in the output and must not raise."""
    g = _build([("alice", "[N1]")])
    data = _request(content="hi alice")
    await g.pre_call(data)

    done_event = b"data: [DONE]\n\n"
    out = await _collect(g, data, [done_event])
    combined = b"".join(out)
    assert b"[DONE]" in combined


async def test_post_call_stream_str_chunks_return_str() -> None:
    """When SSE events are str, the output must also be str."""
    g = _build([("alice", "[N1]")])
    data = _request(content="hi alice")
    await g.pre_call(data)

    str_events = [
        _cb_start().decode(),
        _delta("[N1]").decode(),
        _cb_stop().decode(),
    ]
    out = await _collect(g, data, str_events)
    for chunk in out:
        assert isinstance(chunk, str), f"expected str, got {type(chunk)}: {chunk!r}"


async def test_post_call_stream_str_chunks_placeholder_restored() -> None:
    """str SSE events: placeholder is restored in str output.

    The StreamingDesanitizer hold-back may split 'alice' across two
    content_block_delta events; reconstruct from delta.text fields.
    """
    g = _build([("alice", "[N1]")])
    data = _request(content="hi alice")
    await g.pre_call(data)

    str_events = [
        _cb_start().decode(),
        _delta("[N1]").decode(),
        _cb_stop().decode(),
    ]
    out = await _collect(g, data, str_events)
    text_parts: list[str] = []
    for chunk in out:
        for line in chunk.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].lstrip())
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "content_block_delta":
                delta = obj.get("delta", {})
                if isinstance(delta.get("text"), str):
                    text_parts.append(delta["text"])
    restored = "".join(text_parts)
    assert "alice" in restored, f"placeholder not restored: {restored!r}"
    assert "[N1]" not in restored


async def test_post_call_stream_mixed_bytes_and_dict_chunks() -> None:
    """A stream that mixes bytes SSE and dict chunks must not crash.

    In practice litellm does not mix them, but the production code has both
    paths in the same loop — they must not interfere.
    """
    g = _build([("alice", "[N1]")])
    data = _request(content="hi alice")
    await g.pre_call(data)

    chunks: list[Any] = [
        _MSG_START,  # bytes SSE
        {"choices": [{"delta": {"content": "from dict"}}]},  # dict
        _PING,  # bytes SSE
    ]
    # Must not raise.
    out = await _collect(g, data, chunks)
    assert isinstance(out, list)


async def test_post_call_stream_unknown_type_chunk_passes_through() -> None:
    """Chunks of an unknown type (not bytes, str, or dict) pass through unchanged."""
    g = _build([("alice", "[N1]")])
    data = _request(content="hi alice")
    await g.pre_call(data)

    sentinel = object()
    out = await _collect(g, data, [sentinel])
    assert sentinel in out


async def test_post_call_stream_no_pre_call_passthrough() -> None:
    """Without a preceding pre_call (unknown request_id), SSE bytes pass through."""
    g = _build([("alice", "[N1]")])
    data = {"model": "claude", "_corp_gateway_request_id": "unknown-id-xyz"}
    events: list[bytes] = [_MSG_STOP]
    out = await _collect(g, data, events)
    assert _MSG_STOP in out
