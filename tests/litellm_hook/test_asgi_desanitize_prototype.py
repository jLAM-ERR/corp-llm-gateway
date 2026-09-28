"""Plan 20260926 Task 0: the Option A prototype, ``route_gate/desanitize_middleware.py``.

Stub ASGI apps emit litellm's three wire formats (unary JSON, chat / Anthropic SSE,
Responses SSE); the last group mounts the prototype over litellm's real app. The three
rev 5 contracts are what these tests pin: ticket-keyed state released by the middleware,
never by ``audit()``; a content-free exception boundary; a wire-format adapter per shape.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
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
)
from corp_llm_gateway.route_gate.inflight import (
    _TICKET,
    InflightLimiter,
    RequestTicket,
    current_ticket,
)
from corp_llm_gateway.sanitizer.strategies import StrategyResult
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
    register_response_mapping,
)
from tests.test_litellm_hook import _data_with_token

ROOT = Path(__file__).resolve().parents[2]
QUOTED = 'Dan "the man" O\'Neil'
NAME = "[NAME_1]"
MAPPING = StrategyResult(pairs=((EMAIL, PLACEHOLDER), (QUOTED, NAME)))
CANARY = "CANARY-8d1f"

Message = dict[str, Any]
ASGIApp = Callable[[Any, Any, Any], Awaitable[None]]


# ── driving one request ──────────────────────────────────────────────────────


async def drive(app: ASGIApp, ticket: RequestTicket | None = None) -> list[Message]:
    """One HTTP request, in the ticket's context as the in-flight limiter runs it."""
    sent: list[Message] = []
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
    middleware = DesanitizeMiddleware(app, mappings, enabled=True, **kwargs)

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
    middleware = DesanitizeMiddleware(slow, mappings, enabled=True)

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
    header hook and the ASGI send. Here ``audit()`` completes first: ``_mark_audited`` pops
    ``_req_state``, so today's callback reversal would find no state and return the
    placeholders; the middleware still restores from the mapping it owns.
    """
    guardrail, sink = build_ours()
    mappings = ResponseMappings()
    ticket = RequestTicket("c" * 32)
    token = _TICKET.set(ticket)
    try:
        data = _data_with_token("tok-1", content=f"write to {EMAIL}")
        data["litellm_call_id"] = "call-1"
        await guardrail.pre_call(data)
        register_response_mapping(guardrail, mappings, data)
    finally:
        _TICKET.reset(token)
    assert ticket in mappings
    now = datetime.now(UTC)

    async def audited_then_answers(scope: Any, receive: Any, send: Any) -> None:
        await guardrail.audit(data, None, now, now, status="ok")
        assert "call-1" not in guardrail._req_state
        unrestored = await guardrail.post_call_unary(data, dict(CHAT))
        assert unrestored == CHAT
        await json_app(CHAT)(scope, receive, send)

    sent = await drive(DesanitizeMiddleware(audited_then_answers, mappings, enabled=True), ticket)

    assert [r["status"] for r in sink.records] == ["ok"]
    assert EMAIL in body_of(sent).decode()
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
        await DesanitizeMiddleware(
            sse_app(_stream_for(kind), after_last=hang.wait), mappings, enabled=True
        )(scope, receive_, send_)

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
    return DesanitizeMiddleware(
        json_app(CHAT), mappings, enabled=True, restore_json=_raising_restore
    )


def _unbuildable_restorer(mappings: ResponseMappings) -> ASGIApp:
    return DesanitizeMiddleware(sse_app(_anthropic_chunks()), mappings, enabled=True)


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
            app, mappings, enabled=True, restore_json=_raising_restore, metrics=metrics
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
            wrap=lambda app: DesanitizeMiddleware(app, mappings, enabled=True),
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


def test_the_prototype_is_not_wired() -> None:
    """Flagged off by default and imported by nothing under ``src/``."""
    importers = [
        path.relative_to(ROOT)
        for path in (ROOT / "src").rglob("*.py")
        if "desanitize_middleware" in path.read_text() and path.name != "desanitize_middleware.py"
    ]
    assert importers == []
    assert DesanitizeMiddleware(json_app(CHAT), ResponseMappings())._enabled is False


async def test_disabled_it_passes_everything_through() -> None:
    mappings = ResponseMappings()
    ticket = RequestTicket("d" * 32)
    mappings.register(ticket, MAPPING)

    sent = await drive(DesanitizeMiddleware(json_app(CHAT), mappings), ticket)

    assert json.loads(body_of(sent)) == CHAT
