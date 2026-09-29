"""Drive a response through the gateway's one reversal, ``DesanitizeMiddleware``.

For tests whose pre-call ran outside the route gate (no ticket, so the guardrail kept the
request's state): ``hand_over`` does what a ticketed pre-call does at its end (the facts
deposited on a ticket, then the guardrail's own ``_hand_over``: the response mapping to
the middleware's store, the record to the ticket), then the response goes over the wire
the way litellm sends it — a JSON body, or SSE events — and comes back as the client
reads it.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, _audit_facts
from corp_llm_gateway.route_gate.desanitize_middleware import DesanitizeMiddleware, ResponseMappings
from corp_llm_gateway.route_gate.inflight import _TICKET, RequestTicket
from corp_llm_gateway.route_gate.terminal_audit import TerminalAudit, deposit

Message = dict[str, Any]
App = Callable[[Any, Any, Any], Awaitable[None]]


def hand_over(
    guardrail: CorpLlmGuardrail, data: dict[str, Any]
) -> tuple[ResponseMappings, RequestTicket]:
    mappings = ResponseMappings()
    guardrail.bind_response_mappings(mappings)
    ticket = RequestTicket(uuid.uuid4().hex)
    request_id = CorpLlmGuardrail._ensure_request_id(data)
    state = guardrail._req_state.get(request_id)
    if state is not None and deposit(ticket, _audit_facts(state, started=time.monotonic())):
        guardrail._hand_over(request_id, state, ticket)
    return mappings, ticket


async def restore_unary(
    guardrail: CorpLlmGuardrail,
    data: dict[str, Any],
    response: Any,
    *,
    terminal: TerminalAudit | None = None,
) -> Any:
    """The JSON body the client gets for ``response``."""
    _, body = await respond_unary(guardrail, data, response, terminal=terminal)
    return body


async def respond_unary(
    guardrail: CorpLlmGuardrail,
    data: dict[str, Any],
    response: Any,
    *,
    terminal: TerminalAudit | None = None,
) -> tuple[int, Any]:
    """The status and JSON body the client gets for ``response``."""
    mappings, ticket = hand_over(guardrail, data)
    body = json.dumps(_jsonable(response)).encode()
    sent = await drive(DesanitizeMiddleware(_json_app(body), mappings, terminal=terminal), ticket)
    (start,) = [m for m in sent if m["type"] == "http.response.start"]
    return int(start["status"]), json.loads(body_of(sent))


async def restore_stream(
    guardrail: CorpLlmGuardrail,
    data: dict[str, Any],
    chunks: AsyncIterator[Any],
    *,
    terminal: TerminalAudit | None = None,
) -> AsyncIterator[Any]:
    """What the client reads of a stream: the raw body messages when litellm wrote SSE
    bytes/str, else each ``data:`` event parsed (``[DONE]`` dropped). An exception the
    stream raised is re-raised after what went out before it."""
    mappings, ticket = hand_over(guardrail, data)
    raw: dict[str, bool | None] = {"seen": None, "str": False}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
            }
        )
        async for chunk in chunks:
            if raw["seen"] is None:
                raw["seen"] = isinstance(chunk, (bytes, str))
                raw["str"] = isinstance(chunk, str)
            await send({"type": "http.response.body", "body": _wire(chunk), "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    sent: list[Message] = []
    failure: BaseException | None = None
    try:
        await drive(DesanitizeMiddleware(app, mappings, terminal=terminal), ticket, sent)
    except Exception as exc:
        failure = exc
    bodies = [bytes(m.get("body") or b"") for m in sent if m["type"] == "http.response.body"]
    if raw["seen"]:
        for body in bodies:
            if body:
                yield body.decode() if raw["str"] else body
    else:
        for event in sse_payloads(b"".join(bodies)):
            yield event
    if failure is not None:
        raise failure


def sse_payloads(wire: bytes) -> list[Any]:
    out: list[Any] = []
    for event in wire.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
        data = [line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")]
        if not data or data == ["[DONE]"]:
            continue
        out.append(json.loads("\n".join(data)))
    return out


async def drive(
    app: App, ticket: RequestTicket, sent: list[Message] | None = None
) -> list[Message]:
    """One request, in the ticket's context as the in-flight limiter runs it."""
    sent = sent if sent is not None else []
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"{}", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(dict(message))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "headers": [],
    }
    token = _TICKET.set(ticket)
    try:
        await app(scope, receive, send)
    finally:
        _TICKET.reset(token)
    return sent


def body_of(sent: list[Message]) -> bytes:
    return b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def _json_app(body: bytes) -> App:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return app


def _jsonable(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json", exclude_none=True)
    return value


def _wire(chunk: Any) -> bytes:
    if isinstance(chunk, bytes):
        return chunk
    if isinstance(chunk, str):
        return chunk.encode()
    payload = _jsonable(chunk)
    return b"data: " + json.dumps(payload).encode() + b"\n\n"
