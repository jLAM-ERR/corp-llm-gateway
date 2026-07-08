import httpx
import pytest

from corp_llm_gateway.healthz import build_health_router
from corp_llm_gateway.healthz.checks import (
    ExtensionsCheck,
    HealthStatus,
    LiveCheck,
    ReadyCheck,
    SanitizationCheck,
)
from corp_llm_gateway.healthz.server import HealthRouter, Receive, Scope, Send
from corp_llm_gateway.tokens import (
    InMemoryTokenStore,
    OidcClaims,
    OidcVerificationError,
    TokenIssuer,
)


async def _ok() -> bool:
    return True


async def _fail() -> bool:
    return False


async def _raise() -> bool:
    raise RuntimeError("boom")


async def _ext_healthy() -> dict[str, HealthStatus]:
    return {"audit_sink:stdout": HealthStatus(True, "ok")}


async def _ext_unhealthy() -> dict[str, HealthStatus]:
    return {"audit_sink:stdout": HealthStatus(False, "backend_down")}


def _make_issuer(*, reject: bool = False, store: InMemoryTokenStore | None = None) -> TokenIssuer:
    store = store if store is not None else InMemoryTokenStore()

    async def verifier(oidc_token: str) -> OidcClaims:
        if reject:
            raise OidcVerificationError("bad oidc token")
        return OidcClaims(user_id="alice", team_id="t1", scopes=("read",))

    return TokenIssuer(store, verifier)


def _router(
    *,
    ready: ReadyCheck | None = None,
    sanitization: SanitizationCheck | None = None,
    extensions: ExtensionsCheck | None = None,
    issuer: TokenIssuer | None = None,
    fallthrough: object | None = None,
) -> HealthRouter:
    return build_health_router(
        live_check=LiveCheck(),
        ready_check=ready if ready is not None else ReadyCheck(check_redis=_ok, check_postgres=_ok),
        sanitization_check=(
            sanitization if sanitization is not None else SanitizationCheck(run_round_trip=_ok)
        ),
        extensions_check=(
            extensions if extensions is not None else ExtensionsCheck(health_all=_ext_healthy)
        ),
        token_issuer=issuer if issuer is not None else _make_issuer(),
        fallthrough=fallthrough,  # type: ignore[arg-type]
    )


def _client(router: HealthRouter) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router),
        base_url="http://health",
    )


# Health probes -------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_returns_200() -> None:
    async with _client(_router()) as client:
        resp = await client.get("/healthz/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_ready_healthy_returns_200() -> None:
    router = _router(ready=ReadyCheck(check_redis=_ok, check_postgres=_ok))
    async with _client(router) as client:
        resp = await client.get("/healthz/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_ready_unhealthy_returns_503() -> None:
    router = _router(ready=ReadyCheck(check_redis=_fail, check_postgres=_ok))
    async with _client(router) as client:
        resp = await client.get("/healthz/ready")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unhealthy"
    assert "redis" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_sanitization_healthy_returns_200() -> None:
    router = _router(sanitization=SanitizationCheck(run_round_trip=_ok))
    async with _client(router) as client:
        resp = await client.get("/healthz/sanitization")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_sanitization_unhealthy_returns_503() -> None:
    router = _router(sanitization=SanitizationCheck(run_round_trip=_fail))
    async with _client(router) as client:
        resp = await client.get("/healthz/sanitization")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_sanitization_exception_returns_503() -> None:
    router = _router(sanitization=SanitizationCheck(run_round_trip=_raise))
    async with _client(router) as client:
        resp = await client.get("/healthz/sanitization")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_extensions_healthy_returns_200() -> None:
    router = _router(extensions=ExtensionsCheck(health_all=_ext_healthy))
    async with _client(router) as client:
        resp = await client.get("/healthz/extensions")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_extensions_unhealthy_returns_503() -> None:
    router = _router(extensions=ExtensionsCheck(health_all=_ext_unhealthy))
    async with _client(router) as client:
        resp = await client.get("/healthz/extensions")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unhealthy"


@pytest.mark.asyncio
async def test_ready_ignores_extension_health() -> None:
    # A deep-check, not an LB gate: an unhealthy extension flips /extensions to
    # 503 but must leave /ready green (else a flapping sink yo-yos the pod).
    router = _router(
        ready=ReadyCheck(check_redis=_ok, check_postgres=_ok),
        extensions=ExtensionsCheck(health_all=_ext_unhealthy),
    )
    async with _client(router) as client:
        ready = await client.get("/healthz/ready")
        ext = await client.get("/healthz/extensions")
    assert ready.status_code == 200
    assert ready.json()["status"] == "healthy"
    assert ext.status_code == 503


# Token issuance ------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_token_via_bearer_header_returns_valid_token() -> None:
    store = InMemoryTokenStore()
    router = _router(issuer=_make_issuer(store=store))
    async with _client(router) as client:
        resp = await client.post(
            "/internal/issue-token",
            headers={"Authorization": "Bearer valid-oidc"},
        )
    assert resp.status_code == 200
    corp_token = resp.json()["corp_token"]
    assert corp_token.startswith("ct_")
    assert "expires_at" in resp.json()
    # A valid token is one the issuer actually persisted.
    assert await store.lookup(corp_token) is not None


@pytest.mark.asyncio
async def test_issue_token_via_json_body_returns_valid_token() -> None:
    router = _router(issuer=_make_issuer())
    async with _client(router) as client:
        resp = await client.post("/internal/issue-token", json={"oidc_token": "valid-oidc"})
    assert resp.status_code == 200
    assert resp.json()["corp_token"].startswith("ct_")


@pytest.mark.asyncio
async def test_issue_token_missing_token_returns_400() -> None:
    router = _router(issuer=_make_issuer())
    async with _client(router) as client:
        resp = await client.post("/internal/issue-token")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_issue_token_rejected_oidc_returns_401() -> None:
    router = _router(issuer=_make_issuer(reject=True))
    async with _client(router) as client:
        resp = await client.post(
            "/internal/issue-token",
            headers={"Authorization": "Bearer bad-oidc"},
        )
    assert resp.status_code == 401
    assert "bad-oidc" not in resp.text  # M1-14: the token must not echo back


# Routing edges -------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_path_returns_404() -> None:
    async with _client(_router()) as client:
        resp = await client.get("/does-not-exist")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_wrong_method_returns_405() -> None:
    async with _client(_router()) as client:
        live_post = await client.post("/healthz/live")
        issue_get = await client.get("/internal/issue-token")
    assert live_post.status_code == 405
    assert issue_get.status_code == 405


@pytest.mark.asyncio
async def test_fallthrough_delegates_unknown_paths_but_serves_health() -> None:
    seen: dict[str, str] = {}

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        seen["path"] = scope["path"]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"downstream"})

    router = _router(fallthrough=downstream)
    async with _client(router) as client:
        proxied = await client.get("/v1/chat/completions")
        health = await client.get("/healthz/live")
    assert proxied.status_code == 200
    assert proxied.text == "downstream"
    assert seen["path"] == "/v1/chat/completions"
    # The router still owns its own routes; they are not delegated.
    assert health.status_code == 200
    assert health.json()["status"] == "healthy"
