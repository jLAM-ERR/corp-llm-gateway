"""Parametrised TokenStore contract tests.

Runs against InMemoryTokenStore (always) and PostgresTokenStore (skips when
asyncpg is absent or the demo Postgres is unreachable).

Pattern mirrors tests/storage/test_mapping_store.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio

from corp_llm_gateway.tokens import InMemoryTokenStore, TokenInfo
from corp_llm_gateway.tokens.store import TokenStore

_DEMO_PG_DSN = "postgresql://gateway:gateway@localhost:5432/gateway"

StoreFactory = Callable[[], Awaitable[TokenStore]]


async def _make_in_memory() -> TokenStore:
    return InMemoryTokenStore()


async def _try_make_postgres() -> TokenStore:
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.skip("asyncpg not installed")
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    store = PostgresTokenStore(_DEMO_PG_DSN)
    try:
        await store.init_schema()
        pool = await store._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE corp_tokens")
    except Exception as exc:
        await store.close()
        pytest.skip(f"Postgres unreachable: {exc}")
    return store


@pytest_asyncio.fixture(params=[_make_in_memory, _try_make_postgres], ids=["in_memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[TokenStore]:
    factory: StoreFactory = request.param
    instance = await factory()
    yield instance
    if hasattr(instance, "close"):
        await instance.close()  # type: ignore[union-attr]


def _info(corp_token: str, user_id: str = "alice", team_id: str = "t1") -> TokenInfo:
    now = datetime.now(UTC)
    return TokenInfo(
        corp_token=corp_token,
        user_id=user_id,
        team_id=team_id,
        scopes=("read",),
        issued_at=now,
        expires_at=now + timedelta(days=30),
    )


async def _upsert(store: TokenStore, info: TokenInfo) -> None:
    """Dispatch upsert for both sync (InMemory) and async (Postgres) impls."""
    result: Any = store.upsert(info)  # type: ignore[attr-defined]
    if asyncio.iscoroutine(result):
        await result


# Contract tests -------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_unknown_returns_none(store: TokenStore) -> None:
    assert await store.lookup("ct-contract-unknown") is None


@pytest.mark.asyncio
async def test_upsert_and_lookup(store: TokenStore) -> None:
    info = _info("ct-contract-1")
    await _upsert(store, info)
    got = await store.lookup("ct-contract-1")
    assert got is not None
    assert got.user_id == "alice"
    assert got.team_id == "t1"
    assert got.scopes == ("read",)
    assert got.revoked_at is None


@pytest.mark.asyncio
async def test_upsert_overwrite(store: TokenStore) -> None:
    info = _info("ct-contract-over")
    await _upsert(store, info)
    now = datetime.now(UTC)
    revoked = TokenInfo(
        corp_token="ct-contract-over",
        user_id="alice",
        team_id="t1",
        scopes=("read",),
        issued_at=info.issued_at,
        expires_at=info.expires_at,
        revoked_at=now,
    )
    await _upsert(store, revoked)
    got = await store.lookup("ct-contract-over")
    assert got is not None and got.revoked_at is not None


@pytest.mark.asyncio
async def test_revoke_user_marks_all_their_tokens(store: TokenStore) -> None:
    await _upsert(store, _info("ct-c-tok1", user_id="alice"))
    await _upsert(store, _info("ct-c-tok2", user_id="alice"))
    await _upsert(store, _info("ct-c-tok3", user_id="bob"))

    n = await store.revoke_user("alice")
    assert n == 2

    a1 = await store.lookup("ct-c-tok1")
    a2 = await store.lookup("ct-c-tok2")
    b3 = await store.lookup("ct-c-tok3")
    assert a1 is not None and a1.revoked_at is not None
    assert a2 is not None and a2.revoked_at is not None
    assert b3 is not None and b3.revoked_at is None


@pytest.mark.asyncio
async def test_revoke_user_idempotent(store: TokenStore) -> None:
    await _upsert(store, _info("ct-c-idem1", user_id="alice"))
    n1 = await store.revoke_user("alice")
    n2 = await store.revoke_user("alice")
    assert n1 == 1
    assert n2 == 0


@pytest.mark.asyncio
async def test_revoke_unknown_user_returns_zero(store: TokenStore) -> None:
    n = await store.revoke_user("nobody-has-this-id")
    assert n == 0
