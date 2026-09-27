"""Real-DB integration tests for PostgresTokenStore.

Skips when asyncpg is absent OR the demo Postgres is unreachable; fails
instead on CI (see tests/postgres_support.py).
DSN: CORP_TEST_PG_DSN → CORP_LLM_PG_DSN → demo stack default (localhost:5432).
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
from tests.postgres_support import PG_DSN_ENV_VAR, pg_dsn, require_asyncpg, skip_or_fail


def _dsn() -> str:
    return os.environ.get(PG_DSN_ENV_VAR) or os.environ.get("CORP_LLM_PG_DSN") or pg_dsn()


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
    require_asyncpg()

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
        skip_or_fail(f"Postgres unreachable: {exc}")
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


_PRE_OIDC_CORP_TOKENS = """
CREATE TABLE corp_tokens (
    corp_token           TEXT PRIMARY KEY,
    user_id              TEXT NOT NULL,
    team_id              TEXT NOT NULL,
    scopes               TEXT[] NOT NULL DEFAULT '{}',
    issued_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at           TIMESTAMPTZ NOT NULL,
    revoked_at           TIMESTAMPTZ,
    last_used_at         TIMESTAMPTZ
)
"""


@pytest.mark.asyncio
async def test_init_schema_adds_oidc_columns_to_preexisting_table() -> None:
    """A DB created before developer issuance lacks the oidc_* columns; re-running
    the schema must add them and keep existing (CLI-issued) rows usable."""
    require_asyncpg()
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    store = PostgresTokenStore(_dsn())
    try:
        pool = await store._get_pool()
    except Exception as exc:
        await store.close()
        skip_or_fail(f"Postgres unreachable: {exc}")
    try:
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS corp_tokens CASCADE")
            await conn.execute(_PRE_OIDC_CORP_TOKENS)
        legacy = _tok("pg-legacy")
        await store.upsert(_info(legacy))

        await store.init_schema()
        await store.init_schema()

        now = datetime.now(UTC)
        issued = _info(_tok("pg-oidc"))
        await store.issue_for_subject(
            TokenInfo(
                corp_token=issued.corp_token,
                user_id=issued.user_id,
                team_id=issued.team_id,
                scopes=issued.scopes,
                issued_at=now,
                expires_at=now + timedelta(days=30),
            ),
            issuer="https://kc.corp.test/realms/dev",
            subject="sub-legacy-test",
            jti="jti-legacy-test",
            max_active=1,
            min_interval=timedelta(minutes=10),
        )
        got = await store.lookup(issued.corp_token)
        old = await store.lookup(legacy)
        assert got is not None and got.revoked_at is None
        assert old is not None and old.revoked_at is None
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS corp_tokens CASCADE")
        await store.init_schema()
        await store.close()
