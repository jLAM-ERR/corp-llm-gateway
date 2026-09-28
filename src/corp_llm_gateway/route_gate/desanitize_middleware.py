"""PROTOTYPE, NOT WIRED: restore originals in the response at the ASGI layer (plan 20260926,
Option A). ``asgi.py`` does not import it and ``enabled`` defaults to False.

Sits between the route gate and litellm's app and wraps ``send`` only (``receive`` belongs
to the in-flight limiter). Restoration state is keyed by the gateway ``RequestTicket`` and
owned here, released on the final body or when the request unwinds, never by ``audit()``.
A restoration failure never raises into litellm's app: before ``http.response.start`` it
answers a content-free 500, after it the events already restored are sent and the stream
is closed without a message. Either way the failure is reported (content-free log line +
``gateway_failure{component="desanitize"}``) and marked on the ticket for the audit.
"""

from __future__ import annotations

import codecs
import json
import logging
import re
import weakref
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from corp_llm_gateway.metrics import MetricsExporter, get_exporter
from corp_llm_gateway.route_gate.inflight import RequestTicket, current_ticket
from corp_llm_gateway.sanitizer.strategies import StrategyResult
from corp_llm_gateway.sanitizer.streaming import ResponsesStreamDesanitizer, SseStreamDesanitizer

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
RestoreJson = Callable[[Any, StrategyResult], Any]
# (gateway request id, phase, exception type): never the exception itself, it can quote content.
FailureReporter = Callable[[str, str, type[BaseException]], None]

COMPONENT = "desanitize"
E_INTERNAL = "E_INTERNAL"
INTERNAL_ERROR_BODY = json.dumps(
    {"error": {"type": "internal_error", "code": E_INTERNAL, "message": "internal error"}},
    separators=(",", ":"),
).encode()

_SSE_BOUNDARY = re.compile(r"\r\n\r\n|\n\n|\r\r")


class ResponseMappings:
    """Response-restoration mappings keyed by the gateway ticket."""

    def __init__(self) -> None:
        self._by_ticket: weakref.WeakKeyDictionary[RequestTicket, StrategyResult] = (
            weakref.WeakKeyDictionary()
        )
        # Outlives release(): the audit, written after the response, reads it.
        self._failed: weakref.WeakSet[RequestTicket] = weakref.WeakSet()

    def register(self, ticket: RequestTicket, mapping: StrategyResult) -> None:
        self._by_ticket[ticket] = mapping

    def get(self, ticket: RequestTicket) -> StrategyResult | None:
        return self._by_ticket.get(ticket)

    def release(self, ticket: RequestTicket) -> None:
        self._by_ticket.pop(ticket, None)

    def mark_failed(self, ticket: RequestTicket) -> None:
        self._failed.add(ticket)

    def failed(self, ticket: RequestTicket) -> bool:
        return ticket in self._failed

    def __contains__(self, ticket: object) -> bool:
        return ticket in self._by_ticket

    def __len__(self) -> int:
        return len(self._by_ticket)


def _default_restore_json(payload: Any, mapping: StrategyResult) -> Any:
    from corp_llm_gateway.litellm_hook import _apply_reverse_to_response

    return _apply_reverse_to_response(payload, mapping)


class DesanitizeMiddleware:
    """Restores placeholders in 2xx JSON and SSE responses of a ticketed request."""

    def __init__(
        self,
        app: ASGIApp,
        mappings: ResponseMappings,
        *,
        enabled: bool = False,
        restore_json: RestoreJson | None = None,
        metrics: MetricsExporter | None = None,
        on_failure: FailureReporter | None = None,
    ) -> None:
        self.app = app
        self._mappings = mappings
        self._enabled = enabled
        self._restore_json = restore_json or _default_restore_json
        self._metrics = metrics
        self._on_failure = on_failure or self._report_failure

    def _report_failure(self, request_id: str, phase: str, error_type: type[BaseException]) -> None:
        logger.error(
            "gateway_desanitize_failed request_id=%s phase=%s error=%s",
            request_id,
            phase,
            error_type.__name__,
        )
        (self._metrics or get_exporter()).record_failure(COMPONENT)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        ticket = current_ticket() if scope.get("type") == "http" else None
        if not self._enabled or ticket is None:
            await self.app(scope, receive, send)
            return
        response = _Response(ticket, self._mappings, send, self._restore_json, self._on_failure)
        try:
            await self.app(scope, receive, response.send)
        finally:
            self._mappings.release(ticket)


class _Response:
    def __init__(
        self,
        ticket: RequestTicket,
        mappings: ResponseMappings,
        send: Send,
        restore_json: RestoreJson,
        on_failure: FailureReporter,
    ) -> None:
        self._ticket = ticket
        self._mappings = mappings
        self._send = send
        self._restore_json = restore_json
        self._on_failure = on_failure
        self._mode = "unstarted"
        self._start: Message | None = None
        self._mapping: StrategyResult | None = None
        self._body = bytearray()
        self._sse: _SseRestorer | None = None

    async def send(self, message: Message) -> None:
        if self._mode == "closed":
            return
        kind = message.get("type")
        if kind == "http.response.start":
            await self._on_start(message)
            return
        if kind != "http.response.body" or self._mode in ("unstarted", "pass"):
            await self._send(message)
            return
        chunk = bytes(message.get("body") or b"")
        more = bool(message.get("more_body", False))
        if self._mode == "unary":
            self._body += chunk
            if not more:
                await self._finish_unary()
            return
        await self._on_sse(chunk, more)

    async def _on_start(self, message: Message) -> None:
        mapping = self._mappings.get(self._ticket)
        status = int(message.get("status") or 0)
        content_type = _header(message, b"content-type").lower()
        if mapping is None or not mapping.pairs or not 200 <= status < 300:
            self._mode = "pass"
            await self._send(message)
            return
        self._mapping = mapping
        if content_type.startswith("text/event-stream"):
            failure: type[BaseException] | None = None
            try:
                self._sse = _SseRestorer(mapping)
            except Exception as exc:
                failure = type(exc)
            # Answered outside the handler, as in _finish_unary.
            if failure is not None:
                self._failed("before_start", failure)
                await self._internal_error()
                return
            self._mode = "sse"
            await self._send({**message, "headers": _without(message, b"content-length")})
            return
        if "json" in content_type:
            self._mode = "unary"
            self._start = message
            return
        self._mode = "pass"
        await self._send(message)

    async def _finish_unary(self) -> None:
        assert self._start is not None and self._mapping is not None
        body = b""
        failure: type[BaseException] | None = None
        try:
            payload = json.loads(bytes(self._body))
            restored = self._restore_json(payload, self._mapping)
            body = json.dumps(restored, ensure_ascii=False, separators=(",", ":")).encode()
        except Exception as exc:
            failure = type(exc)
        finally:
            self._body.clear()
        # Sent outside the handler: a failing send must not carry the failure as __context__.
        if failure is not None:
            self._failed("before_start", failure)
            await self._internal_error()
            return
        headers = [
            *_without(self._start, b"content-length"),
            (b"content-length", str(len(body)).encode()),
        ]
        self._mappings.release(self._ticket)
        await self._send({**self._start, "headers": headers})
        await self._send({"type": "http.response.body", "body": body, "more_body": False})

    async def _on_sse(self, chunk: bytes, more: bool) -> None:
        assert self._sse is not None
        failure: type[BaseException] | None = None
        try:
            self._sse.feed(chunk)
            if not more:
                self._sse.flush()
        except Exception as exc:
            failure = type(exc)
        # Whole events restored before a failure; the failing one is dropped.
        out = self._sse.take()
        if failure is not None:
            self._mode = "closed"
            self._failed("after_start", failure)
            if out:
                await self._send({"type": "http.response.body", "body": out, "more_body": True})
            await self._send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        if not more:
            self._mappings.release(self._ticket)
        await self._send({"type": "http.response.body", "body": out, "more_body": more})

    async def _internal_error(self) -> None:
        self._mode = "closed"
        self._mappings.release(self._ticket)
        await self._send(
            {
                "type": "http.response.start",
                "status": 500,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(INTERNAL_ERROR_BODY)).encode()),
                ],
            }
        )
        await self._send(
            {"type": "http.response.body", "body": INTERNAL_ERROR_BODY, "more_body": False}
        )

    def _failed(self, phase: str, error_type: type[BaseException]) -> None:
        self._mappings.mark_failed(self._ticket)
        self._mappings.release(self._ticket)
        try:
            self._on_failure(self._ticket.gateway_id, phase, error_type)
        except Exception as exc:
            logger.error(
                "gateway_desanitize_report_failed request_id=%s error=%s",
                self._ticket.gateway_id,
                type(exc).__name__,
            )


class _SseRestorer:
    """Complete SSE events in, restored events out; Responses events by their own adapter."""

    def __init__(self, mapping: StrategyResult) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._ready: list[str] = []
        self._events = SseStreamDesanitizer(mapping)
        self._responses = ResponsesStreamDesanitizer(mapping)

    def feed(self, chunk: bytes) -> None:
        """Restore every complete event; if one raises, those before it stay in ``take()``."""
        self._buffer += self._utf8.decode(chunk, final=False)
        while (match := _SSE_BOUNDARY.search(self._buffer)) is not None:
            event, self._buffer = self._buffer[: match.end()], self._buffer[match.end() :]
            self._ready.extend(self._event(event))

    def flush(self) -> None:
        self._buffer += self._utf8.decode(b"", final=True)
        if self._buffer:
            event, self._buffer = self._buffer, ""
            self._ready.extend(self._event(event))
        self._ready.extend(_text(item) for item in self._events.flush())
        self._ready.extend(
            _frame(json.loads(item), with_event_line=False) for item in self._responses.flush()
        )

    def take(self) -> bytes:
        out, self._ready = "".join(self._ready), []
        return out.encode()

    def _event(self, event: str) -> list[str]:
        payload = _data_payload(event)
        if isinstance(payload, dict) and str(payload.get("type") or "").startswith("response."):
            with_event_line = any(line.startswith("event:") for line in event.splitlines())
            return [
                _frame(item if isinstance(item, dict) else json.loads(item), with_event_line)
                for item in self._responses.feed(payload)
            ]
        return [_text(item) for item in self._events.feed(event)]


def _data_payload(event: str) -> Any:
    data = [line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")]
    if not data:
        return None
    try:
        return json.loads("\n".join(data))
    except ValueError:
        return None


def _frame(payload: dict[str, Any], with_event_line: bool) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    prefix = f"event: {payload.get('type')}\n" if with_event_line else ""
    return f"{prefix}data: {data}\n\n"


def _text(item: bytes | str) -> str:
    return item.decode("utf-8", "replace") if isinstance(item, bytes) else item


def _header(message: Message, name: bytes) -> str:
    for key, value in message.get("headers") or ():
        if bytes(key).lower() == name:
            return bytes(value).decode("latin-1")
    return ""


def _without(message: Message, name: bytes) -> list[tuple[bytes, bytes]]:
    return [
        (bytes(key), bytes(value))
        for key, value in message.get("headers") or ()
        if bytes(key).lower() != name
    ]
