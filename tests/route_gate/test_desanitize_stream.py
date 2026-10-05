"""The response reversal on a stream: chat chunks, Anthropic SSE and Responses events
restored by the desanitiser (through `tests/response_restore.py`), upstream vs own-bug errors."""

import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
from corp_llm_gateway.rules import Rule, Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo
from tests.hook_fixtures import (
    _anthropic_stream_through_the_callback,
    _async_iter,
    _build_guardrail,
    _corp_llm_email_per_segment,
    _data_with_token,
    _sse_events,
    _tc_delta_chunk,
    _terminal,
    _text_deltas,
)
from tests.response_restore import restore_stream
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


async def test_post_call_stream_upstream_error_propagates_unconverted() -> None:
    """Major: a failure fetching the NEXT chunk from the upstream iterator
    (e.g. a mid-stream `httpx.RemoteProtocolError`) is not our bug — it must
    reach litellm untouched, not get relabelled E_INTERNAL and swallowed as
    an unrecoverable, unclassified gateway failure."""
    g, sink = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    async def _raising_iter() -> AsyncIterator[Any]:
        yield {"choices": [{"delta": {"content": "hello [N1]"}}]}
        raise httpx.RemoteProtocolError("peer reset")

    chunks = []
    with pytest.raises(httpx.RemoteProtocolError):
        async for chunk in restore_stream(g, data, _raising_iter()):
            chunks.append(chunk)

    # No fabricated E_INTERNAL for a real provider failure — litellm owns it.
    assert sink.records == []


async def test_post_call_stream_own_bug_mid_stream_returns_opaque_500(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Major: a bug in OUR OWN desanitization work mid-stream (as opposed to an upstream
    transport failure) is caught at the reversal and never reaches the client or a log
    with content — this runs after placeholders were replaced by originals. The status
    already went out, so the stream is closed and the record is ``failed`` +
    ``E_INTERNAL``."""
    from corp_llm_gateway.route_gate import desanitize_middleware

    g, sink = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    async def _good_iter() -> AsyncIterator[Any]:
        yield {"choices": [{"delta": {"content": "hello [N1]"}}]}
        yield {"choices": [{"delta": {"content": " again [N1]"}}]}

    def _boom(self: Any, event: Any) -> Any:
        raise RuntimeError("desanitize bug for alice")

    monkeypatch.setattr(desanitize_middleware.SseStreamDesanitizer, "feed", _boom)
    terminal, records = _terminal()
    caplog.clear()  # the pre-call's own lines name the user, who is also "alice"
    with caplog.at_level(logging.DEBUG):
        out = [chunk async for chunk in restore_stream(g, data, _good_iter(), terminal=terminal)]

    assert "alice" not in json.dumps(out) and "alice" not in caplog.text
    assert "gateway_desanitize_failed" in caplog.text
    assert sink.records == []
    (record,) = records.records
    assert (record["status"], record["error_code"]) == ("failed", "E_INTERNAL")


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
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    assert out_text == "msg=b@corp.example sys=a@corp.example"


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
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    assert out_text == "known a@x hallucinated [EMAIL_999]"


async def test_post_call_stream_desanitizes_chunks() -> None:
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    chunks_in = [
        {"choices": [{"delta": {"content": "hello [N"}}]},
        {"choices": [{"delta": {"content": "1] world"}}]},
    ]
    out_text = ""
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
        text = chunk["choices"][0]["delta"]["content"]
        out_text += text
    assert out_text == "hello alice world"


async def test_post_call_stream_no_mapping_passes_through() -> None:
    g, _ = _build_guardrail([])
    data = _data_with_token("tok-1", content="no PII")
    await g.pre_call(data)
    chunks_in = [{"choices": [{"delta": {"content": "boring text"}}]}]
    out = []
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
        out.append(chunk)
    assert out == chunks_in


async def test_post_call_stream_unknown_request_passes_through() -> None:
    g, _ = _build_guardrail()
    data = {"model": "claude", "_corp_gateway_request_id": "never-seen"}
    chunks_in = [{"choices": [{"delta": {"content": "x"}}]}]
    out = []
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
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
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    assert out_text == "hello alice world"


async def test_post_call_stream_responses_events_restore_split_placeholder() -> None:
    pytest.importorskip("litellm", reason="typed Responses stream events need litellm installed")
    from litellm.types.llms.openai import (
        OutputTextDeltaEvent,
        OutputTextDoneEvent,
        ResponsesAPIStreamEvents,
    )

    g, _ = _build_guardrail([("Kdir", "[ORG_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "input": "Implement KdirService",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(data)
    events = [
        OutputTextDeltaEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DELTA,
            item_id="item_1",
            output_index=0,
            content_index=0,
            delta="Result [ORG_",
        ),
        OutputTextDeltaEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DELTA,
            item_id="item_1",
            output_index=0,
            content_index=0,
            delta="001]Service",
        ),
        OutputTextDoneEvent(
            type=ResponsesAPIStreamEvents.OUTPUT_TEXT_DONE,
            item_id="item_1",
            output_index=0,
            content_index=0,
            text="Result [ORG_001]Service",
        ),
    ]

    out: list[Any] = []
    async for chunk in restore_stream(g, data, _async_iter(events)):
        out.append(chunk)

    deltas: list[str] = []
    done_text = ""
    for chunk in out:
        # The client reads each typed event as the JSON litellm put on the wire.
        payload = chunk
        if payload["type"] == "response.output_text.delta":
            deltas.append(payload["delta"])
        elif payload["type"] == "response.output_text.done":
            done_text = payload["text"]
    assert "".join(deltas) == "Result KdirService"
    assert done_text == "Result KdirService"
    assert "[ORG_001]" not in json.dumps(out)


async def test_post_call_stream_responses_restores_bracket_stripped_identifier() -> None:
    """Models drop placeholder brackets when generating identifiers and paths.

    Bare-alias restoration is a Codex-only behavior (defect #6) — only
    exercised with ``forward_chatgpt_auth=True``.
    """
    original = "KdirCorpCalculatorService"
    g, _ = _build_guardrail([(original, "[LOCATION_007]")], forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-luna",
        "input": f"Create class {original}",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(data)
    events = [
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": "Create class LOCA",
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": "TION_007",
        },
        {
            "type": "response.output_text.done",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "text": "Create class LOCATION_007",
        },
        {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "tool_1",
            "output_index": 1,
            "delta": "*** Add File: LOCATION_",
        },
        {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "tool_1",
            "output_index": 1,
            "delta": "007.cs",
        },
        {
            "type": "response.custom_tool_call_input.done",
            "item_id": "tool_1",
            "output_index": 1,
            "input": "*** Add File: LOCATION_007.cs",
        },
    ]

    out: list[Any] = []
    async for chunk in restore_stream(g, data, _async_iter(events)):
        out.append(json.loads(chunk) if isinstance(chunk, str) else chunk)

    text_deltas = "".join(
        event["delta"] for event in out if event["type"] == "response.output_text.delta"
    )
    tool_deltas = "".join(
        event["delta"] for event in out if event["type"] == "response.custom_tool_call_input.delta"
    )
    assert text_deltas == f"Create class {original}"
    assert tool_deltas == f"*** Add File: {original}.cs"
    assert (
        next(event for event in out if event["type"] == "response.output_text.done")["text"]
        == f"Create class {original}"
    )
    assert (
        next(event for event in out if event["type"] == "response.custom_tool_call_input.done")[
            "input"
        ]
        == f"*** Add File: {original}.cs"
    )


async def test_post_call_stream_custom_tool_input_delta_and_done_agree_on_special_chars() -> None:
    """custom_tool_call.input is freeform text (a diff/shell command), not JSON —
    escaping the delta half while the `.done` half stays unescaped (Task 15 item 1)
    made quotes/backslashes/newlines diverge between the two."""
    original = 'diff --git a/x "b/x"\ncontent with \\ backslash'
    g, _ = _build_guardrail([(original, "[SECRET_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "input": f"apply: {original}",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(data)
    events = [
        {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "tool_1",
            "output_index": 0,
            "delta": "*** patch: [SECR",
        },
        {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "tool_1",
            "output_index": 0,
            "delta": "ET_001]",
        },
        {
            "type": "response.custom_tool_call_input.done",
            "item_id": "tool_1",
            "output_index": 0,
            "input": "*** patch: [SECRET_001]",
        },
    ]

    out: list[Any] = []
    async for chunk in restore_stream(g, data, _async_iter(events)):
        out.append(json.loads(chunk) if isinstance(chunk, str) else chunk)

    tool_deltas = "".join(
        event["delta"] for event in out if event["type"] == "response.custom_tool_call_input.delta"
    )
    done_input = next(
        event for event in out if event["type"] == "response.custom_tool_call_input.done"
    )["input"]
    expected = f"*** patch: {original}"
    assert tool_deltas == expected
    assert done_input == expected
    assert tool_deltas == done_input


async def test_post_call_stream_responses_bare_alias_does_not_corrupt_containing_identifier() -> (
    None
):
    """Same repro as the unary test, over the Responses SSE streaming path,
    with the alias split across two delta events at the identifier boundary."""
    original = "SecretPlace"
    g, _ = _build_guardrail([(original, "[LOCATION_007]")], forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-luna",
        "input": f"see {original}",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(data)
    events = [
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": "const MY_",
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "delta": "LOCATION_007 = 1;",
        },
        {
            "type": "response.output_text.done",
            "item_id": "msg_1",
            "output_index": 0,
            "content_index": 0,
            "text": "const MY_LOCATION_007 = 1;",
        },
    ]

    out: list[Any] = []
    async for chunk in restore_stream(g, data, _async_iter(events)):
        out.append(json.loads(chunk) if isinstance(chunk, str) else chunk)

    text_deltas = "".join(
        event["delta"] for event in out if event["type"] == "response.output_text.delta"
    )
    assert text_deltas == "const MY_LOCATION_007 = 1;"
    assert (
        next(event for event in out if event["type"] == "response.output_text.done")["text"]
        == "const MY_LOCATION_007 = 1;"
    )


async def test_post_call_stream_anthropic_sse_bare_alias_does_not_corrupt_identifier() -> None:
    """Same repro over the Anthropic/OpenAI SSE `StreamingDesanitizer` path,
    with the alias split across two text_delta events."""
    original = "SecretPlace"
    g, _ = _build_guardrail([(original, "[LOCATION_007]")], forward_chatgpt_auth=True)
    data = _data_with_token("tok-1", content=f"see {original}")
    await g.pre_call(data)

    sse_events: list[bytes] = [
        _MSG_START,
        _cb_start(0),
        _delta("const MY_"),
        _delta("LOCATION_007 = 1;"),
        _cb_stop(0),
        _MSG_DELTA,
        _MSG_STOP,
    ]

    out_chunks: list[bytes] = []
    async for chunk in restore_stream(g, data, _async_iter(sse_events)):
        out_chunks.append(chunk)

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
    assert full_text == "const MY_LOCATION_007 = 1;"


_ANTHROPIC_ERROR = (
    b"event: error\n"
    b'data: {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}\n\n'
)


async def test_post_call_stream_anthropic_error_event_follows_the_restored_held_tail() -> None:
    """Live callback path: a mid-stream ``event: error`` ends the stream for the client, so
    the held tail goes out restored BEFORE it, the error bytes are unchanged, and nothing
    is sent after it."""
    out = await _anthropic_stream_through_the_callback(
        [_MSG_START, _cb_start(0), _delta("to [EMAIL_001]"), _ANTHROPIC_ERROR]
    )

    events = _sse_events(out)
    assert events[-1] == _ANTHROPIC_ERROR
    assert _ANTHROPIC_ERROR not in b"".join(events[:-1])
    assert _text_deltas(events[:-1]) == "to user@example.com"
    assert b"[EMAIL_001]" not in b"".join(out)


async def test_post_call_stream_anthropic_truncated_stream_still_sends_the_held_tail() -> None:
    """No ``content_block_stop`` / ``message_stop``: the held tail goes out at the end."""
    out = await _anthropic_stream_through_the_callback(
        [_MSG_START, _cb_start(0), _delta("to [EMAIL_001]")]
    )

    events = _sse_events(out)
    assert _text_deltas(events) == "to user@example.com"
    assert _text_deltas(events[-1:]) != ""
    assert b"[EMAIL_001]" not in b"".join(out)


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
    async for chunk in restore_stream(g, data, _async_iter(sse_events)):
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


async def test_post_call_stream_openai_tool_calls_arguments_desanitized() -> None:
    """F4: streamed OpenAI tool_calls argument deltas are desanitized (dict chunks)."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice", model="gpt-4o")
    await g.pre_call(data)

    # [N1] straddles two argument fragments; id/name arrive on the first delta.
    chunks_in = [
        _tc_delta_chunk("", first=True),
        _tc_delta_chunk('{"who": "[N'),
        _tc_delta_chunk('1]"}'),
    ]
    args_out = ""
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
        for tc in chunk["choices"][0]["delta"].get("tool_calls") or []:
            args_out += tc["function"]["arguments"]
    assert json.loads(args_out) == {"who": "alice"}


async def test_post_call_stream_openai_tool_calls_sse_desanitized() -> None:
    """F4: raw-SSE OpenAI tool_calls argument deltas are desanitized (bytes path)."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice", model="gpt-4o")
    await g.pre_call(data)

    def _ev(obj: dict) -> bytes:
        return ("data: " + json.dumps(obj) + "\n\n").encode()

    sse_in = [
        _ev(_tc_delta_chunk("", first=True)),
        _ev(_tc_delta_chunk('{"who": "[N')),
        _ev(_tc_delta_chunk('1]"}')),
        b"data: [DONE]\n\n",
    ]
    args_out = ""
    async for chunk in restore_stream(g, data, _async_iter(sse_in)):
        for line in chunk.decode().splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            obj = json.loads(payload)
            for tc in obj["choices"][0]["delta"].get("tool_calls") or []:
                args_out += tc["function"]["arguments"]
    assert json.loads(args_out) == {"who": "alice"}


async def test_post_call_stream_mixed_content_and_tool_calls_dict() -> None:
    """A3 review: a dict delta carrying content:'' AND tool_calls must keep the
    tool_calls (id/name/arguments) — the held-back content must not drop them."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice", model="gpt-4o")
    await g.pre_call(data)

    chunk = {
        "choices": [
            {
                "delta": {
                    "content": "",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": '{"who": "[N1]"}'},
                        }
                    ],
                }
            }
        ]
    }
    ids: list[str] = []
    args_out = ""
    async for out in restore_stream(g, data, _async_iter([chunk])):
        for tc in out["choices"][0]["delta"].get("tool_calls") or []:
            if tc.get("id"):
                ids.append(tc["id"])
            args_out += tc["function"].get("arguments", "")
    assert ids == ["c1"], "tool_call id/name dropped by held-back content"
    assert json.loads(args_out) == {"who": "alice"}


async def test_post_call_stream_mixed_content_and_tool_calls_sse() -> None:
    """A3 review: same as above for the raw-SSE path — content:'' must not drop
    the tool_calls in the same delta."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice", model="gpt-4o")
    await g.pre_call(data)

    delta = {
        "content": "",
        "tool_calls": [
            {
                "index": 0,
                "id": "c1",
                "type": "function",
                "function": {"name": "f", "arguments": '{"who": "[N1]"}'},
            }
        ],
    }
    sse_in = [
        ("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n").encode(),
        b"data: [DONE]\n\n",
    ]
    ids: list[str] = []
    args_out = ""
    async for chunk in restore_stream(g, data, _async_iter(sse_in)):
        for line in chunk.decode().splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            for tc in json.loads(payload)["choices"][0]["delta"].get("tool_calls") or []:
                if tc.get("id"):
                    ids.append(tc["id"])
                args_out += tc["function"].get("arguments", "")
    assert ids == ["c1"], "tool_call id/name dropped by held-back content (SSE)"
    assert json.loads(args_out) == {"who": "alice"}


async def test_post_call_stream_legacy_function_call_desanitized_dict() -> None:
    """A3 review: streamed legacy delta.function_call.arguments fragments are
    desanitized (dict path) — placeholders must not leak to the client."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice", model="gpt-4o")
    await g.pre_call(data)

    chunks_in = [
        {"choices": [{"delta": {"function_call": {"name": "f", "arguments": ""}}}]},
        {"choices": [{"delta": {"function_call": {"arguments": '{"who": "[N'}}}]},
        {"choices": [{"delta": {"function_call": {"arguments": '1]"}'}}}]},
    ]
    args_out = ""
    async for out in restore_stream(g, data, _async_iter(chunks_in)):
        fc = out["choices"][0]["delta"].get("function_call") or {}
        args_out += fc.get("arguments", "")
    assert json.loads(args_out) == {"who": "alice"}


async def test_post_call_stream_legacy_function_call_desanitized_sse() -> None:
    """A3 review: streamed legacy function_call.arguments is desanitized (SSE path)."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice", model="gpt-4o")
    await g.pre_call(data)

    def _ev(fc: dict) -> bytes:
        return (
            "data: " + json.dumps({"choices": [{"delta": {"function_call": fc}}]}) + "\n\n"
        ).encode()

    sse_in = [
        _ev({"name": "f", "arguments": ""}),
        _ev({"arguments": '{"who": "[N'}),
        _ev({"arguments": '1]"}'}),
        b"data: [DONE]\n\n",
    ]
    args_out = ""
    async for chunk in restore_stream(g, data, _async_iter(sse_in)):
        for line in chunk.decode().splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            fc = json.loads(payload)["choices"][0]["delta"].get("function_call") or {}
            args_out += fc.get("arguments", "")
    assert json.loads(args_out) == {"who": "alice"}


async def test_post_call_stream_parallel_tool_calls_per_index_reassembly() -> None:
    """A3 review: parallel tool_calls (index 0 and 1), each with a split placeholder,
    reassemble per index. They arrive one call after the other, as OpenAI streams them;
    fragments of two calls interleaved (off-spec) degrade to placeholders, never to a
    wrong original (``test_interleaved_tool_call_fragments_degrade_to_placeholders_never_
    originals``)."""
    g, _ = _build_guardrail([("alice", "[N1]"), ("bob", "[N2]")])
    data = _data_with_token("tok-1", content="hi alice and bob", model="gpt-4o")
    await g.pre_call(data)

    chunks_in = [
        _tc_delta_chunk("", index=0, first=True),
        _tc_delta_chunk('{"a": "[N', index=0),
        _tc_delta_chunk('1]"}', index=0),
        _tc_delta_chunk("", index=1, first=True),
        _tc_delta_chunk('{"b": "[N', index=1),
        _tc_delta_chunk('2]"}', index=1),
    ]
    by_index: dict[int, str] = {}
    async for out in restore_stream(g, data, _async_iter(chunks_in)):
        for tc in out["choices"][0]["delta"].get("tool_calls") or []:
            by_index[tc["index"]] = by_index.get(tc["index"], "") + tc["function"]["arguments"]
    assert json.loads(by_index[0]) == {"a": "alice"}
    assert json.loads(by_index[1]) == {"b": "bob"}


async def test_post_call_stream_interleaved_tool_calls_degrade_to_placeholders() -> None:
    """Known limitation, accepted: two calls' fragments interleaved (off-spec for OpenAI,
    never produced by the v1 providers) come back as placeholders, never as originals, and
    the stream does not fail. The restorer's own pin is
    ``tests/sanitizer/test_streaming_chat_sse.py::test_interleaved_tool_call_fragments_
    degrade_to_placeholders_never_originals``."""
    g, _ = _build_guardrail([("alice", "[N1]"), ("bob", "[N2]")])
    data = _data_with_token("tok-1", content="hi alice and bob", model="gpt-4o")
    await g.pre_call(data)

    chunks_in = [
        _tc_delta_chunk("", index=0, first=True),
        _tc_delta_chunk("", index=1, first=True),
        _tc_delta_chunk('{"a": "[N', index=0),
        _tc_delta_chunk('{"b": "[N', index=1),
        _tc_delta_chunk('1]"}', index=0),
        _tc_delta_chunk('2]"}', index=1),
    ]
    by_index: dict[int, str] = {}
    async for out in restore_stream(g, data, _async_iter(chunks_in)):
        for tc in out["choices"][0]["delta"].get("tool_calls") or []:
            by_index[tc["index"]] = by_index.get(tc["index"], "") + tc["function"]["arguments"]
    assert by_index == {0: '{"a": "[N1]"}', 1: '{"b": "[N2]"}'}


async def test_post_call_stream_tool_calls_bad_index_does_not_crash() -> None:
    """A3 review: a garbage tool_call index must be skipped, not crash the stream —
    subsequent legit content still desanitizes."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice", model="gpt-4o")
    await g.pre_call(data)

    chunks_in = [
        {"choices": [{"delta": {"tool_calls": [{"index": None, "function": {"arguments": "x"}}]}}]},
        {"choices": [{"delta": {"content": "hi [N1]"}}]},
    ]
    content_out = ""
    async for out in restore_stream(g, data, _async_iter(chunks_in)):
        delta = out["choices"][0]["delta"]
        if isinstance(delta.get("content"), str):
            content_out += delta["content"]
    assert "alice" in content_out


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
    async for chunk in restore_stream(g, data2, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    assert out_text == "msg=b@corp.example sys=a@corp.example", f"round-trip failed: {out_text!r}"


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
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
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
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
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
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    assert out_text == "john.doe@corp.example / john", (
        f"reverse substitution corrupted: {out_text!r}"
    )


async def test_rule_overlap_round_trip_survives_placeholder_canonicalization() -> None:
    """Span decisions survive allocator remapping and reverse correctly."""
    g, _ = _build_guardrail(
        [
            ("Alice", "[PERSON_001]"),
            ("Bob", "[PERSON_001]"),
        ],
        rules=Rules(rules=(Rule("Alice Smith", "[CONTRACTOR_001]"),)),
    )
    data = _data_with_token(
        "tok-1",
        content="Alice Smith met Alice and Bob; marker [PERSON_001]",
    )

    out = await g.pre_call(data)
    sanitized = out["messages"][0]["content"]
    match = re.fullmatch(
        r"\[CONTRACTOR_001\] met (\[PERSON_\d+\]) and (\[PERSON_\d+\]); "
        r"marker \[PERSON_001\]",
        sanitized,
    )
    assert match is not None, sanitized
    alice_token, bob_token = match.groups()
    assert alice_token != bob_token
    assert "Alice" not in sanitized
    assert "Bob" not in sanitized

    chunks_in = [
        {
            "choices": [
                {
                    "delta": {
                        "content": (
                            f"[CONTRACTOR_001] met {alice_token} and {bob_token}; "
                            "marker [PERSON_001]"
                        )
                    }
                }
            ]
        }
    ]
    restored = ""
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
        restored += chunk["choices"][0]["delta"]["content"]

    assert restored == "Alice Smith met Alice and Bob; marker [PERSON_001]"


async def test_case_insensitive_rule_matches_collapse_to_one_configured_token() -> None:
    """Rule `Acme = PARTNER-A`, matched case-insensitively against
    `acme`/`ACME`/`Acme`, must ALL become the one configured replacement —
    not three tokens, two of them minted and never documented anywhere in
    the operator's dictionary."""
    g, _ = _build_guardrail([], rules=Rules(rules=(Rule("Acme", "PARTNER-A"),)))
    data = _data_with_token("tok-1", content="acme and ACME and Acme")

    out = await g.pre_call(data)
    sanitized = out["messages"][0]["content"]
    assert sanitized == "PARTNER-A and PARTNER-A and PARTNER-A"

    # Many-to-one is inherently lossy on reverse: one casing wins for every
    # occurrence of the shared token. Which one wins is deterministic (the
    # FIRST-registered pair for that placeholder — build_reverse_substituter
    # agrees with RequestPlaceholderAllocator's own first-claim semantics),
    # not a leak either way.
    restored = ""
    chunks_in = [{"choices": [{"delta": {"content": sanitized}}]}]
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
        restored += chunk["choices"][0]["delta"]["content"]
    assert restored == "acme and acme and acme"


# ---- Anthropic SSE bytes and str chunks, with their own harness (empty rules) ----


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
    async for chunk in restore_stream(g, data, _iter(chunks)):
        out.append(chunk)
    return out


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
    """A stream event the restorer cannot read (not JSON, no ``data:``) passes through
    unchanged. On the wire every chunk is bytes; what was an unknown Python type in the
    callback is an unreadable event here."""
    g = _build([("alice", "[N1]")])
    data = _request(content="hi alice")
    await g.pre_call(data)

    unreadable = b": keep-alive \x01\n\n"
    out = await _collect(g, data, [unreadable])
    assert b"".join(out) == unreadable


async def test_post_call_stream_no_pre_call_passthrough() -> None:
    """Without a preceding pre_call (unknown request_id), SSE bytes pass through."""
    g = _build([("alice", "[N1]")])
    data = {"model": "claude", "_corp_gateway_request_id": "unknown-id-xyz"}
    events: list[bytes] = [_MSG_STOP]
    out = await _collect(g, data, events)
    assert _MSG_STOP in out
