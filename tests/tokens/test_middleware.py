import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from corp_llm_gateway.tokens import (
    AuthMiddleware,
    ExpiredTokenError,
    InMemoryTokenStore,
    InvalidTokenError,
    MissingTokenError,
    PostgresTokenStore,
    RevokedTokenError,
    TokenInfo,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _info(
    corp_token: str = "tok-1",
    *,
    revoked_at: datetime | None = None,
    expires_in: timedelta = timedelta(days=30),
    user_id: str = "alice",
    team_id: str = "t1",
) -> TokenInfo:
    now = _now()
    return TokenInfo(
        corp_token=corp_token,
        user_id=user_id,
        team_id=team_id,
        scopes=("read",),
        issued_at=now,
        expires_at=now + expires_in,
        revoked_at=revoked_at,
    )


# Header extraction & stripping ---------------------------------------------


def test_strip_corp_token_removes_header_case_insensitively() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    headers = {"X-Corp-Auth": "tok", "Authorization": "Bearer x", "Accept": "*/*"}
    out = mw.strip_corp_token(headers)
    assert "X-Corp-Auth" not in out
    assert "Authorization" in out
    assert "Accept" in out


def test_strip_corp_token_lower_cased_header() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    headers = {"x-corp-auth": "tok", "Authorization": "Bearer x"}
    out = mw.strip_corp_token(headers)
    assert "x-corp-auth" not in out
    assert "Authorization" in out


def test_strip_corp_token_does_not_mutate_input() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    headers = {"X-Corp-Auth": "tok"}
    mw.strip_corp_token(headers)
    assert "X-Corp-Auth" in headers


# authenticate() — happy & error paths --------------------------------------


@pytest.mark.asyncio
async def test_missing_token_raises() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    with pytest.raises(MissingTokenError):
        await mw.authenticate(None)


@pytest.mark.asyncio
async def test_empty_token_raises() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    with pytest.raises(MissingTokenError):
        await mw.authenticate("")


@pytest.mark.asyncio
async def test_unknown_token_raises_invalid() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    with pytest.raises(InvalidTokenError):
        await mw.authenticate("nonexistent")


@pytest.mark.asyncio
async def test_valid_token_returns_context() -> None:
    store = InMemoryTokenStore()
    store.upsert(_info(corp_token="tok-1"))
    mw = AuthMiddleware(store)
    ctx = await mw.authenticate("tok-1")
    assert ctx.user_id == "alice"
    assert ctx.team_id == "t1"
    assert ctx.scopes == ("read",)


@pytest.mark.asyncio
async def test_expired_token_raises() -> None:
    store = InMemoryTokenStore()
    store.upsert(_info(corp_token="tok-1", expires_in=timedelta(seconds=-1)))
    mw = AuthMiddleware(store)
    with pytest.raises(ExpiredTokenError):
        await mw.authenticate("tok-1")


@pytest.mark.asyncio
async def test_revoked_token_raises() -> None:
    store = InMemoryTokenStore()
    info = _info(corp_token="tok-1")
    store.upsert(info)
    await store.revoke_user("alice")
    mw = AuthMiddleware(store)
    with pytest.raises(RevokedTokenError):
        await mw.authenticate("tok-1")


# Header-based auth ---------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_headers_picks_corp_auth() -> None:
    store = InMemoryTokenStore()
    store.upsert(_info(corp_token="tok-1"))
    mw = AuthMiddleware(store)
    ctx = await mw.authenticate_headers({"X-Corp-Auth": "tok-1"})
    assert ctx.user_id == "alice"


@pytest.mark.asyncio
async def test_authenticate_headers_case_insensitive() -> None:
    store = InMemoryTokenStore()
    store.upsert(_info(corp_token="tok-1"))
    mw = AuthMiddleware(store)
    ctx = await mw.authenticate_headers({"x-corp-auth": "tok-1"})
    assert ctx.user_id == "alice"


@pytest.mark.asyncio
async def test_authenticate_headers_missing_corp_auth_raises() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    with pytest.raises(MissingTokenError):
        await mw.authenticate_headers({"Authorization": "Bearer x"})


# Revocation cache lag ------------------------------------------------------


@pytest.mark.asyncio
async def test_revocation_cache_serves_stale_within_window() -> None:
    """Revoking a cached token still validates as OK until cache TTL expires."""
    store = InMemoryTokenStore()
    store.upsert(_info(corp_token="tok-1"))
    mw = AuthMiddleware(store, revocation_cache_seconds=1.0)

    ctx_before = await mw.authenticate("tok-1")
    assert ctx_before.user_id == "alice"

    await store.revoke_user("alice")

    ctx_after = await mw.authenticate("tok-1")
    assert ctx_after.user_id == "alice"


@pytest.mark.asyncio
async def test_revocation_cache_picks_up_revoke_after_ttl() -> None:
    store = InMemoryTokenStore()
    store.upsert(_info(corp_token="tok-1"))
    mw = AuthMiddleware(store, revocation_cache_seconds=0.5)

    await mw.authenticate("tok-1")
    await store.revoke_user("alice")
    await asyncio.sleep(0.7)

    with pytest.raises(RevokedTokenError):
        await mw.authenticate("tok-1")


# PostgresTokenStore import guard -------------------------------------------


def test_postgres_store_without_asyncpg_raises() -> None:
    """When asyncpg is absent, instantiating PostgresTokenStore raises RuntimeError."""
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="asyncpg"):
            PostgresTokenStore("postgresql://x")
    else:
        pytest.skip("asyncpg is installed; cannot test the absent-asyncpg path")
