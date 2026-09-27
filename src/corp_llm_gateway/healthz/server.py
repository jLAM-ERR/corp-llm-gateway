"""HTTP surface for the health probes + developer token issuance.

`build_health_router` returns a dependency-injected ASGI app that serves:

    GET|HEAD /healthz/live         -> LiveCheck
    GET|HEAD /healthz/ready        -> ReadyCheck          (503 when unhealthy)
    GET|HEAD /healthz/sanitization -> SanitizationCheck   (503 when unhealthy)
    GET|HEAD /healthz/extensions   -> ExtensionsCheck     (503 when unhealthy)
    POST     /internal/issue-token -> TokenIssuer         (404 when no issuer)

Framework choice: a framework-free ASGI app. LiteLLM is built on
FastAPI/Starlette, but neither (nor litellm) ships wheels for the 3.14
graceful-degradation venv, so a hand-rolled ASGI callable keeps this
importable + unit-testable everywhere (via `httpx.ASGITransport`) with no
new dependency. A pure-ASGI app mounts unchanged in front of LiteLLM's app.

The issue-token route is a public endpoint and is bounded like one: the Keycloak
access token comes from the `Authorization: Bearer` header only, any request
body byte is refused (the read stops at 1 KiB and at 2 s), and the route has its
own in-flight cap and token bucket, answered with 429 without queueing. The
issuer's work after the body (verification, team lookup, token store) is bounded
too: past the bound the request answers 503 and frees its slot. With no issuer
the path is a local 404 for every method — it never falls through. Error bodies
carry a code only, every issuance response is ``cache-control: no-store``, and
the one log line per request carries the status and the code: never the bearer,
the minted token or a claim (M1-14).

Production wiring lives in `corp_llm_gateway.bootstrap.build_health_router`;
this module imports no composition root.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from corp_llm_gateway.healthz.checks import HealthCheck
from corp_llm_gateway.tokens.errors import IssuancePolicyError
from corp_llm_gateway.tokens.issuance import (
    JwksUnavailableError,
    OidcTeamMappingError,
    OidcVerificationError,
    TokenIssuer,
)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_ISSUE_TOKEN_PATH = "/internal/issue-token"

ISSUE_MAX_BODY_BYTES = 1024
ISSUE_BODY_TIMEOUT_S = 2.0
DEFAULT_ISSUE_MAX_INFLIGHT = 4
DEFAULT_ISSUE_RATE_PER_MINUTE = 30
DEFAULT_ISSUE_TIMEOUT_S = 10.0

_CODE = re.compile(r"E_[A-Z0-9_]{1,64}")
_DISCONNECTED = "disconnected"

_log = logging.getLogger(__name__)


class _TokenBucket:
    """``per_minute`` requests a minute, bursting to the same number."""

    def __init__(self, per_minute: int, clock: Callable[[], float]) -> None:
        self._capacity = float(per_minute)
        self._per_second = per_minute / 60.0
        self._clock = clock
        self._tokens = self._capacity
        self._at = clock()

    def take(self) -> bool:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._at) * self._per_second)
        self._at = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True


class HealthRouter:
    """Framework-free ASGI app for the health + issue-token routes."""

    def __init__(
        self,
        *,
        live_check: HealthCheck,
        ready_check: HealthCheck,
        sanitization_check: HealthCheck,
        extensions_check: HealthCheck,
        token_issuer: TokenIssuer | None = None,
        fallthrough: ASGIApp | None = None,
        issue_max_inflight: int = DEFAULT_ISSUE_MAX_INFLIGHT,
        issue_rate_per_minute: int = DEFAULT_ISSUE_RATE_PER_MINUTE,
        issue_body_timeout_s: float = ISSUE_BODY_TIMEOUT_S,
        issue_timeout_s: float = DEFAULT_ISSUE_TIMEOUT_S,
        issue_clock: Callable[[], float] = time.monotonic,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        bounds = (issue_max_inflight, issue_rate_per_minute, issue_body_timeout_s, issue_timeout_s)
        # `not x > 0` also refuses NaN.
        if any(not bound > 0 for bound in bounds):
            raise ValueError("issuance bounds must be positive")
        self._checks: dict[str, HealthCheck] = {
            "/healthz/live": live_check,
            "/healthz/ready": ready_check,
            "/healthz/sanitization": sanitization_check,
            "/healthz/extensions": extensions_check,
        }
        self._issuer = token_issuer
        self._fallthrough = fallthrough
        self._issue_max_inflight = issue_max_inflight
        self._issue_inflight = 0
        self._issue_bucket = _TokenBucket(issue_rate_per_minute, issue_clock)
        self._issue_body_timeout_s = issue_body_timeout_s
        self._issue_timeout_s = issue_timeout_s
        self._on_close = on_close

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
            # HEAD follows GET: Starlette registers one for every GET route, the
            # route gate's table lists `GET|HEAD /healthz/*`, and probes that use
            # HEAD (curl -I, some ingress controllers) must get the same status.
            if method not in ("GET", "HEAD"):
                await _send_json(send, 405, {"error": "method not allowed"})
                return
            await self._handle_health(check, send, body=method == "GET")
            return

        if path == _ISSUE_TOKEN_PATH:
            # A response to HEAD carries no body (h11 refuses to send one).
            if self._issuer is None:
                await _issue_respond(send, 404, "E_ISSUE_DISABLED", body=method != "HEAD")
            elif method != "POST":
                await _issue_respond(send, 405, "E_METHOD_NOT_ALLOWED", body=method != "HEAD")
            else:
                await self._handle_issue_token(self._issuer, scope, receive, send)
            return

        if self._fallthrough is not None:
            await self._fallthrough(scope, receive, send)
            return
        await _send_json(send, 404, {"error": "not found"})

    async def _handle_health(self, check: HealthCheck, send: Send, *, body: bool = True) -> None:
        status = await check.check()
        code = 200 if status.healthy else 503
        await _send_json(
            send,
            code,
            {"status": "healthy" if status.healthy else "unhealthy", "detail": status.detail},
            body=body,
        )

    async def _handle_issue_token(
        self, issuer: TokenIssuer, scope: Scope, receive: Receive, send: Send
    ) -> None:
        # Checked in this order so a refused request spends no bucket token.
        if self._issue_inflight >= self._issue_max_inflight:
            await _issue_respond(send, 429, "E_ISSUE_INFLIGHT")
            return
        if not self._issue_bucket.take():
            await _issue_respond(send, 429, "E_ISSUE_THROTTLED")
            return
        self._issue_inflight += 1
        try:
            outcome = await self._issue(issuer, scope, receive)
        finally:
            self._issue_inflight -= 1
        if outcome is None:
            return
        status, code, payload = outcome
        await _issue_respond(send, status, code, payload)

    async def _issue(
        self, issuer: TokenIssuer, scope: Scope, receive: Receive
    ) -> tuple[int, str, dict[str, Any] | None] | None:
        refused = await self._refuse_body(receive)
        if refused == _DISCONNECTED:
            return None
        if refused is not None:
            return (408 if refused == "E_ISSUE_BODY_TIMEOUT" else 400), refused, None
        bearer = _bearer_token(scope)
        if not bearer:
            return 401, "E_OIDC_MISSING", None
        bound = asyncio.timeout(self._issue_timeout_s)
        try:
            async with bound:
                result = await issuer.issue(bearer)
        except OidcVerificationError as exc:
            return 401, _code_of(exc, "E_OIDC_INVALID"), None
        except OidcTeamMappingError as exc:
            return 403, _code_of(exc, "E_ISSUE_NO_TEAM"), None
        except IssuancePolicyError as exc:
            code = _code_of(exc, "E_ISSUE_REFUSED")
            return (503 if code == IssuancePolicyError.BUSY else 403), code, None
        except JwksUnavailableError:
            return 503, "E_JWKS_UNAVAILABLE", None
        except Exception as exc:
            # The class name only: a driver message can quote a token or a claim.
            if bound.expired():
                _log.warning("issue_token store work past its bound: %s", type(exc).__name__)
                return 503, "E_ISSUE_STORE_TIMEOUT", None
            if _store_unavailable(exc):
                _log.warning("issue_token store unavailable: %s", type(exc).__name__)
                return 503, "E_ISSUE_STORE_UNAVAILABLE", None
            _log.error("issue_token internal failure: %s", type(exc).__name__)
            return 500, "E_ISSUE_INTERNAL", None
        payload = {"corp_token": result.corp_token, "expires_at": result.expires_at.isoformat()}
        return 200, "ok", payload

    async def _refuse_body(self, receive: Receive) -> str | None:
        """Drain the request body within the bounds; ``None`` when it was empty."""
        size = 0
        try:
            async with asyncio.timeout(self._issue_body_timeout_s):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return _DISCONNECTED
                    if message["type"] != "http.request":
                        continue
                    size += len(message.get("body") or b"")
                    if size > ISSUE_MAX_BODY_BYTES:
                        return "E_ISSUE_BODY"
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return "E_ISSUE_BODY_TIMEOUT"
        return "E_ISSUE_BODY" if size else None

    async def aclose(self) -> None:
        """Release what the issuance route holds open (the JWKS HTTP client).
        Idempotent; a failure is logged by class name and never raised."""
        on_close, self._on_close = self._on_close, None
        if on_close is None:
            return
        try:
            await on_close()
        except Exception as exc:
            _log.warning("issuance resources not closed cleanly: %s", type(exc).__name__)

    async def _handle_lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._fallthrough is not None:

            async def send_after_closing(message: Message) -> None:
                if message["type"] in ("lifespan.shutdown.complete", "lifespan.shutdown.failed"):
                    await self.aclose()
                await send(message)

            await self._fallthrough(scope, receive, send_after_closing)
            return
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return


def build_health_router(
    *,
    live_check: HealthCheck,
    ready_check: HealthCheck,
    sanitization_check: HealthCheck,
    extensions_check: HealthCheck,
    token_issuer: TokenIssuer | None = None,
    fallthrough: ASGIApp | None = None,
    issue_max_inflight: int = DEFAULT_ISSUE_MAX_INFLIGHT,
    issue_rate_per_minute: int = DEFAULT_ISSUE_RATE_PER_MINUTE,
    issue_body_timeout_s: float = ISSUE_BODY_TIMEOUT_S,
    issue_timeout_s: float = DEFAULT_ISSUE_TIMEOUT_S,
    issue_clock: Callable[[], float] = time.monotonic,
    on_close: Callable[[], Awaitable[None]] | None = None,
) -> HealthRouter:
    """Build the ASGI router with all dependencies injected as parameters.

    ``token_issuer=None`` disables issuance: the path answers 404 locally.
    ``on_close`` runs once, at lifespan shutdown or on ``aclose()``.
    """
    return HealthRouter(
        live_check=live_check,
        ready_check=ready_check,
        sanitization_check=sanitization_check,
        extensions_check=extensions_check,
        token_issuer=token_issuer,
        fallthrough=fallthrough,
        issue_max_inflight=issue_max_inflight,
        issue_rate_per_minute=issue_rate_per_minute,
        issue_body_timeout_s=issue_body_timeout_s,
        issue_timeout_s=issue_timeout_s,
        issue_clock=issue_clock,
        on_close=on_close,
    )


def _bearer_token(scope: Scope) -> str:
    """The one ``Authorization: Bearer`` value; empty when absent, another scheme
    or sent twice (two credentials are ambiguous, never a choice to make)."""
    values = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
    if len(values) != 1:
        return ""
    scheme, _, token = values[0].decode("latin-1").partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def _store_unavailable(exc: BaseException) -> bool:
    """A connection-class store failure. asyncpg is only consulted when something
    already imported it: without it loaded, none of its errors can be in flight."""
    if isinstance(exc, OSError):  # ConnectionRefusedError, TimeoutError (pool acquire), …
        return True
    asyncpg = sys.modules.get("asyncpg")
    return asyncpg is not None and isinstance(
        exc,
        (
            asyncpg.PostgresConnectionError,
            asyncpg.InterfaceError,
            asyncpg.CannotConnectNowError,
            asyncpg.TooManyConnectionsError,
        ),
    )


def _code_of(exc: BaseException, default: str) -> str:
    arg = exc.args[0] if exc.args else None
    return arg if isinstance(arg, str) and _CODE.fullmatch(arg) else default


async def _issue_respond(
    send: Send,
    status: int,
    code: str,
    payload: dict[str, Any] | None = None,
    *,
    body: bool = True,
) -> None:
    _log.info("issue_token status=%d code=%s", status, code)
    await _send_json(
        send,
        status,
        payload if payload is not None else {"error": code},
        body=body,
        extra_headers=[(b"cache-control", b"no-store")],
    )


async def _send_json(
    send: Send,
    status: int,
    payload: dict[str, Any],
    *,
    body: bool = True,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    encoded = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                # The length the GET would have, as RFC 9110 allows for HEAD.
                (b"content-length", str(len(encoded)).encode("ascii")),
                *(extra_headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": encoded if body else b""})
