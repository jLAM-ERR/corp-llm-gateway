"""HTTP surface for the health probes + token issuance (Task B3).

`build_health_router` returns a dependency-injected ASGI app that serves:

    GET  /healthz/live            -> LiveCheck
    GET  /healthz/ready           -> ReadyCheck          (503 when unhealthy)
    GET  /healthz/sanitization    -> SanitizationCheck   (503 when unhealthy)
    GET  /healthz/extensions      -> ExtensionsCheck     (503 when unhealthy)
    POST /internal/issue-token    -> TokenIssuer         (fixes install.sh:149)

Framework choice: a framework-free ASGI app. LiteLLM is built on
FastAPI/Starlette, but neither (nor litellm) ships wheels for the 3.14
graceful-degradation venv, so a hand-rolled ASGI callable keeps this
importable + unit-testable everywhere (via `httpx.ASGITransport`) with no
new dependency. A pure-ASGI app mounts unchanged onto LiteLLM's ASGI app.

The issue-token route reads the OIDC token from the `Authorization: Bearer`
header (matching `scripts/install.sh`), falling back to a JSON body
`{"oidc_token": "..."}`. It never logs either token (M1-14).

Production wiring is a thin hook (do NOT edit bootstrap.py to test this) --
construct the checks + issuer, then serve the router as the ASGI entrypoint
with LiteLLM's app as `fallthrough`::

    from corp_llm_gateway.extensions import REGISTRY
    from corp_llm_gateway.healthz import build_health_router

    router = build_health_router(
        live_check=LiveCheck(),
        ready_check=ReadyCheck(check_redis=..., check_postgres=...),
        sanitization_check=SanitizationCheck(run_round_trip=...),
        extensions_check=ExtensionsCheck(health_all=REGISTRY.health_all),
        token_issuer=TokenIssuer(store, verifier),
        fallthrough=litellm_asgi_app,  # unknown paths delegate to litellm
    )

Every dependency is a parameter; this module imports no bootstrap/composition
root.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from corp_llm_gateway.healthz.checks import HealthCheck
from corp_llm_gateway.tokens.issuance import OidcVerificationError, TokenIssuer

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_ISSUE_TOKEN_PATH = "/internal/issue-token"


class HealthRouter:
    """Framework-free ASGI app for the health + issue-token routes."""

    def __init__(
        self,
        *,
        live_check: HealthCheck,
        ready_check: HealthCheck,
        sanitization_check: HealthCheck,
        extensions_check: HealthCheck,
        token_issuer: TokenIssuer,
        fallthrough: ASGIApp | None = None,
    ) -> None:
        self._checks: dict[str, HealthCheck] = {
            "/healthz/live": live_check,
            "/healthz/ready": ready_check,
            "/healthz/sanitization": sanitization_check,
            "/healthz/extensions": extensions_check,
        }
        self._issuer = token_issuer
        self._fallthrough = fallthrough

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self._handle_lifespan(scope, receive, send)
            return
        if scope_type != "http":
            if self._fallthrough is not None:
                await self._fallthrough(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        check = self._checks.get(path)
        if check is not None:
            if method != "GET":
                await _send_json(send, 405, {"error": "method not allowed"})
                return
            await self._handle_health(check, send)
            return

        if path == _ISSUE_TOKEN_PATH:
            if method != "POST":
                await _send_json(send, 405, {"error": "method not allowed"})
                return
            await self._handle_issue_token(scope, receive, send)
            return

        if self._fallthrough is not None:
            await self._fallthrough(scope, receive, send)
            return
        await _send_json(send, 404, {"error": "not found"})

    async def _handle_health(self, check: HealthCheck, send: Send) -> None:
        status = await check.check()
        code = 200 if status.healthy else 503
        await _send_json(
            send,
            code,
            {"status": "healthy" if status.healthy else "unhealthy", "detail": status.detail},
        )

    async def _handle_issue_token(self, scope: Scope, receive: Receive, send: Send) -> None:
        raw = await _read_body(receive)
        oidc_token = _bearer_token(scope) or _oidc_from_body(raw)
        if not oidc_token:
            await _send_json(send, 400, {"error": "missing OIDC token"})
            return
        try:
            result = await self._issuer.issue(oidc_token)
        except OidcVerificationError:
            await _send_json(send, 401, {"error": "OIDC verification failed"})
            return
        await _send_json(
            send,
            200,
            {"corp_token": result.corp_token, "expires_at": result.expires_at.isoformat()},
        )

    async def _handle_lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._fallthrough is not None:
            await self._fallthrough(scope, receive, send)
            return
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


def build_health_router(
    *,
    live_check: HealthCheck,
    ready_check: HealthCheck,
    sanitization_check: HealthCheck,
    extensions_check: HealthCheck,
    token_issuer: TokenIssuer,
    fallthrough: ASGIApp | None = None,
) -> HealthRouter:
    """Build the ASGI router with all dependencies injected as parameters."""
    return HealthRouter(
        live_check=live_check,
        ready_check=ready_check,
        sanitization_check=sanitization_check,
        extensions_check=extensions_check,
        token_issuer=token_issuer,
        fallthrough=fallthrough,
    )


def _bearer_token(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            text = value.decode("latin-1")
            if text.startswith("Bearer "):
                return text[len("Bearer ") :].strip()
            return ""
    return ""


def _oidc_from_body(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if isinstance(payload, dict):
        token = payload.get("oidc_token")
        if isinstance(token, str):
            return token
    return ""


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b"") or b"")
            if not message.get("more_body", False):
                break
        elif message["type"] == "http.disconnect":
            break
    return b"".join(chunks)


async def _send_json(send: Send, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
