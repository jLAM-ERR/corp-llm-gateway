"""The gate itself: a pure-ASGI middleware that answers before litellm's router.

Pure ASGI, not ``BaseHTTPMiddleware``: it must see ``lifespan`` and ``websocket``
scopes (Starlette's HTTP middleware never does) and it must never buffer a
response, or SSE streaming would stall behind it. Forwarding is therefore a bare
``await self.app(scope, receive, send)`` — no wrapping of ``send``.

Fail-closed: a REFUSE, an unarmed REWRITTEN route and any classifier exception
all end here, never at litellm. The request body is never read on a refusal and
no request byte is echoed (M1-14); the refusal names only the route the caller
itself sent.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from datetime import UTC, datetime
from typing import Any

from corp_llm_gateway.audit.event import AuditEvent
from corp_llm_gateway.audit.logger import AuditLogger
from corp_llm_gateway.metrics import MetricsExporter
from corp_llm_gateway.route_gate.classify import (
    ROUTE_GATE_ERROR,
    ROUTE_GATE_LISTED,
    ROUTE_GATE_MALFORMED,
    ROUTE_GATE_UNARMED,
    ROUTE_GATE_UNLISTED,
    ROUTE_GATE_WEBSOCKET,
    classify,
)
from corp_llm_gateway.route_gate.table import HTTP_METHODS, Entry, Verdict

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# The gateway_failure{component} label for every gate-side failure.
COMPONENT = "route_gate"

_BLOCKED = "E_ROUTE_BLOCKED"

_STATUS: dict[str, int] = {
    ROUTE_GATE_UNLISTED: 404,
    ROUTE_GATE_LISTED: 403,
    ROUTE_GATE_WEBSOCKET: 403,
    ROUTE_GATE_MALFORMED: 403,
    ROUTE_GATE_UNARMED: 503,
    ROUTE_GATE_ERROR: 500,
}

_ERROR_CODE: dict[str, str] = {
    ROUTE_GATE_UNLISTED: _BLOCKED,
    ROUTE_GATE_LISTED: _BLOCKED,
    ROUTE_GATE_WEBSOCKET: _BLOCKED,
    ROUTE_GATE_MALFORMED: _BLOCKED,
    ROUTE_GATE_UNARMED: "E_ROUTE_GATE_UNARMED",
    ROUTE_GATE_ERROR: "E_ROUTE_GATE_ERROR",
}

_ERROR_TYPE: dict[str, str] = {
    ROUTE_GATE_UNLISTED: "route_blocked",
    ROUTE_GATE_LISTED: "route_blocked",
    ROUTE_GATE_WEBSOCKET: "route_blocked",
    ROUTE_GATE_MALFORMED: "route_blocked",
    ROUTE_GATE_UNARMED: "route_gate_unarmed",
    ROUTE_GATE_ERROR: "route_gate_error",
}

# Refusals that mean the gateway itself is wrong, not the caller.
_FAILURES = frozenset({ROUTE_GATE_UNARMED, ROUTE_GATE_ERROR})

_UNARMED_WHY = (
    "the guardrail callback is not registered, so a rewritten route cannot be proven sanitized"
)
_ERROR_WHY = "the route gate could not classify this request"


class RouteGateMiddleware:
    """Default-deny gate in front of litellm's ASGI app."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        metrics: MetricsExporter,
        audit_logger: AuditLogger,
        extras: Mapping[tuple[str, str], Entry] | None = None,
    ) -> None:
        self.app = app
        self._metrics = metrics
        self._audit = audit_logger
        self._extras = dict(extras) if extras else {}
        self.armed = False

    def arm(self) -> None:
        """Called from the lifespan wrapper once the guardrail is registered."""
        self.armed = True

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "GET").upper()
        path = str(scope.get("path") or "")
        try:
            decision = classify(
                method,
                path,
                scope.get("raw_path"),
                scope_type=str(scope["type"]),
                upgrade_header=_upgrade_header(scope),
                extras=self._extras,
            )
        except Exception as exc:
            # Neither the path nor a traceback: an exception message can quote
            # the input it choked on, and gateway stdout is an audited surface.
            logger.error(
                "route_gate_classify_failed method=%s error=%s",
                _safe_method(method),
                type(exc).__name__,
            )
            await self._refuse(scope, receive, send, method, path, ROUTE_GATE_ERROR, _ERROR_WHY)
            return

        if decision.verdict is Verdict.REFUSE:
            reason = decision.block_reason or ROUTE_GATE_ERROR
            await self._refuse(scope, receive, send, method, path, reason, decision.why)
            return
        if decision.verdict is Verdict.REWRITTEN and not self.armed:
            await self._refuse(scope, receive, send, method, path, ROUTE_GATE_UNARMED, _UNARMED_WHY)
            return
        if decision.verdict is Verdict.PASSTHROUGH or decision.verdict is Verdict.REWRITTEN:
            await self.app(scope, receive, send)
            return
        # A verdict this middleware does not know is a gate defect, not a pass.
        logger.error("route_gate_unknown_verdict method=%s", _safe_method(method))
        await self._refuse(scope, receive, send, method, path, ROUTE_GATE_ERROR, _ERROR_WHY)

    async def _refuse(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        method: str,
        path: str,
        reason: str,
        why: str,
    ) -> None:
        self._metrics.record_block(reason)
        if reason in _FAILURES:
            self._metrics.record_failure(COMPONENT)
        scope_type = str(scope["type"])
        # No path, no header and no request byte: gateway stdout is an audited
        # surface (M1-14) and a path can carry caller content. The method is
        # narrowed to the known verbs because an HTTP method is a caller-chosen
        # token; `why` is a fixed string in every branch, never a request byte.
        logger.warning(
            "route_gate_blocked method=%s scope=%s block_reason=%s why=%s",
            _safe_method(method),
            scope_type,
            reason,
            why,
        )
        await self._emit_audit(reason)

        if scope_type == "websocket":
            await self._refuse_handshake(scope, receive, send, method, path, reason)
        elif scope_type == "http":
            await _send_json(send, _STATUS[reason], _payload(method, path, reason))
        # Any other scope type has no response protocol; not forwarding is the refusal.

    async def _refuse_handshake(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        method: str,
        path: str,
        reason: str,
    ) -> None:
        # The block is already counted: a handshake that disconnects instead of
        # connecting is still a refused route, and it must never be forwarded.
        # ASGI forbids sending anything before the connect event is received.
        message = await receive()
        if message["type"] != "websocket.connect":
            return
        if "websocket.http.response" in (scope.get("extensions") or {}):
            body = _encode(_payload(method, path, reason))
            await send(
                {
                    "type": "websocket.http.response.start",
                    "status": _STATUS[reason],
                    "headers": _headers(body),
                }
            )
            await send({"type": "websocket.http.response.body", "body": body})
            return
        # 1008 = policy violation, for servers without the denial-response extension.
        await send({"type": "websocket.close", "code": 1008})

    async def _emit_audit(self, reason: str) -> None:
        # The gate refuses before litellm assigns a call id, so the record gets
        # its own id. ALWAYS fields only — there is no request identity yet.
        event = AuditEvent(
            timestamp=datetime.now(UTC),
            request_id=uuid.uuid4().hex,
            user_id="unknown",
            team_id="unknown",
            provider="unknown",
            model="unknown",
            latency_ms=0,
            prompt_token_count=0,
            completion_token_count=0,
            redaction_count=0,
            status="failed",
            error_code=_ERROR_CODE[reason],
            block_reason=reason,
        )
        try:
            await self._audit.emit(event)
        except Exception:
            # The refusal stands either way; a lost audit record is a gate failure.
            self._metrics.record_failure(COMPONENT)
            logger.error("route_gate_audit_failed block_reason=%s", reason)


def _payload(method: str, path: str, reason: str) -> dict[str, Any]:
    return {
        "error": {
            "type": _ERROR_TYPE[reason],
            "code": _ERROR_CODE[reason],
            "route": f"{method} {path}",
            "reason": reason,
        }
    }


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def _headers(body: bytes) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]


async def _send_json(send: Send, status: int, payload: dict[str, Any]) -> None:
    body = _encode(payload)
    await send({"type": "http.response.start", "status": status, "headers": _headers(body)})
    await send({"type": "http.response.body", "body": body})


def _safe_method(method: str) -> str:
    return method if method in HTTP_METHODS else "other"


def _upgrade_header(scope: Scope) -> str | None:
    for name, value in scope.get("headers") or ():
        if name.lower() == b"upgrade":
            return value.decode("latin-1")
    return None
