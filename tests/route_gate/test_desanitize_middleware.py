"""The response desanitiser middleware: originals restored per request, failures content-free.

It restores the originals in unary JSON, chat / Anthropic SSE and Responses SSE with the mapping
held on the request's ticket (never a client header), releases that mapping on the final body,
and answers any failure with a response that carries no content.

Plan 20260926 Task 0; the module is ``route_gate/desanitize_middleware.py``.

Stub ASGI apps emit litellm's three wire formats (unary JSON, chat / Anthropic SSE,
Responses SSE); the last group mounts the prototype over litellm's real app. The three
rev 5 contracts are what these tests pin: ticket-keyed state released by the middleware,
never by ``audit()``; a content-free exception boundary; a wire-format adapter per shape.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import logging
import uuid
import weakref
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("litellm.proxy.proxy_server", reason="litellm proxy not installed")

from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from corp_llm_gateway.metrics import NoopExporter
from corp_llm_gateway.route_gate import desanitize_middleware
from corp_llm_gateway.route_gate.desanitize_middleware import (
    COMPONENT,
    INTERNAL_ERROR_BODY,
    DesanitizeMiddleware,
    ResponseMappings,
    compressor_problems,
)
from corp_llm_gateway.route_gate.inflight import (
    _TICKET,
    InflightLimiter,
    RequestTicket,
    current_ticket,
    install_task_factory,
)
from corp_llm_gateway.route_gate.terminal_audit import (
    AuditFacts,
    TerminalAudit,
    TerminalRecord,
    deposit,
    deposit_usage,
)
from corp_llm_gateway.sanitizer.strategies import StrategyResult
from tests.hook_fixtures import _data_with_token
from tests.litellm_hook import _dispatch_fixtures
from tests.litellm_hook._dispatch_fixtures import (
    EMAIL,
    ORIGINAL_MARK,
    PLACEHOLDER,
    PLACEHOLDER_MARK,
    DispatchHarness,
    OptionAPreCall,
    StubUpstream,
    build_ours,
)

ROOT = Path(__file__).resolve().parents[2]
QUOTED = 'Dan "the man" O\'Neil'
NAME = "[NAME_1]"
MAPPING = StrategyResult(pairs=((EMAIL, PLACEHOLDER), (QUOTED, NAME)))
CANARY = "CANARY-8d1f"

Message = dict[str, Any]
ASGIApp = Callable[[Any, Any, Any], Awaitable[None]]


# ── driving one request ──────────────────────────────────────────────────────


async def drive(
    app: ASGIApp, ticket: RequestTicket | None = None, sent: list[Message] | None = None
) -> list[Message]:
    """One HTTP request, in the ticket's context as the in-flight limiter runs it."""
    sent = sent if sent is not None else []
    delivered = False
    done = asyncio.Event()

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"{}", "more_body": False}
        # As a server does: the disconnect comes once the response is complete.
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(dict(message))
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            done.set()

    token = _TICKET.set(ticket or RequestTicket(uuid.uuid4().hex))
    try:
        await app(_scope(), receive, send)
    finally:
        _TICKET.reset(token)
    return sent


def _scope() -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "server": ("gateway", 80),
        "client": ("127.0.0.1", 50000),
    }


def body_of(sent: list[Message]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def start_of(sent: list[Message]) -> Message:
    (start,) = [m for m in sent if m["type"] == "http.response.start"]
    return start


def header(message: Message, name: bytes) -> bytes | None:
    return next((v for k, v in message.get("headers", []) if k.lower() == name), None)


def json_app(
    payload: Any, *, status: int = 200, extra_headers: list[tuple[bytes, bytes]] | None = None
) -> ASGIApp:
    body = json.dumps(payload).encode()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    *(extra_headers or []),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return app


def sse_app(
    chunks: list[str], *, after_last: Callable[[], Awaitable[None]] | None = None
) -> ASGIApp:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
            }
        )
        for chunk in chunks:
            await send({"type": "http.response.body", "body": chunk.encode(), "more_body": True})
        if after_last is not None:
            await after_last()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    return app


def mounted(app: ASGIApp, mappings: ResponseMappings, **kwargs: Any) -> ASGIApp:
    """The prototype, with the mapping registered for whichever ticket the request runs in."""
    middleware = DesanitizeMiddleware(app, mappings, **kwargs)

    async def with_mapping(scope: Any, receive: Any, send: Any) -> None:
        ticket = current_ticket()
        assert ticket is not None
        mappings.register(ticket, MAPPING)
        await middleware(scope, receive, send)

    return with_mapping


# ── wire formats ─────────────────────────────────────────────────────────────

CHAT = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": f"mail {PLACEHOLDER} for {NAME}"},
            "finish_reason": "stop",
        }
    ],
}
ANTHROPIC = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": f"mail {PLACEHOLDER} for {NAME}"}],
}
RESPONSES = {
    "id": "resp_1",
    "object": "response",
    "status": "completed",
    "output": [
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": f"mail {PLACEHOLDER} for {NAME}"}],
        }
    ],
}


UNARY = {
    "chat": (CHAT, lambda body: body["choices"][0]["message"]["content"]),
    "anthropic": (ANTHROPIC, lambda body: body["content"][0]["text"]),
    "responses": (RESPONSES, lambda body: body["output"][0]["content"][0]["text"]),
}


@pytest.mark.parametrize("shape", sorted(UNARY))
async def test_unary_json_is_restored_and_content_length_recomputed(shape: str) -> None:
    payload, text_of = UNARY[shape]
    mappings = ResponseMappings()

    sent = await drive(mounted(json_app(payload), mappings))

    body = body_of(sent)
    assert int(header(start_of(sent), b"content-length") or b"0") == len(body)
    assert len(body) != len(json.dumps(payload).encode())
    assert text_of(json.loads(body)) == f"mail {EMAIL} for {QUOTED}"
    assert len(mappings) == 0


def _chat_chunks() -> list[str]:
    def chunk(content: str) -> str:
        return (
            "data: "
            + json.dumps({"choices": [{"index": 0, "delta": {"content": content}}]})
            + "\n\n"
        )

    # Placeholders split across chunk AND across an SSE event boundary mid-write.
    whole = chunk("mail [EM") + chunk("AIL_1] for [NA") + chunk("ME_1]") + "data: [DONE]\n\n"
    return [whole[:37], whole[37:120], whole[120:]]


def _anthropic_chunks() -> list[str]:
    def delta(text: str) -> str:
        data = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": text},
        }
        return f"event: content_block_delta\ndata: {json.dumps(data)}\n\n"

    start = {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    return [
        f"event: content_block_start\ndata: {json.dumps(start)}\n\n",
        delta("mail [EM"),
        delta("AIL_1] for [NA") + delta("ME_1]"),
        'event: content_block_stop\ndata: {"type": "content_block_stop", "index": 0}\n\n',
        'event: message_stop\ndata: {"type": "message_stop"}\n\n',
    ]


def _chat_text(stream: str) -> str:
    out = []
    for line in stream.splitlines():
        if line.startswith("data: {"):
            out.append(json.loads(line[6:])["choices"][0]["delta"].get("content") or "")
    return "".join(out)


def _anthropic_text(stream: str) -> str:
    out = []
    for line in stream.splitlines():
        if line.startswith("data: {"):
            event = json.loads(line[6:])
            if event.get("type") == "content_block_delta":
                out.append(event["delta"]["text"])
    return "".join(out)


@pytest.mark.parametrize(
    ("chunks", "text_of"),
    [(_chat_chunks(), _chat_text), (_anthropic_chunks(), _anthropic_text)],
    ids=["chat", "anthropic"],
)
async def test_sse_is_restored_across_split_placeholders(
    chunks: list[str], text_of: Callable[[str], str]
) -> None:
    mappings = ResponseMappings()

    sent = await drive(mounted(sse_app(chunks), mappings))

    stream = body_of(sent).decode()
    assert text_of(stream) == f"mail {EMAIL} for {QUOTED}"
    assert PLACEHOLDER_MARK not in stream and "[NA" not in stream
    assert header(start_of(sent), b"content-length") is None
    assert len(mappings) == 0


def _responses_events(*, with_event_lines: bool) -> list[str]:
    ids = {"item_id": "fc_1", "output_index": 1}
    text_ids = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
    args = json.dumps({"to": NAME, "cc": PLACEHOLDER})
    cut = args.index("[NA") + 3
    events = [
        {
            "type": "response.created",
            "sequence_number": 0,
            "response": {"id": "resp_1", "output": []},
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 1,
            **text_ids,
            "delta": "mail [EM",
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 2,
            **text_ids,
            "delta": "AIL_1] ok",
        },
        {
            "type": "response.output_text.done",
            "sequence_number": 3,
            **text_ids,
            "text": f"mail {PLACEHOLDER} ok",
        },
        {
            "type": "response.function_call_arguments.delta",
            "sequence_number": 4,
            **ids,
            "delta": args[:cut],
        },
        {
            "type": "response.function_call_arguments.delta",
            "sequence_number": 5,
            **ids,
            "delta": args[cut:],
        },
        {
            "type": "response.function_call_arguments.done",
            "sequence_number": 6,
            **ids,
            "arguments": args,
        },
        {
            "type": "response.output_item.done",
            "sequence_number": 7,
            "output_index": 1,
            "item": {"id": "fc_1", "type": "function_call", "name": "send", "arguments": args},
        },
        {
            "type": "response.completed",
            "sequence_number": 8,
            "response": {
                **RESPONSES,
                "output": [
                    *RESPONSES["output"],
                    {"id": "fc_1", "type": "function_call", "name": "send", "arguments": args},
                ],
            },
        },
    ]
    frames = [
        (f"event: {e['type']}\n" if with_event_lines else "") + f"data: {json.dumps(e)}\n\n"
        for e in events
    ]
    return [*frames, "data: [DONE]\n\n"]


def _events(stream: str) -> list[dict[str, Any]]:
    return [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: {")]


@pytest.mark.parametrize("with_event_lines", [False, True], ids=["data-only", "event-lines"])
async def test_responses_sse_goes_through_the_responses_adapter(with_event_lines: bool) -> None:
    """``SseStreamDesanitizer`` leaves ``response.*`` events untouched; the prototype routes
    them through ``ResponsesStreamDesanitizer``: split text deltas, JSON-escaped split
    function-call arguments, and the ``done`` / ``output_item.done`` / ``completed``
    duplicates all come back restored, and the event framing is kept."""
    mappings = ResponseMappings()

    sent = await drive(
        mounted(sse_app(_responses_events(with_event_lines=with_event_lines)), mappings)
    )

    stream = body_of(sent).decode()
    events = _events(stream)
    text = "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")
    args = "".join(
        e["delta"] for e in events if e["type"] == "response.function_call_arguments.delta"
    )
    assert text == f"mail {EMAIL} ok"
    assert json.loads(args) == {"to": QUOTED, "cc": EMAIL}
    by_type = {e["type"]: e for e in events}
    assert by_type["response.output_text.done"]["text"] == f"mail {EMAIL} ok"
    assert json.loads(by_type["response.function_call_arguments.done"]["arguments"])["to"] == QUOTED
    assert QUOTED in json.loads(by_type["response.output_item.done"]["item"]["arguments"])["to"]
    assert EMAIL in json.dumps(by_type["response.completed"])
    assert PLACEHOLDER_MARK not in stream and "[NA" not in stream
    assert ("event: response.completed" in stream) is with_event_lines
    assert stream.endswith("data: [DONE]\n\n")
    assert len(mappings) == 0


# ── correlation and state ownership ──────────────────────────────────────────


@pytest.mark.parametrize("call_id", [b"someone-elses-id", None], ids=["overwritten", "omitted"])
async def test_correlation_is_the_ticket_not_the_call_id_header(call_id: bytes | None) -> None:
    """A header hook can overwrite ``x-litellm-call-id`` (common_request_processing.py:
    1736-1743) and early responses carry none; the prototype never reads it."""
    extra = [(b"x-litellm-call-id", call_id)] if call_id is not None else []
    mappings = ResponseMappings()

    sent = await drive(mounted(json_app(CHAT, extra_headers=extra), mappings))

    assert EMAIL in body_of(sent).decode()


async def test_concurrent_requests_restore_with_their_own_mapping() -> None:
    mappings = ResponseMappings()
    other = StrategyResult(pairs=(("bob@corp.example", PLACEHOLDER),))
    both_started = asyncio.Event()
    started = 0

    async def slow(scope: Any, receive: Any, send: Any) -> None:
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await both_started.wait()
        await json_app(CHAT)(scope, receive, send)

    first, second = RequestTicket("a" * 32), RequestTicket("b" * 32)
    mappings.register(first, MAPPING)
    mappings.register(second, other)
    middleware = DesanitizeMiddleware(slow, mappings)

    a, b = await asyncio.gather(drive(middleware, first), drive(middleware, second))

    assert EMAIL in body_of(a).decode() and "bob@corp.example" not in body_of(a).decode()
    assert "bob@corp.example" in body_of(b).decode() and EMAIL not in body_of(b).decode()
    assert len(mappings) == 0


async def test_error_responses_pass_through_and_still_release() -> None:
    mappings = ResponseMappings()
    payload = {"error": {"message": f"401 E_MISSING_TOKEN near {PLACEHOLDER}"}}

    sent = await drive(mounted(json_app(payload, status=401), mappings))

    assert json.loads(body_of(sent)) == payload
    assert len(mappings) == 0


async def test_the_mapping_is_released_on_the_final_body_before_the_app_returns() -> None:
    mappings = ResponseMappings()
    seen_after: list[bool] = []

    async def check() -> None:
        ticket = current_ticket()
        seen_after.append(ticket in mappings)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await json_app(CHAT)(scope, receive, send)
        await check()

    await drive(mounted(app, mappings))

    assert seen_after == [False]


async def test_audit_before_response_start_does_not_lose_the_mapping() -> None:
    """Contract (ii), made deterministic: litellm flushes deferred success logging right
    after ``post_call_success_hook`` (common_request_processing.py:2629-2632), before the
    header hook and the ASGI send. Here ``audit()`` completes first: it neither releases the
    mapping the pre-call handed the ticket nor writes a record of its own; it only adds the
    token counts. The middleware restores, and the terminal record is the one record.
    """
    guardrail, sink = build_ours()
    mappings = ResponseMappings()
    guardrail.bind_response_mappings(mappings)
    records = Records()
    ticket = RequestTicket("c" * 32)
    token = _TICKET.set(ticket)
    try:
        data = _data_with_token("tok-1", content=f"write to {EMAIL}")
        data["litellm_call_id"] = "call-1"
        await guardrail.pre_call(data)
    finally:
        _TICKET.reset(token)
    assert ticket in mappings and "call-1" not in guardrail._req_state
    now = datetime.now(UTC)
    usage = {"usage": {"prompt_tokens": TOKEN_COUNTS[0], "completion_tokens": TOKEN_COUNTS[1]}}

    async def audited_then_answers(scope: Any, receive: Any, send: Any) -> None:
        await guardrail.audit(data, usage, now, now, status="ok")
        assert ticket in mappings and sink.records == []
        await json_app(CHAT)(scope, receive, send)

    sent = await drive(
        DesanitizeMiddleware(audited_then_answers, mappings, terminal=TerminalAudit(records)),
        ticket,
    )

    assert EMAIL in body_of(sent).decode()
    assert sink.records == [] and records.outcomes == [("ok", None)]
    event = records.records[0].event()
    assert (event.prompt_token_count, event.completion_token_count) == TOKEN_COUNTS
    assert len(mappings) == 0


# ── abort ────────────────────────────────────────────────────────────────────


def _stream_for(kind: str) -> list[str]:
    """A stream cut after a placeholder started: what a client that leaves mid-way got."""
    if kind == "chat":
        opening = "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "hi. "}}]})
        return [opening + "\n\n", _chat_chunks()[0] + _chat_chunks()[1]]
    if kind == "anthropic":
        return _anthropic_chunks()[:3]
    return _responses_events(with_event_lines=False)[:3]


@pytest.mark.parametrize("kind", ["chat", "anthropic", "responses"])
async def test_stream_abort_releases_the_mapping_and_logs_no_content(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A client that leaves mid-stream: the real in-flight limiter cancels the downstream;
    the middleware's ``finally`` releases the ticket's mapping, and nothing restored so far
    reaches the cancellation log."""
    mappings = ResponseMappings()
    first_chunk = asyncio.Event()
    hang = asyncio.Event()
    sent: list[Message] = []
    served = {"n": 0}

    async def receive() -> Message:
        served["n"] += 1
        if served["n"] == 1:
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await first_chunk.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(dict(message))
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()

    registered: list[RequestTicket] = []

    async def app(scope: Any, receive_: Any, send_: Any) -> None:
        ticket = current_ticket()
        assert ticket is not None
        registered.append(ticket)
        mappings.register(ticket, MAPPING)
        await DesanitizeMiddleware(sse_app(_stream_for(kind), after_last=hang.wait), mappings)(
            scope, receive_, send_
        )

    limiter = InflightLimiter(0, metrics=NoopExporter(), cancel_grace_s=1.0)

    async def refuse(reason: str) -> None:
        raise AssertionError(reason)

    with caplog.at_level(logging.DEBUG):
        await asyncio.wait_for(limiter.run(_scope(), receive, send, app, refuse=refuse), 5)

    assert registered and registered[0].cancelled
    assert len(mappings) == 0
    assert ORIGINAL_MARK not in caplog.text and QUOTED not in caplog.text


# ── the exception boundary ───────────────────────────────────────────────────


def _raising_restore(payload: Any, mapping: StrategyResult) -> Any:
    raise ValueError(f"{CANARY} {EMAIL}")


def _starlette(response: Callable[[], Any]) -> Starlette:
    async def endpoint(request: Any) -> Any:
        return response()

    # Starlette's own ServerErrorMiddleware wraps this, as it wraps litellm's app.
    return Starlette(routes=[Route("/v1/messages", endpoint, methods=["POST"])])


class _Failures(NoopExporter):
    def __init__(self) -> None:
        self.components: list[str] = []

    def record_failure(self, component: str) -> None:
        self.components.append(component)


def _raise_on(monkeypatch: pytest.MonkeyPatch, needle: str) -> None:
    """Make the SSE restorer fail, with a canary in the message, on the event holding ``needle``."""
    real_feed = desanitize_middleware.SseStreamDesanitizer.feed

    def feed(self: Any, chunk: Any) -> Any:
        if needle in str(chunk):
            raise ValueError(f"{CANARY} {EMAIL}")
        return real_feed(self, chunk)

    monkeypatch.setattr(desanitize_middleware.SseStreamDesanitizer, "feed", feed)


class _UnbuildableDesanitizer:
    def __init__(self, mapping: StrategyResult) -> None:
        raise ValueError(f"{CANARY} {EMAIL}")


async def test_restoration_failure_before_start_is_a_content_free_500(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mappings = ResponseMappings()
    metrics = _Failures()
    ticket = RequestTicket("1" * 32)
    app = _starlette(lambda: JSONResponse(CHAT))

    with caplog.at_level(logging.DEBUG):
        sent = await drive(
            mounted(app, mappings, restore_json=_raising_restore, metrics=metrics), ticket
        )

    assert start_of(sent)["status"] == 500
    assert body_of(sent) == INTERNAL_ERROR_BODY
    assert CANARY not in caplog.text and EMAIL not in caplog.text
    assert "Traceback" not in caplog.text
    assert "gateway_desanitize_failed" in caplog.text and "phase=before_start" in caplog.text
    assert metrics.components == [COMPONENT] == ["desanitize"]
    assert mappings.failed(ticket)
    assert len(mappings) == 0


async def test_a_restorer_that_cannot_be_built_is_a_content_free_500(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Building the SSE restorer compiles a pattern from the originals; a failure there is
    inside the boundary like any other: content-free 500, nothing raised into the app."""
    monkeypatch.setattr(desanitize_middleware, "SseStreamDesanitizer", _UnbuildableDesanitizer)
    mappings = ResponseMappings()
    metrics = _Failures()
    ticket = RequestTicket("2" * 32)

    async def stream() -> Any:
        for chunk in _anthropic_chunks():
            yield chunk

    app = _starlette(lambda: StreamingResponse(stream(), media_type="text/event-stream"))

    with caplog.at_level(logging.DEBUG):
        sent = await drive(mounted(app, mappings, metrics=metrics), ticket)

    assert start_of(sent)["status"] == 500
    assert body_of(sent) == INTERNAL_ERROR_BODY
    assert CANARY not in caplog.text and EMAIL not in caplog.text
    assert "Traceback" not in caplog.text
    assert "phase=before_start" in caplog.text
    assert metrics.components == [COMPONENT]
    assert mappings.failed(ticket)
    assert len(mappings) == 0


async def test_restoration_failure_after_start_closes_the_stream_silently(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fed = {"n": 0}
    real_feed = desanitize_middleware.SseStreamDesanitizer.feed

    def feed(self: Any, chunk: Any) -> Any:
        fed["n"] += 1
        if fed["n"] == 3:
            raise ValueError(f"{CANARY} {EMAIL}")
        return real_feed(self, chunk)

    monkeypatch.setattr(desanitize_middleware.SseStreamDesanitizer, "feed", feed)
    mappings = ResponseMappings()

    async def stream() -> Any:
        for chunk in _anthropic_chunks():
            yield chunk

    app = _starlette(lambda: StreamingResponse(stream(), media_type="text/event-stream"))

    with caplog.at_level(logging.DEBUG):
        sent = await drive(mounted(app, mappings))

    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert bodies[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert CANARY.encode() not in body_of(sent)
    assert CANARY not in caplog.text and EMAIL not in caplog.text
    assert "Traceback" not in caplog.text
    assert "phase=after_start" in caplog.text
    assert len(mappings) == 0


@pytest.mark.parametrize("framing", ["event-per-chunk", "one-chunk"])
async def test_after_start_failure_delivers_what_was_restored_then_closes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, framing: str
) -> None:
    """Events restored before the failing one reach the client, then one clean close; the
    failure is counted as ``gateway_failure{component="desanitize"}`` and marked on the
    ticket for the audit. ``one-chunk`` is the whole stream in one ASGI message, where an
    all-or-nothing feed would hand the client 0 bytes."""
    _raise_on(monkeypatch, PLACEHOLDER_MARK)
    chunks = _anthropic_chunks()
    if framing == "one-chunk":
        chunks = ["".join(chunks)]
    mappings = ResponseMappings()
    metrics = _Failures()
    ticket = RequestTicket("3" * 32)

    with caplog.at_level(logging.DEBUG):
        sent = await drive(mounted(sse_app(chunks), mappings, metrics=metrics), ticket)

    stream = body_of(sent).decode()
    assert start_of(sent)["status"] == 200
    assert _events(stream) == [json.loads(_anthropic_chunks()[0].split("data: ", 1)[1])]
    assert PLACEHOLDER_MARK not in stream and "message_stop" not in stream
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert bodies[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert [bool(m.get("more_body")) for m in bodies].count(False) == 1
    assert metrics.components == [COMPONENT]
    assert mappings.failed(ticket)
    assert len(mappings) == 0
    assert CANARY not in stream
    assert CANARY not in caplog.text and EMAIL not in caplog.text
    assert "Traceback" not in caplog.text


async def test_a_request_that_restored_cleanly_is_not_marked_failed() -> None:
    mappings = ResponseMappings()
    ticket = RequestTicket("4" * 32)

    await drive(mounted(sse_app(_anthropic_chunks()), mappings), ticket)

    assert not mappings.failed(ticket)


async def test_the_failure_reporter_is_injectable_and_gets_no_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raise_on(monkeypatch, PLACEHOLDER_MARK)
    reports: list[tuple[Any, ...]] = []
    ticket = RequestTicket("5" * 32)

    await drive(
        mounted(
            sse_app(_anthropic_chunks()),
            ResponseMappings(),
            on_failure=lambda *args: reports.append(args),
        ),
        ticket,
    )

    assert reports == [(ticket.gateway_id, "after_start", ValueError)]


async def test_a_raising_failure_reporter_still_closes_the_stream(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _raise_on(monkeypatch, PLACEHOLDER_MARK)
    mappings = ResponseMappings()
    ticket = RequestTicket("6" * 32)

    def reporter(request_id: str, phase: str, error_type: type[BaseException]) -> None:
        raise RuntimeError("exporter down")

    with caplog.at_level(logging.DEBUG):
        sent = await drive(
            mounted(sse_app(_anthropic_chunks()), mappings, on_failure=reporter), ticket
        )

    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert bodies[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert mappings.failed(ticket)
    assert "gateway_desanitize_report_failed" in caplog.text and "Traceback" not in caplog.text


def _broken_restore_json(mappings: ResponseMappings) -> ASGIApp:
    return DesanitizeMiddleware(json_app(CHAT), mappings, restore_json=_raising_restore)


def _unbuildable_restorer(mappings: ResponseMappings) -> ASGIApp:
    return DesanitizeMiddleware(sse_app(_anthropic_chunks()), mappings)


@pytest.mark.parametrize(
    "build", [_broken_restore_json, _unbuildable_restorer], ids=["restore-json", "sse-restorer"]
)
async def test_a_failing_client_send_after_a_restoration_failure_carries_no_content(
    monkeypatch: pytest.MonkeyPatch, build: Callable[[ResponseMappings], ASGIApp]
) -> None:
    """The error response is sent outside the handler: if the client's ``send`` then fails
    too, that exception must not hold the canary-carrying failure as ``__context__``."""
    monkeypatch.setattr(desanitize_middleware, "SseStreamDesanitizer", _UnbuildableDesanitizer)
    mappings = ResponseMappings()
    ticket = RequestTicket("e" * 32)
    mappings.register(ticket, MAPPING)
    middleware = build(mappings)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: Message) -> None:
        raise OSError("client gone")

    token = _TICKET.set(ticket)
    try:
        with pytest.raises(OSError) as raised:
            await middleware(_scope(), receive, send)
    finally:
        _TICKET.reset(token)

    assert raised.value.__context__ is None and raised.value.__cause__ is None
    assert len(mappings) == 0


@pytest.fixture
def upstream() -> Iterator[StubUpstream]:
    stub = StubUpstream()
    yield stub
    stub.close()


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
async def test_restoration_failure_never_reaches_litellms_handler(
    monkeypatch: pytest.MonkeyPatch,
    upstream: StubUpstream,
    caplog: pytest.LogCaptureFixture,
    stream: bool,
) -> None:
    """Over litellm's real app: an exception raised in ``send`` would re-enter litellm's
    ``otel_unhandled_exception_handler`` (traceback log + OTEL span, proxy_server.py:
    1755-1760) and starlette's ServerErrorMiddleware. The prototype swallows its own
    failure: no handler record, no traceback, no canary anywhere, content-free client."""
    if stream:
        real_feed = desanitize_middleware.SseStreamDesanitizer.feed

        def feed(self: Any, chunk: Any) -> Any:
            if PLACEHOLDER_MARK in str(chunk):
                raise ValueError(f"{CANARY} {EMAIL}")
            return real_feed(self, chunk)

        monkeypatch.setattr(desanitize_middleware.SseStreamDesanitizer, "feed", feed)
    mappings = ResponseMappings()
    metrics = _Failures()
    engine, _ = build_ours()
    harness = DispatchHarness(
        monkeypatch,
        upstream,
        [OptionAPreCall(engine, mappings)],
        wrap=lambda app: DesanitizeMiddleware(
            app, mappings, restore_json=_raising_restore, metrics=metrics
        ),
    )

    with caplog.at_level(logging.DEBUG):
        exchange = await harness.send("messages", stream=stream)

    assert metrics.components == [COMPONENT]
    assert exchange.ticket is not None and mappings.failed(exchange.ticket)

    if stream:
        assert exchange.status == 200 and ORIGINAL_MARK not in exchange.text
    else:
        assert exchange.status == 500 and exchange.text.encode() == INTERNAL_ERROR_BODY
    phase = "after_start" if stream else "before_start"
    assert "gateway_desanitize_failed request_id=" in caplog.text and phase in caplog.text
    assert "Unhandled exception in request" not in caplog.text
    assert CANARY not in caplog.text and "Traceback" not in caplog.text
    assert len(mappings) == 0


def _long_anthropic(path: str, text: str) -> list[str]:
    events = _dispatch_fixtures._anthropic_sse(text)
    filler = [
        "event: content_block_delta\ndata: "
        + json.dumps(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": f" w{i}"},
            }
        )
        for i in range(30)
    ]
    return [*events[:4], *filler, *events[4:]]


async def test_after_start_failure_tears_the_upstream_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the close, the server's ``receive`` yields ``http.disconnect``; starlette
    cancels litellm's generator and litellm drops the provider connection mid-stream. The
    stub, pacing 34 events 0.1 s apart, sees its connection broken rather than finishing."""
    _raise_on(monkeypatch, PLACEHOLDER_MARK)
    monkeypatch.setattr(_dispatch_fixtures, "_stream", _long_anthropic)
    stub = StubUpstream(event_delay=0.1)
    mappings = ResponseMappings()
    engine, _ = build_ours()
    try:
        harness = DispatchHarness(
            monkeypatch,
            stub,
            [OptionAPreCall(engine, mappings)],
            wrap=lambda app: DesanitizeMiddleware(app, mappings),
        )
        exchange = await harness.send("messages", stream=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while not stub.stream_outcomes and loop.time() < deadline:
            await asyncio.sleep(0.05)
    finally:
        stub.close()

    assert exchange.status == 200 and "message_stop" not in exchange.text
    assert stub.stream_outcomes == ["broken"]


def test_the_middleware_is_wired_in_the_entrypoint_with_no_off_switch() -> None:
    """Imported by ``asgi.py`` (and nothing else under ``src/`` builds one), and it takes
    no flag that would switch restoration off: the security boundary has no off switch."""
    import inspect

    importers = sorted(
        str(path.relative_to(ROOT))
        for path in (ROOT / "src").rglob("*.py")
        if "DesanitizeMiddleware(" in path.read_text() and path.name != "desanitize_middleware.py"
    )
    assert importers == ["src/corp_llm_gateway/asgi.py"]
    assert "enabled" not in inspect.signature(DesanitizeMiddleware).parameters


async def test_outside_a_ticketed_request_it_passes_everything_through() -> None:
    """Only the limiter's requests carry a ticket; anything else is not a rewritten route."""
    mappings = ResponseMappings()
    ticket = RequestTicket("d" * 32)
    mappings.register(ticket, MAPPING)
    middleware = DesanitizeMiddleware(json_app(CHAT), mappings)
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(dict(message))

    await middleware(_scope(), receive, send)

    assert json.loads(body_of(sent)) == CHAT


# ══ Task 2: the hardened, still unwired middleware ════════════════════════════

TOKEN_COUNTS = (7, 3)


def ticketed(app: ASGIApp) -> ASGIApp:
    """Run each request in a fresh ticket, as the in-flight limiter would."""

    async def with_ticket(scope: Any, receive: Any, send: Any) -> None:
        token = _TICKET.set(RequestTicket(uuid.uuid4().hex))
        try:
            await app(scope, receive, send)
        finally:
            _TICKET.reset(token)

    return with_ticket


def byte_app(pieces: list[bytes], *, raise_after: BaseException | None = None) -> ASGIApp:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        for piece in pieces:
            await send({"type": "http.response.body", "body": piece, "more_body": True})
        if raise_after is not None:
            raise raise_after
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    return app


class Records:
    """A terminal-audit sink: what was published, in order."""

    def __init__(self, *failures: BaseException) -> None:
        self.records: list[TerminalRecord] = []
        self.failures = list(failures)

    async def __call__(self, record: TerminalRecord) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.records.append(record)

    @property
    def outcomes(self) -> list[tuple[str, str | None]]:
        return [(r.outcome, r.error_code) for r in self.records]


def facts_for(request_id: str = "call-1") -> AuditFacts:
    return AuditFacts(
        request_id=request_id,
        user_id="alice",
        team_id="t1",
        provider="anthropic",
        model="claude",
        redaction_count=2,
        finding_label_counts={"EMAIL": 1, "NAME": 1},
    )


def with_facts(app: ASGIApp, mappings: ResponseMappings) -> ASGIApp:
    """The pre-call's two effects on the ticket: the response mapping and the audit facts."""

    async def registered(scope: Any, receive: Any, send: Any) -> None:
        ticket = current_ticket()
        assert ticket is not None
        assert mappings.register(ticket, MAPPING)
        assert deposit(ticket, facts_for())
        await app(scope, receive, send)

    return registered


async def run_until_the_client_leaves(limiter: InflightLimiter, app: ASGIApp) -> None:
    """One request through the limiter; the client disconnects once a body byte arrived."""
    first_chunk = asyncio.Event()
    served = {"n": 0}

    async def receive() -> Message:
        served["n"] += 1
        if served["n"] == 1:
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await first_chunk.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()

    async def refuse(reason: str) -> None:
        raise AssertionError(reason)

    await asyncio.wait_for(limiter.run(_scope(), receive, send, app, refuse=refuse), 5)


# ── chat SSE through the installed OpenAI SDK ────────────────────────────────

CHAT_META = {
    "id": "chatcmpl-9",
    "object": "chat.completion.chunk",
    "created": 1758500000,
    "model": "gpt-4o-mini",
}


def chat_chunk(*choices: dict[str, Any]) -> bytes:
    body = json.dumps({**CHAT_META, "choices": list(choices)}, ensure_ascii=False)
    return f"data: {body}\n\n".encode()


def chat_choice(index: int, finish: str | None = None, **delta: Any) -> dict[str, Any]:
    return {"index": index, "delta": delta, "finish_reason": finish}


async def sdk_stream(app: ASGIApp) -> tuple[Any, list[Any]]:
    """``client.chat.completions.stream`` over the ASGI app: the SDK's own SSE decoding,
    chunk building and ``ChatCompletionStreamState`` accumulator."""
    openai = pytest.importorskip("openai")
    import httpx

    transport = httpx.ASGITransport(app=ticketed(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as http:
        client = openai.AsyncOpenAI(
            api_key="sk-test", base_url="http://gateway/v1", http_client=http
        )
        async with client.chat.completions.stream(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
        ) as stream:
            events = [event async for event in stream]
            final = await stream.get_final_completion()
    return final, events


async def test_chat_sse_through_the_middleware_is_what_the_sdk_accumulates() -> None:
    """Two choices, placeholders split inside each, a Cyrillic character split across two
    ASGI messages: the SDK builds every chunk, and each choice's ``content.done`` holds the
    whole restored text."""
    whole = b"".join(
        [
            chat_chunk(
                chat_choice(0, role="assistant", content="Привет, [EM"),
                chat_choice(1, role="assistant", content="b [NA"),
            ),
            chat_chunk(chat_choice(0, content="AIL_1] и [NA"), chat_choice(1, content="ME_1]")),
            chat_chunk(chat_choice(0, content="ME_1]")),
            chat_chunk(chat_choice(0, "stop"), chat_choice(1, "stop")),
            b"data: [DONE]\n\n",
        ]
    )
    cut = whole.index("Привет".encode()) + 1
    mappings = ResponseMappings()

    final, events = await sdk_stream(mounted(byte_app([whole[:cut], whole[cut:]]), mappings))

    assert [c.message.content for c in final.choices] == [
        f"Привет, {EMAIL} и {QUOTED}",
        f"b {QUOTED}",
    ]
    done = sorted(e.content for e in events if e.type == "content.done")
    assert done == sorted([f"Привет, {EMAIL} и {QUOTED}", f"b {QUOTED}"])
    assert len(mappings) == 0


async def test_chat_tool_call_arguments_through_the_middleware_are_complete_in_the_sdk() -> None:
    first = json.dumps({"to": PLACEHOLDER})
    second = json.dumps({"who": NAME})

    def call(index: int, arguments: str, head: bool = False) -> dict[str, Any]:
        entry: dict[str, Any] = {"index": index, "function": {"arguments": arguments}}
        if head:
            entry.update(id=f"call_{index}", type="function")
            entry["function"]["name"] = f"f{index}"
        return entry

    pieces = [
        chat_chunk(chat_choice(0, role="assistant", tool_calls=[call(0, "", head=True)])),
        chat_chunk(chat_choice(0, tool_calls=[call(0, first[:10])])),
        chat_chunk(chat_choice(0, tool_calls=[call(0, first[10:])])),
        chat_chunk(chat_choice(0, tool_calls=[call(1, "", head=True)])),
        chat_chunk(chat_choice(0, tool_calls=[call(1, second[:11])])),
        chat_chunk(chat_choice(0, tool_calls=[call(1, second[11:])])),
        chat_chunk(chat_choice(0, "tool_calls")),
        b"data: [DONE]\n\n",
    ]

    final, events = await sdk_stream(mounted(byte_app(pieces), ResponseMappings()))

    args = [json.loads(c.function.arguments) for c in final.choices[0].message.tool_calls]
    assert args == [{"to": EMAIL}, {"who": QUOTED}]
    done = [
        json.loads(e.arguments) for e in events if e.type == "tool_calls.function.arguments.done"
    ]
    assert done == [{"to": EMAIL}, {"who": QUOTED}]


# ── Responses terminal ordering ──────────────────────────────────────────────


def _responses_frames(*events: dict[str, Any]) -> list[str]:
    return [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events]


_TEXT_IDS = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
_HELD = [
    {"type": "response.output_text.delta", "sequence_number": 1, **_TEXT_IDS, "delta": "mail [EM"},
    {"type": "response.output_text.delta", "sequence_number": 2, **_TEXT_IDS, "delta": "AIL_1]"},
]
_ENDINGS: dict[str, list[str]] = {
    "done": ["data: [DONE]\n\n"],
    "completed": [
        *_responses_frames(
            {"type": "response.completed", "sequence_number": 3, "response": RESPONSES}
        ),
        "data: [DONE]\n\n",
    ],
    "failed": _responses_frames(
        {"type": "response.failed", "sequence_number": 3, "response": {"id": "resp_1"}}
    ),
    "error": _responses_frames(
        {"type": "error", "sequence_number": 3, "code": "server_error", "message": "gone"}
    ),
}


@pytest.mark.parametrize("ending", sorted(_ENDINGS))
async def test_responses_tails_go_out_before_the_end_never_after(ending: str) -> None:
    """No ``output_text.done`` arrived: the held tail must precede ``[DONE]``, the terminal
    ``response.*`` event and an ``error`` event, the points where a client stops reading."""
    mappings = ResponseMappings()
    records = Records()
    chunks = [*_responses_frames(*_HELD), *_ENDINGS[ending]]

    sent = await drive(
        DesanitizeMiddleware(
            with_facts(sse_app(chunks), mappings),
            mappings,
            terminal=TerminalAudit(records),
        )
    )

    stream = body_of(sent).decode()
    ordered = _ordered(stream)
    end = next(i for i, e in enumerate(ordered) if _type_of(e) == _type(ending))
    head, tail = ordered[:end], ordered[end:]
    assert "".join(e.get("delta", "") for e in head) == f"mail {EMAIL}"
    assert not any(_type_of(e) == "response.output_text.delta" for e in tail)
    assert PLACEHOLDER_MARK not in stream
    ok = ending in ("done", "completed")
    assert records.outcomes == [("ok", None) if ok else ("failed", None)]


def _type(ending: str) -> str:
    return {"done": "[DONE]", "error": "error"}.get(ending, f"response.{ending}")


def _type_of(event: Any) -> str:
    return event if isinstance(event, str) else str(event.get("type"))


def _ordered(stream: str) -> list[Any]:
    """Every ``data:`` payload in order; ``[DONE]`` as the string."""
    out: list[Any] = []
    for line in stream.splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


_PROVIDER_ERRORS: dict[str, tuple[list[str], str]] = {
    "chat": (
        [
            "data: "
            + json.dumps(
                {**CHAT_META, "choices": [{"index": 0, "delta": {"content": f"to {PLACEHOLDER}"}}]}
            )
            + "\n\n",
            'data: {"error": {"message": "provider failed", "code": "500"}}\n\n',
        ],
        'data: {"error"',
    ),
    "anthropic": (
        [
            *_anthropic_chunks()[:1],
            "event: content_block_delta\ndata: "
            + json.dumps(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": f"to {PLACEHOLDER}"},
                }
            )
            + "\n\n",
            'event: error\ndata: {"type": "error", "error": {"type": "overloaded_error"}}\n\n',
        ],
        "event: error",
    ),
    "responses": (
        [
            *_responses_frames(
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    **_TEXT_IDS,
                    "delta": f"to {PLACEHOLDER}",
                },
                {"type": "error", "sequence_number": 2, "code": "server_error", "message": "gone"},
            )
        ],
        "event: error",
    ),
}


@pytest.mark.parametrize("family", sorted(_PROVIDER_ERRORS))
async def test_a_provider_error_mid_stream_flushes_the_held_tail_first(family: str) -> None:
    """litellm turns a provider exception mid-stream into an error event and ends the
    stream; the callback path flushes tails before it propagates (litellm_hook.py
    ``_post_call_stream_impl``), and so does the middleware."""
    chunks, marker = _PROVIDER_ERRORS[family]
    mappings = ResponseMappings()
    records = Records()

    sent = await drive(
        DesanitizeMiddleware(
            with_facts(sse_app(chunks), mappings),
            mappings,
            terminal=TerminalAudit(records),
        )
    )

    stream = body_of(sent).decode()
    at = stream.index(marker)
    assert _stream_text(stream[:at]) == f"to {EMAIL}"
    assert _stream_text(stream[at:]) == ""
    assert PLACEHOLDER not in stream
    assert records.outcomes == [("failed", None)]
    assert len(mappings) == 0


def _stream_text(stream: str) -> str:
    """The model text of any family's events, in order."""
    out: list[str] = []
    for event in _ordered(stream):
        if not isinstance(event, dict):
            continue
        for choice in event.get("choices") or []:
            out.append(choice.get("delta", {}).get("content") or "")
        delta = event.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            out.append(delta["text"])
        elif event.get("type") == "response.output_text.delta":
            out.append(event["delta"])
    return "".join(out)


async def test_an_app_that_raises_mid_stream_still_gets_the_restored_tail_out() -> None:
    """Parity with the callback path: tails first, then the app's own exception, untouched."""
    held = (
        "data: "
        + json.dumps(
            {**CHAT_META, "choices": [{"index": 0, "delta": {"content": f"to {PLACEHOLDER}"}}]}
        )
        + "\n\n"
    )
    mappings = ResponseMappings()
    records = Records()
    boom = RuntimeError("upstream transport broke")
    ticket = RequestTicket("7" * 32)
    middleware = DesanitizeMiddleware(
        with_facts(byte_app([held.encode()], raise_after=boom), mappings),
        mappings,
        terminal=TerminalAudit(records),
    )

    sent: list[Message] = []

    with pytest.raises(RuntimeError) as raised:
        await drive(middleware, ticket, sent)

    assert raised.value is boom
    assert _stream_text(body_of(sent).decode()) == f"to {EMAIL}"
    assert all(m.get("more_body") for m in sent if m["type"] == "http.response.body")
    assert len(mappings) == 0
    assert records.outcomes == [("failed", None)]


async def test_the_app_error_stays_the_error_when_the_tail_send_fails_too() -> None:
    held = (
        "data: "
        + json.dumps({"choices": [{"index": 0, "delta": {"content": f"to {PLACEHOLDER}"}}]})
        + "\n\n"
    )
    mappings = ResponseMappings()
    boom = RuntimeError("upstream transport broke")
    ticket = RequestTicket("8" * 32)
    mappings.register(ticket, MAPPING)
    sends = {"n": 0}

    async def receive() -> Message:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: Message) -> None:
        sends["n"] += 1
        # The start and the restored head go out; the tail after the app's error does not.
        if sends["n"] > 2:
            raise OSError("client gone")

    token = _TICKET.set(ticket)
    try:
        with pytest.raises(RuntimeError) as raised:
            await DesanitizeMiddleware(byte_app([held.encode()], raise_after=boom), mappings)(
                _scope(), receive, send
            )
    finally:
        _TICKET.reset(token)

    assert raised.value is boom
    assert sends["n"] == 3
    assert len(mappings) == 0


# ── mapping lifecycle ────────────────────────────────────────────────────────


def _text_app(status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> ASGIApp:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return app


_GZIPPED = __import__("gzip").compress(json.dumps(CHAT).encode())
_MODES: dict[str, ASGIApp] = {
    "2xx-json": json_app(CHAT),
    "2xx-sse": sse_app(_anthropic_chunks()),
    "non-2xx": json_app({"error": {"message": f"no {PLACEHOLDER}"}}, status=429),
    "non-json": _text_app(200, [(b"content-type", b"text/plain")], f"hi {PLACEHOLDER}".encode()),
    "gzip": _text_app(
        200, [(b"content-type", b"application/json"), (b"content-encoding", b"gzip")], _GZIPPED
    ),
}


@pytest.mark.parametrize("mode", sorted(_MODES))
async def test_the_mapping_goes_on_the_final_body_in_every_mode(mode: str) -> None:
    """Not only in ``finally``: a response that finished while its app keeps running (a
    pass-through, an error) holds no mapping past its final body."""
    mappings = ResponseMappings()
    seen_after: list[bool] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _MODES[mode](scope, receive, send)
        seen_after.append(current_ticket() in mappings)

    sent = await drive(mounted(app, mappings))

    assert seen_after == [False]
    if mode == "gzip":
        assert body_of(sent) == _GZIPPED
        assert header(start_of(sent), b"content-encoding") == b"gzip"


@pytest.mark.parametrize("state", ["cancelled", "closed"])
async def test_a_mapping_is_refused_for_a_cancelled_or_closed_ticket(
    state: str, caplog: pytest.LogCaptureFixture
) -> None:
    ticket = RequestTicket("9" * 32)
    if state == "cancelled":
        ticket.cancelled = True
    else:
        ticket.close()
    mappings = ResponseMappings()

    with caplog.at_level(logging.DEBUG):
        registered = mappings.register(ticket, MAPPING)

    assert registered is False and len(mappings) == 0
    assert "gateway_desanitize_register_refused" in caplog.text and f"reason={state}" in caplog.text
    assert ORIGINAL_MARK not in caplog.text and QUOTED not in caplog.text


async def test_closing_the_ticket_releases_the_mapping_and_the_restorer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The limiter's close reaches a request whose downstream never returns: the mapping
    and every buffer holding restored text are gone at once."""
    restorers: list[weakref.ref[Any]] = []

    class Tracked(desanitize_middleware._SseRestorer):
        def __init__(self, mapping: StrategyResult) -> None:
            super().__init__(mapping)
            restorers.append(weakref.ref(self))

    monkeypatch.setattr(desanitize_middleware, "_SseRestorer", Tracked)
    mappings = ResponseMappings()
    ticket = RequestTicket("a1" * 16)
    mapped = StrategyResult(pairs=MAPPING.pairs)
    mapping_ref = weakref.ref(mapped)
    mappings.register(ticket, mapped)
    del mapped
    hang = asyncio.Event()
    middleware = DesanitizeMiddleware(
        sse_app(_stream_for("responses"), after_last=hang.wait), mappings
    )
    task = asyncio.create_task(drive(middleware, ticket))
    while not restorers:
        await asyncio.sleep(0.01)

    ticket.close()
    gc.collect()

    assert len(mappings) == 0
    assert restorers[0]() is None and mapping_ref() is None
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_cancellation_resistant_requests_leave_nothing_behind(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """N requests whose downstream ignores cancellation, under the production task factory
    and cancel hook: after each disconnect the store is empty, no restorer or mapping
    survives, the guardrail holds no state, and each has one ``cancelled`` terminal record."""
    restorers: list[weakref.ref[Any]] = []
    mapping_refs: list[weakref.ref[Any]] = []

    class Tracked(desanitize_middleware._SseRestorer):
        def __init__(self, mapping: StrategyResult) -> None:
            super().__init__(mapping)
            restorers.append(weakref.ref(self))

    class TrackedMappings(ResponseMappings):
        def register(self, ticket: RequestTicket, mapping: StrategyResult) -> bool:
            mapping_refs.append(weakref.ref(mapping))
            return super().register(ticket, mapping)

    monkeypatch.setattr(desanitize_middleware, "_SseRestorer", Tracked)
    restore_factory = install_task_factory(asyncio.get_running_loop())
    guardrail, sink = build_ours()
    limiter = InflightLimiter(0, metrics=NoopExporter(), cancel_grace_s=0.05)
    limiter.bind_cancel_hook(guardrail.on_request_cancelled)
    mappings = TrackedMappings()
    guardrail.bind_response_mappings(mappings)
    records = Records()
    terminal = TerminalAudit(records)
    stop = asyncio.Event()
    sizes: list[int] = []

    async def resist() -> None:
        while not stop.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue

    async def app(scope: Any, receive: Any, send: Any) -> None:
        data = _data_with_token("tok-1", content=f"write to {EMAIL}")
        data["litellm_call_id"] = f"call-{uuid.uuid4().hex}"
        await guardrail.pre_call(data)
        sizes.append(len(mappings))
        stream = DesanitizeMiddleware(
            sse_app(_stream_for("anthropic"), after_last=resist),
            mappings,
            terminal=terminal,
        )
        await stream(scope, receive, send)

    n = 5
    try:
        with caplog.at_level(logging.DEBUG):
            for _ in range(n):
                await run_until_the_client_leaves(limiter, app)
                await terminal.drain()
                gc.collect()
                assert len(mappings) == 0
                assert all(ref() is None for ref in restorers)
                assert all(ref() is None for ref in mapping_refs)
                assert guardrail._req_state == {}
    finally:
        stop.set()
        await asyncio.sleep(0.05)
        restore_factory()

    assert sizes == [1] * n and len(restorers) == n
    # One record per request: the ticket's; the guardrail's cancel hook only clears state.
    assert records.outcomes == [("cancelled", "E_CLIENT_DISCONNECTED")] * n
    assert sink.records == []
    assert ORIGINAL_MARK not in caplog.text


# ── the terminal-audit contract through the middleware ───────────────────────


async def test_a_callback_before_the_response_adds_to_the_one_record() -> None:
    """litellm's deferred success log can run before ``http.response.start``: it deposits
    and never publishes; the middleware publishes at the final body."""
    mappings = ResponseMappings()
    records = Records()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        assert deposit_usage(current_ticket(), *TOKEN_COUNTS)
        assert records.records == []
        await json_app(CHAT)(scope, receive, send)

    sent = await drive(
        DesanitizeMiddleware(with_facts(app, mappings), mappings, terminal=TerminalAudit(records))
    )

    assert EMAIL in body_of(sent).decode()
    (record,) = records.records
    event = record.event()
    assert record.outcome == "ok"
    assert (event.prompt_token_count, event.completion_token_count) == TOKEN_COUNTS
    assert event.redaction_count == 2 and event.finding_label_counts == {"EMAIL": 1, "NAME": 1}


async def test_a_callback_after_the_response_changes_nothing() -> None:
    mappings = ResponseMappings()
    records = Records()
    late: list[bool] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await json_app(CHAT)(scope, receive, send)
        assert [r.outcome for r in records.records] == ["ok"]
        late.append(deposit_usage(current_ticket(), *TOKEN_COUNTS))

    ticket = RequestTicket("b1" * 16)
    terminal = TerminalAudit(records)
    await drive(
        DesanitizeMiddleware(with_facts(app, mappings), mappings, terminal=terminal),
        ticket,
    )
    ticket.close()
    await terminal.drain()

    assert late == [False]
    (record,) = records.records
    assert record.event().prompt_token_count == 0 and record.event().redaction_count == 2


@pytest.mark.parametrize("phase", ["before_start", "after_start"])
async def test_a_restoration_failure_is_one_failed_internal_record(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Matches ``_report_internal_failure``: ``status="failed"``, ``error_code="E_INTERNAL"``."""
    mappings = ResponseMappings()
    records = Records()
    if phase == "before_start":
        inner = json_app(CHAT)
        middleware = DesanitizeMiddleware(
            with_facts(inner, mappings),
            mappings,
            restore_json=_raising_restore,
            terminal=TerminalAudit(records),
        )
    else:
        _raise_on(monkeypatch, PLACEHOLDER_MARK)
        middleware = DesanitizeMiddleware(
            with_facts(sse_app(_anthropic_chunks()), mappings),
            mappings,
            terminal=TerminalAudit(records),
        )
    ticket = RequestTicket("c1" * 16)

    await drive(middleware, ticket)
    ticket.close()

    assert records.outcomes == [("failed", "E_INTERNAL")]
    assert records.records[0].event().redaction_count == 2


async def test_a_cancel_before_the_final_body_takes_precedence_over_ok() -> None:
    """The client left mid-stream: the limiter cancels, the ticket's close publishes
    ``cancelled``; nothing publishes ``ok``, whatever order the rest runs in."""
    mappings = ResponseMappings()
    records = Records()
    terminal = TerminalAudit(records)
    first_chunk = asyncio.Event()
    served = {"n": 0}
    hang = asyncio.Event()

    async def receive() -> Message:
        served["n"] += 1
        if served["n"] == 1:
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await first_chunk.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk.set()

    async def app(scope: Any, receive_: Any, send_: Any) -> None:
        await DesanitizeMiddleware(
            with_facts(sse_app(_stream_for("chat"), after_last=hang.wait), mappings),
            mappings,
            terminal=terminal,
        )(scope, receive_, send_)

    async def refuse(reason: str) -> None:
        raise AssertionError(reason)

    limiter = InflightLimiter(0, metrics=NoopExporter(), cancel_grace_s=0.5)
    await asyncio.wait_for(limiter.run(_scope(), receive, send, app, refuse=refuse), 5)
    await terminal.drain()

    assert records.outcomes == [("cancelled", "E_CLIENT_DISCONNECTED")]


async def test_a_failed_terminal_write_is_retried_at_close_and_written_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mappings = ResponseMappings()
    records = Records(RuntimeError(f"sink down {CANARY}"))
    terminal = TerminalAudit(records)
    ticket = RequestTicket("d1" * 16)

    with caplog.at_level(logging.DEBUG):
        await drive(
            DesanitizeMiddleware(with_facts(json_app(CHAT), mappings), mappings, terminal=terminal),
            ticket,
        )
        assert records.records == []
        ticket.close()
        await terminal.drain()

    assert records.outcomes == [("ok", None)]
    assert CANARY not in caplog.text


# The stub's usage (7 in, 2 out) on every flow; on chat SSE from the usage chunk the
# client did not ask for, which the middleware reads and drops.
STUB_USAGE = {
    (route, stream): (7, 2)
    for route in ("chat", "messages", "responses")
    for stream in (False, True)
}


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("route", ["chat", "messages", "responses"])
async def test_over_litellms_app_the_terminal_record_keeps_todays_counts(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, route: str, stream: bool
) -> None:
    """Our real pre-call deposits the facts; the terminal record carries the same identity
    and counts as the record today's ``audit()`` writes for the same request."""
    mappings = ResponseMappings()
    records = Records()
    terminal = TerminalAudit(records)
    engine, sink = build_ours()
    harness = DispatchHarness(
        monkeypatch,
        upstream,
        [OptionAPreCall(engine, mappings)],
        wrap=lambda app: DesanitizeMiddleware(app, mappings, terminal=terminal),
    )

    exchange = await harness.send(route, stream=stream)
    await terminal.drain()

    assert exchange.status == 200 and ORIGINAL_MARK in exchange.text
    (record,) = records.records
    # The guardrail writes nothing of its own: exactly one record per request.
    assert sink.records == []
    event = record.event()
    assert record.outcome == "ok"
    assert event.request_id == exchange.ticket.call_ids[0]  # type: ignore[union-attr]
    assert (event.user_id, event.team_id) == ("alice", "t1")
    assert event.redaction_count == 1
    assert event.finding_label_counts == {"EMAIL": 1}
    assert event.placeholder_list == (PLACEHOLDER,)
    # Read off the response by the middleware, whenever litellm's log runs.
    assert (event.prompt_token_count, event.completion_token_count) == STUB_USAGE[(route, stream)]
    if (route, stream) == ("chat", True):
        assert '"usage"' not in exchange.text
    assert len(mappings) == 0


async def test_chat_sse_usage_reaches_the_record_when_the_client_asks_for_it(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream
) -> None:
    """``stream_options.include_usage``: the provider's usage chunk passes litellm and the
    middleware reads it; the record carries it (and exactly once: never summed with a
    success log that might have run first)."""
    mappings = ResponseMappings()
    records = Records()
    engine, sink = build_ours()
    harness = DispatchHarness(
        monkeypatch,
        upstream,
        [OptionAPreCall(engine, mappings)],
        wrap=lambda app: DesanitizeMiddleware(app, mappings, terminal=TerminalAudit(records)),
    )

    exchange = await harness.send(
        "chat", stream=True, extra={"stream_options": {"include_usage": True}}
    )

    assert exchange.status == 200 and ORIGINAL_MARK in exchange.text
    assert exchange.text.count('"usage"') == 1
    (record,) = records.records
    event = record.event()
    assert (event.prompt_token_count, event.completion_token_count) == (7, 2)
    assert sink.records == []


# ── boundaries, across the three families ────────────────────────────────────

FAIL_HERE = "FAIL-HERE-3c2"


def _family_stream(family: str) -> list[str]:
    """A restored placeholder, then an event carrying ``FAIL_HERE``, then more."""
    if family == "chat":

        def c(text: str) -> str:
            return (
                "data: "
                + json.dumps({**CHAT_META, "choices": [{"index": 0, "delta": {"content": text}}]})
                + "\n\n"
            )

        return [
            c(f"to {PLACEHOLDER}. "),
            c(f"{'x' * 12} ok. "),
            c(FAIL_HERE),
            c("never"),
            "data: [DONE]\n\n",
        ]
    if family == "anthropic":

        def d(text: str) -> str:
            return (
                "event: content_block_delta\ndata: "
                + json.dumps(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": text},
                    }
                )
                + "\n\n"
            )

        return [
            _anthropic_chunks()[0],
            d(f"to {PLACEHOLDER}. "),
            d("x" * 12 + " ok. "),
            d(FAIL_HERE),
            d("never"),
        ]
    return _responses_frames(
        *(
            {"type": "response.output_text.delta", "sequence_number": i, **_TEXT_IDS, "delta": text}
            for i, text in enumerate(
                [f"to {PLACEHOLDER}. ", "x" * 12 + " ok. ", FAIL_HERE, "never"]
            )
        )
    )


def _fail_on_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    real = desanitize_middleware._SseRestorer._event

    def event(self: Any, text: str) -> list[str]:
        if FAIL_HERE in text:
            raise ValueError(f"{CANARY} {EMAIL}")
        return real(self, text)

    monkeypatch.setattr(desanitize_middleware._SseRestorer, "_event", event)


@pytest.mark.parametrize("family", ["chat", "anthropic", "responses"])
async def test_restored_text_reaches_the_client_before_a_later_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, family: str
) -> None:
    _fail_on_marker(monkeypatch)
    mappings = ResponseMappings()
    metrics = _Failures()

    with caplog.at_level(logging.DEBUG):
        sent = await drive(mounted(sse_app(_family_stream(family)), mappings, metrics=metrics))

    stream = body_of(sent).decode()
    assert _stream_text(stream).startswith(f"to {EMAIL}")
    assert FAIL_HERE not in stream and "never" not in stream and CANARY not in stream
    assert [bool(m.get("more_body")) for m in sent if m["type"] == "http.response.body"][
        -1
    ] is False
    assert metrics.components == [COMPONENT]
    assert CANARY not in caplog.text and "Traceback" not in caplog.text
    assert len(mappings) == 0


class _UnbuildableResponses:
    def __init__(self, mapping: StrategyResult) -> None:
        raise ValueError(f"{CANARY} {EMAIL}")


@pytest.mark.parametrize("adapter", ["SseStreamDesanitizer", "ResponsesStreamDesanitizer"])
@pytest.mark.parametrize("family", ["chat", "anthropic", "responses"])
async def test_an_adapter_that_cannot_be_built_is_a_content_free_500_for_every_family(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, adapter: str, family: str
) -> None:
    monkeypatch.setattr(desanitize_middleware, adapter, _UnbuildableResponses)
    mappings = ResponseMappings()

    with caplog.at_level(logging.DEBUG):
        sent = await drive(mounted(sse_app(_family_stream(family)), mappings))

    assert start_of(sent)["status"] == 500 and body_of(sent) == INTERNAL_ERROR_BODY
    assert CANARY not in caplog.text and "phase=before_start" in caplog.text
    assert len(mappings) == 0


@pytest.mark.parametrize("family", ["chat", "anthropic", "responses"])
async def test_a_flush_failure_delivers_what_was_restored_and_closes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, family: str
) -> None:
    def flush(self: Any) -> None:
        raise ValueError(f"{CANARY} {EMAIL}")

    monkeypatch.setattr(desanitize_middleware._SseRestorer, "flush", flush)
    mappings = ResponseMappings()
    chunks = [c for c in _family_stream(family) if FAIL_HERE not in c]

    with caplog.at_level(logging.DEBUG):
        sent = await drive(mounted(sse_app(chunks), mappings))

    stream = body_of(sent).decode()
    assert _stream_text(stream).startswith(f"to {EMAIL}") and CANARY not in stream
    assert "phase=after_start" in caplog.text and CANARY not in caplog.text
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
    assert len(mappings) == 0


def _chain(exc: BaseException | None) -> list[BaseException]:
    seen: list[BaseException] = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


@pytest.mark.parametrize("fail_at", ["restored-chunk", "final-body", "reporter-then-send"])
async def test_a_client_send_failing_after_an_after_start_failure_chains_no_content(
    monkeypatch: pytest.MonkeyPatch, fail_at: str
) -> None:
    _fail_on_marker(monkeypatch)
    mappings = ResponseMappings()
    ticket = RequestTicket("e1" * 16)
    mappings.register(ticket, MAPPING)
    bodies = {"n": 0}

    def reporter(request_id: str, phase: str, error_type: type[BaseException]) -> None:
        raise RuntimeError(f"exporter down {CANARY}")

    async def receive() -> Message:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] != "http.response.body":
            return
        bodies["n"] += 1
        final = not message.get("more_body")
        if fail_at == "restored-chunk" and message.get("body") and bodies["n"] > 1:
            raise OSError("client gone")
        if fail_at in ("final-body", "reporter-then-send") and final:
            raise OSError("client gone")

    middleware = DesanitizeMiddleware(
        sse_app(_family_stream("anthropic")),
        mappings,
        on_failure=reporter if fail_at == "reporter-then-send" else None,
        metrics=_Failures(),
    )
    token = _TICKET.set(ticket)
    try:
        with pytest.raises(OSError) as raised:
            await middleware(_scope(), receive, send)
    finally:
        _TICKET.reset(token)

    chain = _chain(raised.value)
    assert all(CANARY not in repr(e) and EMAIL not in repr(e) for e in chain)
    assert raised.value.__context__ is None and raised.value.__cause__ is None
    assert len(mappings) == 0


@pytest.fixture
def failing_upstream() -> Iterator[StubUpstream]:
    stub = StubUpstream(error_status=500)
    yield stub
    stub.close()


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("route", ["chat", "messages", "responses"])
async def test_a_provider_error_after_the_mapping_was_registered_passes_and_releases(
    monkeypatch: pytest.MonkeyPatch, failing_upstream: StubUpstream, route: str, stream: bool
) -> None:
    mappings = ResponseMappings()
    records = Records()
    terminal = TerminalAudit(records)
    engine, _ = build_ours()
    harness = DispatchHarness(
        monkeypatch,
        failing_upstream,
        [OptionAPreCall(engine, mappings)],
        wrap=lambda app: DesanitizeMiddleware(app, mappings, terminal=terminal),
    )

    exchange = await harness.send(route, stream=stream)
    await terminal.drain()

    assert exchange.status >= 400 and ORIGINAL_MARK not in exchange.text
    assert failing_upstream.bodies and all(ORIGINAL_MARK not in b for b in failing_upstream.bodies)
    assert len(mappings) == 0
    assert [r.outcome for r in records.records] == ["failed"]


def _thinking_stream() -> list[str]:
    def ev(data: dict[str, Any]) -> str:
        return f"event: {data['type']}\ndata: {json.dumps(data)}\n\n"

    return [
        ev(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            }
        ),
        ev(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": f"user {PLACEHOLDER}"},
            }
        ),
        ev(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "c2ln"},
            }
        ),
        ev({"type": "content_block_stop", "index": 0}),
        ev(
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "tool_use", "id": "t", "name": "mail", "input": {}},
            }
        ),
        ev(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"to": "[NA'},
            }
        ),
        ev(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": 'ME_1]"}'},
            }
        ),
        ev({"type": "content_block_stop", "index": 1}),
    ]


async def test_thinking_and_its_signature_pass_through_and_tool_input_is_restored() -> None:
    """Anthropic signs thinking blocks: they go out as received, placeholder and all;
    the tool input beside them is restored and stays valid JSON."""
    chunks = _thinking_stream()

    sent = await drive(mounted(sse_app(chunks), ResponseMappings()))

    stream = body_of(sent).decode()
    for event in chunks[:4]:
        assert event in stream
    partial = "".join(
        e["delta"]["partial_json"]
        for e in _events(stream)
        if e.get("delta", {}).get("type") == "input_json_delta"
    )
    assert json.loads(partial) == {"to": QUOTED}


async def test_a_unary_thinking_block_passes_through_untouched() -> None:
    payload = {
        **ANTHROPIC,
        "content": [
            {"type": "thinking", "thinking": f"user {PLACEHOLDER}", "signature": "c2ln"},
            {"type": "text", "text": f"mail {PLACEHOLDER}"},
        ],
    }

    sent = await drive(mounted(json_app(payload), ResponseMappings()))

    body = json.loads(body_of(sent))
    assert body["content"][0] == payload["content"][0]
    assert body["content"][1]["text"] == f"mail {EMAIL}"


def test_litellms_app_carries_no_compressor() -> None:
    """The Content-Encoding pass-through is only safe with nothing compressing upstream of
    the middleware: litellm's own app adds none."""
    from litellm.proxy import proxy_server

    assert compressor_problems(proxy_server.app) == []


def test_a_compressor_on_the_app_is_detected() -> None:
    from starlette.middleware.gzip import GZipMiddleware

    app = Starlette()
    app.add_middleware(GZipMiddleware)

    assert compressor_problems(app) == ["GZipMiddleware"]


# ── pins ─────────────────────────────────────────────────────────────────────


def test_head_never_reaches_a_rewritten_verdict_on_any_shipped_route() -> None:
    """A HEAD response has a Content-Length and no body: were it ever REWRITTEN, the unary
    path would parse an empty body. Every path the tables make REWRITTEN refuses HEAD."""
    from corp_llm_gateway.route_gate import LITELLM_ROUTE_TABLE, Verdict, classify
    from corp_llm_gateway.route_gate.table import GATEWAY_ROUTE_TABLE

    rewritten = {
        path
        for table in (LITELLM_ROUTE_TABLE, GATEWAY_ROUTE_TABLE)
        for (_, path), entry in table.items()
        if entry.verdict is Verdict.REWRITTEN
    }
    assert rewritten
    for path in sorted(rewritten):
        assert classify("HEAD", path, path.encode()).verdict is not Verdict.REWRITTEN, path


async def test_restoration_does_not_depend_on_cache_b(monkeypatch: pytest.MonkeyPatch) -> None:
    """The middleware restores from the mapping the pre-call handed it; Cache B (the
    conversation store) can expire in between."""
    from corp_llm_gateway.storage import in_memory

    guardrail, _ = build_ours()
    mappings = ResponseMappings()
    guardrail.bind_response_mappings(mappings)
    ticket = RequestTicket("f1" * 16)
    token = _TICKET.set(ticket)
    try:
        data = _data_with_token("tok-1", content=f"write to {EMAIL}")
        data["litellm_call_id"] = "call-cache-b"
        await guardrail.pre_call(data)
    finally:
        _TICKET.reset(token)
    store = guardrail.orchestrator._mapping_store  # type: ignore[union-attr]
    held = [key for key in store._p2o if key[1] == PLACEHOLDER]
    assert held, "the pre-call wrote no Cache B entry; the test proves nothing"
    real_now = in_memory._now
    monkeypatch.setattr(in_memory, "_now", lambda: real_now() + 10 * 24 * 3600)
    for conversation, placeholder in held:
        assert await store.get_original(conversation, placeholder) is None
    guardrail._req_state.clear()

    sent = await drive(DesanitizeMiddleware(json_app(CHAT), mappings), ticket)

    assert EMAIL in body_of(sent).decode()
