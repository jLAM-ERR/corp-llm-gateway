"""Real-DB integration tests for PostgresTokenStore.

Skips when asyncpg is absent OR the demo Postgres is unreachable.
DSN: CORP_LLM_PG_DSN env var → demo stack default (localhost:5432).
Demo credentials: gateway/gateway/gateway (from docker-compose.demo.yml).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from corp_llm_gateway.tokens.models import TokenInfo

_DEMO_PG_DSN = "postgresql://gateway:gateway@localhost:5432/gateway"


def _dsn() -> str:
    return os.environ.get("CORP_LLM_PG_DSN", _DEMO_PG_DSN)


def _tok(prefix: str = "pg-itest") -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def _info(
    corp_token: str,
    *,
    user_id: str = "pg-test-alice",
    revoked_at: datetime | None = None,
) -> TokenInfo:
    now = datetime.now(UTC)
    return TokenInfo(
        corp_token=corp_token,
        user_id=user_id,
        team_id="t1",
        scopes=("read", "write"),
        issued_at=now,
        expires_at=now + timedelta(days=30),
        revoked_at=revoked_at,
    )


@pytest_asyncio.fixture
async def pg_store() -> AsyncIterator[object]:
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        pytest.skip("asyncpg not installed")

    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    store = PostgresTokenStore(_dsn())
    try:
        await store.init_schema()
        pool = await store._get_pool()
        async with pool.acquire() as conn:
            # clean slate: remove any leftover pg-test-* rows
            await conn.execute("DELETE FROM corp_tokens WHERE user_id LIKE 'pg-test-%'")
    except Exception as exc:
        await store.close()
        pytest.skip(f"Postgres unreachable: {exc}")
    yield store
    try:
        pool = await store._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM corp_tokens WHERE user_id LIKE 'pg-test-%'")
    except Exception:
        pass
    await store.close()


@pytest.mark.asyncio
async def test_pg_lookup_unknown_returns_none(pg_store: object) -> None:
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    assert isinstance(pg_store, PostgresTokenStore)
    assert await pg_store.lookup(_tok("pg-missing")) is None


@pytest.mark.asyncio
async def test_pg_upsert_and_lookup(pg_store: object) -> None:
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    assert isinstance(pg_store, PostgresTokenStore)
    tok = _tok()
    await pg_store.upsert(_info(tok))
    got = await pg_store.lookup(tok)
    assert got is not None
    assert got.corp_token == tok
    assert got.user_id == "pg-test-alice"
    assert got.scopes == ("read", "write")
    assert got.revoked_at is None
    assert got.issued_at.tzinfo is not None
    assert got.expires_at.tzinfo is not None


@pytest.mark.asyncio
async def test_pg_revoke_reflects_in_lookup(pg_store: object) -> None:
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    assert isinstance(pg_store, PostgresTokenStore)
    tok = _tok()
    await pg_store.upsert(_info(tok))
    n = await pg_store.revoke_user("pg-test-alice")
    assert n == 1
    got = await pg_store.lookup(tok)
    assert got is not None
    assert got.revoked_at is not None
    assert got.revoked_at.tzinfo is not None


@pytest.mark.asyncio
async def test_pg_revoke_idempotent(pg_store: object) -> None:
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    assert isinstance(pg_store, PostgresTokenStore)
    tok = _tok()
    await pg_store.upsert(_info(tok))
    n1 = await pg_store.revoke_user("pg-test-alice")
    n2 = await pg_store.revoke_user("pg-test-alice")
    assert n1 == 1
    assert n2 == 0


@pytest.mark.asyncio
async def test_pg_upsert_overwrite(pg_store: object) -> None:
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    assert isinstance(pg_store, PostgresTokenStore)
    tok = _tok()
    await pg_store.upsert(_info(tok))
    now = datetime.now(UTC)
    await pg_store.upsert(_info(tok, revoked_at=now))
    got = await pg_store.lookup(tok)
    assert got is not None
    assert got.revoked_at is not None


@pytest.mark.asyncio
async def test_pg_revoke_only_affects_target_user(pg_store: object) -> None:
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    assert isinstance(pg_store, PostgresTokenStore)
    tok_alice = _tok()
    tok_bob = _tok()
    await pg_store.upsert(_info(tok_alice, user_id="pg-test-alice"))
    await pg_store.upsert(_info(tok_bob, user_id="pg-test-bob"))
    n = await pg_store.revoke_user("pg-test-alice")
    assert n == 1
    a = await pg_store.lookup(tok_alice)
    b = await pg_store.lookup(tok_bob)
    assert a is not None and a.revoked_at is not None
    assert b is not None and b.revoked_at is None
