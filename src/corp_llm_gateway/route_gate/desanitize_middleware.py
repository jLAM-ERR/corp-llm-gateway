"""NOT WIRED: restore originals in the response at the ASGI layer (plan 20260926,
Option A). ``asgi.py`` does not import it and ``enabled`` defaults to False.

Sits between the in-flight limiter and litellm's app and wraps ``send`` only (``receive``
belongs to the limiter). Restoration state is keyed by the gateway ``RequestTicket`` and
owned here, never by ``audit()``: the mapping is released on the final body in every mode
(restored, non-2xx, pass-through), when the request unwinds, and at the ticket's close,
which the limiter reaches after its grace even if the downstream never unwinds; the
restorer's buffers go with it. A mapping offered for a cancelled or closed ticket is
refused.

A restoration failure never raises into litellm's app: before ``http.response.start`` it
answers a content-free 500, after it the events already restored are sent and the stream
is closed without a message. Either way it is reported (content-free log line +
``gateway_failure{component="desanitize"}``). The audit does not learn about it from a
marker: litellm's success event can be written before the marker is set. The terminal
record is published once through ``terminal_audit.TerminalAudit``, here at the final body
or the failure, and at the ticket's close for a request that got neither.

``Content-Encoding``: a response carrying one passes through untouched (placeholders,
never originals); ``compressor_problems`` is the arm check that nothing in the served
stack compresses a response before it reaches this middleware.
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
from corp_llm_gateway.route_gate.terminal_audit import Outcome, TerminalAudit
from corp_llm_gateway.sanitizer.strategies import StrategyResult
from corp_llm_gateway.sanitizer.streaming import (
    ResponsesStreamDesanitizer,
    SseStreamDesanitizer,
    is_stream_error_event,
)

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
_COMPRESSORS = ("gzip", "brotli", "zstd", "deflate", "compress")


class ResponseMappings:
    """Response-restoration mappings keyed by the gateway ticket."""

    def __init__(self) -> None:
        self._by_ticket: weakref.WeakKeyDictionary[RequestTicket, StrategyResult] = (
            weakref.WeakKeyDictionary()
        )
        # Diagnostic only; the terminal audit record is what reports the failure.
        self._failed: weakref.WeakSet[RequestTicket] = weakref.WeakSet()

    def register(self, ticket: RequestTicket, mapping: StrategyResult) -> bool:
        """Hold ``mapping`` until the ticket's final body, unwind or close; False, with a
        content-free log line, for a ticket already cancelled or closed. Never raises."""
        try:
            refused = "cancelled" if ticket.cancelled else "closed" if ticket.closed else None
            if refused is None and not ticket.on_close(self.release):
                refused = "closed"
            if refused is not None:
                logger.warning(
                    "gateway_desanitize_register_refused request_id=%s reason=%s",
                    ticket.gateway_id,
                    refused,
                )
                return False
            self._by_ticket[ticket] = mapping
            return True
        except Exception as exc:
            logger.error("gateway_desanitize_register_failed error=%s", type(exc).__name__)
            return False

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


def compressor_problems(app: Any) -> list[str]:
    """Middleware on ``app`` (a Starlette/FastAPI app) that would compress a response
    before this middleware sees it; the arm check for the Content-Encoding pass-through."""
    found: list[str] = []
    for entry in getattr(app, "user_middleware", None) or ():
        cls = getattr(entry, "cls", entry)
        name = getattr(cls, "__name__", type(cls).__name__)
        if any(word in name.lower() for word in _COMPRESSORS):
            found.append(name)
    return found


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
        terminal: TerminalAudit | None = None,
    ) -> None:
        self.app = app
        self._mappings = mappings
        self._enabled = enabled
        self._restore_json = restore_json or _default_restore_json
        self._metrics = metrics
        self._on_failure = on_failure or self._report_failure
        self._terminal = terminal

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
        response = _Response(
            ticket, self._mappings, send, self._restore_json, self._on_failure, self._terminal
        )
        ticket.on_close(response.close)
        if self._terminal is not None:
            self._terminal.bind(ticket)
        raised: Exception | None = None
        try:
            try:
                await self.app(scope, receive, response.send)
            except Exception as exc:
                raised = exc
            if raised is not None:
                # Outside the handler: the tails' send must not chain the app's error.
                await response.app_failed()
                raise raised
        finally:
            response.close()


class _Response:
    def __init__(
        self,
        ticket: RequestTicket,
        mappings: ResponseMappings,
        send: Send,
        restore_json: RestoreJson,
        on_failure: FailureReporter,
        terminal: TerminalAudit | None,
    ) -> None:
        self._ticket = ticket
        self._mappings = mappings
        self._send = send
        self._restore_json = restore_json
        self._on_failure = on_failure
        self._terminal = terminal
        self._mode = "unstarted"
        self._status = 0
        self._start: Message | None = None
        self._mapping: StrategyResult | None = None
        self._body = bytearray()
        self._sse: _SseRestorer | None = None

    def close(self, _ticket: RequestTicket | None = None) -> None:
        """Drop everything restoration holds; later messages are not sent."""
        self._mode = "closed"
        self._mappings.release(self._ticket)
        self._mapping = None
        self._start = None
        self._sse = None
        self._body.clear()

    async def send(self, message: Message) -> None:
        if self._mode == "closed":
            return
        kind = message.get("type")
        if kind == "http.response.start":
            await self._on_start(message)
            return
        if kind != "http.response.body" or self._mode == "unstarted":
            await self._send(message)
            return
        more = bool(message.get("more_body", False))
        if self._mode == "pass":
            if more:
                await self._send(message)
                return
            self.close()
            await self._send(message)
            await self._publish("ok" if 200 <= self._status < 300 else "failed")
            return
        chunk = bytes(message.get("body") or b"")
        if self._mode == "unary":
            self._body += chunk
            if not more:
                await self._finish_unary()
            return
        await self._on_sse(chunk, more)

    async def app_failed(self) -> None:
        """The app raised: restored tails of a stream still go out, then the record."""
        sse = self._sse if self._mode == "sse" else None
        self.close()
        if sse is not None:
            out = b""
            failure: type[BaseException] | None = None
            try:
                sse.flush()
            except Exception as exc:
                failure = type(exc)
            out = sse.take()
            if failure is not None:
                self._failed("after_start", failure)
            if out:
                try:
                    await self._send({"type": "http.response.body", "body": out, "more_body": True})
                except Exception as exc:
                    logger.warning(
                        "gateway_desanitize_tail_send_failed request_id=%s error=%s",
                        self._ticket.gateway_id,
                        type(exc).__name__,
                    )
        await self._publish("failed")

    async def _on_start(self, message: Message) -> None:
        mapping = self._mappings.get(self._ticket)
        self._status = int(message.get("status") or 0)
        content_type = _header(message, b"content-type").lower()
        encoding = _header(message, b"content-encoding").strip().lower()
        if (
            mapping is None
            or not mapping.pairs
            or not 200 <= self._status < 300
            or encoding not in ("", "identity")
        ):
            self._mode = "pass"
            self._mappings.release(self._ticket)
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
        self._mappings.release(self._ticket)
        await self._send(message)

    async def _finish_unary(self) -> None:
        start, mapping = self._start, self._mapping
        assert start is not None and mapping is not None
        body = b""
        failure: type[BaseException] | None = None
        try:
            payload = json.loads(bytes(self._body))
            restored = self._restore_json(payload, mapping)
            body = json.dumps(restored, ensure_ascii=False, separators=(",", ":")).encode()
        except Exception as exc:
            failure = type(exc)
        finally:
            self.close()
        # Sent outside the handler: a failing send must not carry the failure as __context__.
        if failure is not None:
            self._failed("before_start", failure)
            await self._internal_error()
            return
        headers = [
            *_without(start, b"content-length"),
            (b"content-length", str(len(body)).encode()),
        ]
        await self._send({**start, "headers": headers})
        await self._send({"type": "http.response.body", "body": body, "more_body": False})
        await self._publish("ok")

    async def _on_sse(self, chunk: bytes, more: bool) -> None:
        sse = self._sse
        if sse is None:
            return
        failure: type[BaseException] | None = None
        try:
            sse.feed(chunk)
            if not more:
                sse.flush()
        except Exception as exc:
            failure = type(exc)
        # Whole events restored before a failure; the failing one is dropped.
        out = sse.take()
        if failure is not None:
            self.close()
            self._failed("after_start", failure)
            if out:
                await self._send({"type": "http.response.body", "body": out, "more_body": True})
            await self._send({"type": "http.response.body", "body": b"", "more_body": False})
            await self._publish("failed", E_INTERNAL)
            return
        if not more:
            self.close()
            await self._send({"type": "http.response.body", "body": out, "more_body": False})
            await self._publish("failed" if sse.saw_error else "ok")
            return
        await self._send({"type": "http.response.body", "body": out, "more_body": True})

    async def _internal_error(self) -> None:
        self.close()
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
        await self._publish("failed", E_INTERNAL)

    async def _publish(self, outcome: Outcome, error_code: str | None = None) -> None:
        if self._terminal is not None:
            await self._terminal.publish(self._ticket, outcome, error_code=error_code)

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
    """Complete SSE events in, restored events out; Responses events by their own adapter.

    Held Responses text goes out before ``[DONE]`` and before an error event, never
    after; the chat / Anthropic adapter does the same for its own buffers."""

    def __init__(self, mapping: StrategyResult) -> None:
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._ready: list[str] = []
        self._events = SseStreamDesanitizer(mapping)
        self._responses = ResponsesStreamDesanitizer(mapping)
        self._event_lines = False
        # An error event or ``response.failed`` went out: the response did not succeed.
        self.saw_error = False

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
        self._ready.extend(self._responses_tails())
        self._ready.extend(_text(item) for item in self._events.flush())

    def take(self) -> bytes:
        out, self._ready = "".join(self._ready), []
        return out.encode()

    def _event(self, event: str) -> list[str]:
        data = _data_field(event)
        payload = _json_or_none(data)
        head: list[str] = []
        if data == "[DONE]" or is_stream_error_event(payload):
            self.saw_error = self.saw_error or data != "[DONE]"
            head = self._responses_tails()
        event_type = str(payload.get("type") or "") if isinstance(payload, dict) else ""
        if event_type.startswith("response."):
            with_event_line = any(line.startswith("event:") for line in event.splitlines())
            self._event_lines = self._event_lines or with_event_line
            self.saw_error = self.saw_error or event_type == "response.failed"
            return head + [
                _frame(item if isinstance(item, dict) else json.loads(item), with_event_line)
                for item in self._responses.feed(payload)
            ]
        return head + [_text(item) for item in self._events.feed(event)]

    def _responses_tails(self) -> list[str]:
        return [_frame(json.loads(item), self._event_lines) for item in self._responses.flush()]


def _data_field(event: str) -> str | None:
    data = [line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")]
    return "\n".join(data) if data else None


def _json_or_none(data: str | None) -> Any:
    if data is None:
        return None
    try:
        return json.loads(data)
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
