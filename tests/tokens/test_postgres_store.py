"""Real-DB integration tests for PostgresTokenStore.

Skips when asyncpg is absent OR the demo Postgres is unreachable; fails
instead on CI (see tests/postgres_support.py).
DSN: CORP_TEST_PG_DSN → CORP_LLM_PG_DSN → demo stack default (localhost:5432).
Demo credentials: gateway/gateway/gateway (from docker-compose.demo.yml).
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from corp_llm_gateway.tokens.errors import IssuancePolicyError
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


_ISS = "https://kc.corp.test/realms/dev"


async def _issue_for(store: object, corp_token: str, *, subject: str, jti: str) -> TokenInfo:
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    assert isinstance(store, PostgresTokenStore)
    return await store.issue_for_subject(
        _info(corp_token),
        issuer=_ISS,
        subject=subject,
        jti=jti,
        max_active=2,
        min_interval=timedelta(minutes=10),
    )


async def _wait_for_lock_waiter(pool: object) -> None:
    async with pool.acquire() as conn:  # type: ignore[attr-defined]
        for _ in range(500):
            waiting = await conn.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            if waiting:
                return
            await asyncio.sleep(0.01)
    pytest.fail("issuance never blocked on the uncommitted jti row")


@pytest.mark.asyncio
async def test_pg_jti_unique_violation_is_a_replay_without_driver_detail(
    pg_store: object,
) -> None:
    """The in-transaction jti check cannot see an uncommitted twin; the unique
    index can. The mapped error must not chain the driver's error, whose detail
    quotes the jti."""
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    assert isinstance(pg_store, PostgresTokenStore)
    jti = f"jti-uv-{secrets.token_hex(4)}"
    tok = _tok()
    pool = await pg_store._get_pool()
    async with pool.acquire() as other:
        tx = other.transaction()
        await tx.start()
        try:
            await other.execute(
                "INSERT INTO corp_tokens (corp_token, user_id, team_id, expires_at, "
                "oidc_issuer, oidc_subject, oidc_jti) "
                "VALUES ($1, 'pg-test-bob', 't1', now() + interval '1 day', $2, $3, $4)",
                _tok(),
                _ISS,
                f"sub-bob-{secrets.token_hex(4)}",
                jti,
            )
            task = asyncio.create_task(
                _issue_for(pg_store, tok, subject=f"sub-alice-{secrets.token_hex(4)}", jti=jti)
            )
            await _wait_for_lock_waiter(pool)
        except BaseException:
            await tx.rollback()
            raise
        await tx.commit()

    with pytest.raises(IssuancePolicyError) as exc_info:
        await task

    exc = exc_info.value
    assert exc.args == ("E_ISSUE_REPLAY",)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert jti not in f"{exc!s} {exc!r}"
    assert await pg_store.lookup(tok) is None


@pytest.mark.asyncio
async def test_pg_subject_lock_wait_times_out_as_busy(
    pg_store: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from corp_llm_gateway.tokens import postgres_store
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    assert isinstance(pg_store, PostgresTokenStore)
    monkeypatch.setattr(postgres_store, "_ISSUE_LOCK_TIMEOUT", "50ms")
    subject = f"sub-busy-{secrets.token_hex(4)}"
    tok = _tok()
    pool = await pg_store._get_pool()
    async with pool.acquire() as holder, holder.transaction():
        await holder.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"{_ISS}\x1f{subject}")
        with pytest.raises(IssuancePolicyError) as exc_info:
            await _issue_for(pg_store, tok, subject=subject, jti=f"jti-busy-{secrets.token_hex(4)}")

    exc = exc_info.value
    assert exc.code == IssuancePolicyError.BUSY == "E_ISSUE_BUSY"
    assert exc.args == ("E_ISSUE_BUSY",)
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert await pg_store.lookup(tok) is None
    # The lock is per subject: once released, the same request goes through.
    await _issue_for(pg_store, tok, subject=subject, jti=f"jti-busy-{secrets.token_hex(4)}")
    assert await pg_store.lookup(tok) is not None


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


async def _issue_many(
    store: object, subject: str, count: int, *, prefix: str
) -> tuple[list[str], list[BaseException]]:
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    assert isinstance(store, PostgresTokenStore)
    tokens = [_tok(f"{prefix}-{i}") for i in range(count)]
    now = datetime.now(UTC)
    results = await asyncio.gather(
        *(
            store.issue_for_subject(
                dataclasses.replace(_info(tok), issued_at=now),
                issuer=_ISS,
                subject=subject,
                jti=f"jti-{prefix}-{secrets.token_hex(6)}",
                max_active=2,
                min_interval=timedelta(0),
            )
            for tok in tokens
        ),
        return_exceptions=True,
    )
    return tokens, [r for r in results if isinstance(r, BaseException)]


@pytest.mark.asyncio
async def test_pg_colliding_subject_locks_serialise_but_never_mix_rows(
    pg_store: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every subject hashes to one lock key here: a real hashtext collision."""
    from corp_llm_gateway.tokens import postgres_store
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    assert isinstance(pg_store, PostgresTokenStore)
    monkeypatch.setattr(
        postgres_store, "_SUBJECT_LOCK_SQL", "SELECT pg_advisory_xact_lock(hashtext($1) * 0 + 4242)"
    )
    monkeypatch.setattr(postgres_store, "_ISSUE_LOCK_TIMEOUT", "50ms")
    alice = f"sub-coll-a-{secrets.token_hex(4)}"
    bob = f"sub-coll-b-{secrets.token_hex(4)}"

    pool = await pg_store._get_pool()
    async with pool.acquire() as holder, holder.transaction():
        await holder.execute("SELECT pg_advisory_xact_lock(4242)")
        with pytest.raises(IssuancePolicyError) as exc_info:
            await _issue_for(pg_store, _tok(), subject=bob, jti=f"jti-{secrets.token_hex(4)}")
    assert exc_info.value.code == IssuancePolicyError.BUSY

    monkeypatch.setattr(postgres_store, "_ISSUE_LOCK_TIMEOUT", "5s")
    (a_tokens, a_errors), (b_tokens, b_errors) = await asyncio.gather(
        _issue_many(pg_store, alice, 5, prefix="pg-coll-a"),
        _issue_many(pg_store, bob, 5, prefix="pg-coll-b"),
    )

    assert a_errors == [] and b_errors == []
    for tokens in (a_tokens, b_tokens):
        rows = [await pg_store.lookup(t) for t in tokens]
        assert all(row is not None for row in rows)
        assert sum(row.revoked_at is None for row in rows if row is not None) == 2


@pytest.mark.asyncio
async def test_pg_issuance_waits_for_a_pool_connection_instead_of_failing() -> None:
    require_asyncpg()
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    store = PostgresTokenStore(_dsn(), pool_max_size=2)
    try:
        await store.init_schema()
    except Exception as exc:
        await store.close()
        skip_or_fail(f"Postgres unreachable: {exc}")
    tokens = [_tok("pg-pool") for _ in range(12)]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    store.issue_for_subject(
                        _info(tok),
                        issuer=_ISS,
                        subject=f"sub-pool-{secrets.token_hex(6)}",
                        jti=f"jti-pool-{secrets.token_hex(6)}",
                        max_active=2,
                        min_interval=timedelta(minutes=10),
                    )
                    for tok in tokens
                ),
                return_exceptions=True,
            ),
            timeout=30,
        )
        assert [r for r in results if isinstance(r, BaseException)] == []
        assert all([await store.lookup(tok) is not None for tok in tokens])
    finally:
        pool = await store._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM corp_tokens WHERE user_id LIKE 'pg-test-%'")
        await store.close()
