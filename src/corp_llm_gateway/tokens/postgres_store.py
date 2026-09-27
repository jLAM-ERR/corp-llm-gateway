"""Postgres-backed TokenStore via asyncpg.

asyncpg is optional (install via the 'postgres' extra):
    pip install 'corp-llm-gateway[postgres]'

This module is safe to import without asyncpg present. RuntimeError is
raised at instantiation time when asyncpg is absent.
"""

from __future__ import annotations

import asyncio
import dataclasses
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from corp_llm_gateway.pg_session import (
    KEEPALIVE_SERVER_SETTINGS,
    RELEASE_BUDGET_S,
    in_transaction,
    run_on_connection,
)
from corp_llm_gateway.tokens.errors import IssuancePolicyError
from corp_llm_gateway.tokens.models import TokenInfo
from corp_llm_gateway.tokens.store import TokenStore

_SCHEMA_SQL = Path(__file__).parent / "schema.sql"

_JTI_UNIQUE_INDEX = "corp_tokens_oidc_jti_key"

# Serialises issuance per (issuer, subject) across replicas, including the very
# first issuance when there are no rows to lock. A hash collision only
# over-serialises two subjects; it never mixes their rows.
_SUBJECT_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext($1))"
_SUBJECT_KEY_SEPARATOR = "\x1f"

# Bounds every lock wait inside the issuance transaction (the subject lock, and a
# unique-index wait on a racing jti); a timeout surfaces as E_ISSUE_BUSY.
_ISSUE_LOCK_TIMEOUT = "5s"
# Bounds each statement of that transaction; also E_ISSUE_BUSY. Under the route's
# own bound (CORP_GATEWAY_ISSUE_STORE_TIMEOUT_SECONDS, default 10 s).
_ISSUE_STATEMENT_TIMEOUT = "8s"
# Waiting for a pooled connection, and opening one; asyncio.TimeoutError past it.
_ACQUIRE_TIMEOUT_S = 5.0
_CONNECT_TIMEOUT_S = 5.0
# Returning an issuance connection to the pool; past it the connection is dropped.
_RELEASE_BUDGET_S = RELEASE_BUDGET_S
# The auth hot path's lookup statement, client-side: it also bounds a server that
# stopped answering. Past it: TimeoutError, the connection is abandoned.
LOOKUP_TIMEOUT_S = 5.0

_asyncpg_mod: types.ModuleType | None = None
_asyncpg_tried = False


def _get_asyncpg() -> types.ModuleType | None:
    global _asyncpg_mod, _asyncpg_tried
    if _asyncpg_tried:
        return _asyncpg_mod
    _asyncpg_tried = True
    try:
        import asyncpg

        _asyncpg_mod = asyncpg
    except ImportError:
        pass
    return _asyncpg_mod


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _ensure_utc_opt(dt: datetime | None) -> datetime | None:
    return None if dt is None else _ensure_utc(dt)


def _row_to_token_info(row: Any) -> TokenInfo:
    return TokenInfo(
        corp_token=row["corp_token"],
        user_id=row["user_id"],
        team_id=row["team_id"],
        scopes=tuple(row["scopes"]),
        issued_at=_ensure_utc(row["issued_at"]),
        expires_at=_ensure_utc(row["expires_at"]),
        revoked_at=_ensure_utc_opt(row["revoked_at"]),
    )


class PostgresTokenStore(TokenStore):
    """Postgres-backed TokenStore using an asyncpg connection pool.

    The pool is created lazily on first use. Call close() at shutdown.
    DSN config key: CORP_LLM_PG_DSN.
    """

    def __init__(self, dsn: str, *, pool_max_size: int = 5) -> None:
        if _get_asyncpg() is None:
            raise RuntimeError(
                "PostgresTokenStore requires asyncpg: pip install 'corp-llm-gateway[postgres]'"
            )
        self._dsn = dsn
        self._pool_max_size = pool_max_size
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        asyncpg_mod = _get_asyncpg()
        assert asyncpg_mod is not None  # guarded in __init__
        async with self._lock:
            if self._pool is None:
                self._pool = await asyncpg_mod.create_pool(  # type: ignore[attr-defined]
                    self._dsn,
                    min_size=1,
                    max_size=self._pool_max_size,
                    timeout=_CONNECT_TIMEOUT_S,
                    server_settings=KEEPALIVE_SERVER_SETTINGS,
                )
        return self._pool

    def _acquire(self, pool: Any) -> Any:
        return pool.acquire(timeout=_ACQUIRE_TIMEOUT_S)

    async def init_schema(self) -> None:
        """Apply schema.sql idempotently; safe on an already-initialised DB."""
        pool = await self._get_pool()
        sql = _SCHEMA_SQL.read_text()
        async with self._acquire(pool) as conn:
            await conn.execute(sql)

    async def upsert(self, info: TokenInfo) -> None:
        """Insert or replace a token record."""
        pool = await self._get_pool()
        async with self._acquire(pool) as conn:
            await conn.execute(
                """
                INSERT INTO corp_tokens
                    (corp_token, user_id, team_id, scopes,
                     issued_at, expires_at, revoked_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (corp_token) DO UPDATE SET
                    user_id    = EXCLUDED.user_id,
                    team_id    = EXCLUDED.team_id,
                    scopes     = EXCLUDED.scopes,
                    issued_at  = EXCLUDED.issued_at,
                    expires_at = EXCLUDED.expires_at,
                    revoked_at = EXCLUDED.revoked_at
                """,
                info.corp_token,
                info.user_id,
                info.team_id,
                list(info.scopes),
                info.issued_at,
                info.expires_at,
                info.revoked_at,
            )

    async def lookup(self, corp_token: str) -> TokenInfo | None:
        pool = await self._get_pool()
        row: Any = await run_on_connection(
            pool,
            lambda conn: conn.fetchrow(
                """
                SELECT corp_token, user_id, team_id, scopes,
                       issued_at, expires_at, revoked_at
                FROM corp_tokens
                WHERE corp_token = $1
                """,
                corp_token,
                timeout=LOOKUP_TIMEOUT_S,
            ),
            acquire_timeout=_ACQUIRE_TIMEOUT_S,
            release_budget=_RELEASE_BUDGET_S,
        )
        if row is None:
            return None
        return _row_to_token_info(row)

    async def revoke_user(self, user_id: str) -> int:
        pool = await self._get_pool()
        now = datetime.now(UTC)
        async with self._acquire(pool) as conn:
            # asyncpg execute returns "UPDATE N" for DML
            status: str = await conn.execute(
                """
                UPDATE corp_tokens
                SET revoked_at = $1
                WHERE user_id = $2 AND revoked_at IS NULL
                """,
                now,
                user_id,
            )
        return int(status.split()[-1])

    async def list_tokens(self, user_id: str | None = None) -> tuple[TokenInfo, ...]:
        pool = await self._get_pool()
        select = (
            "SELECT corp_token, user_id, team_id, scopes, "
            "issued_at, expires_at, revoked_at FROM corp_tokens"
        )
        async with self._acquire(pool) as conn:
            rows: Any
            if user_id is None:
                rows = await conn.fetch(f"{select} ORDER BY issued_at DESC")
            else:
                rows = await conn.fetch(
                    f"{select} WHERE user_id = $1 ORDER BY issued_at DESC",
                    user_id,
                )
        return tuple(_row_to_token_info(r) for r in rows)

    async def issue_for_subject(
        self,
        info: TokenInfo,
        *,
        issuer: str,
        subject: str,
        jti: str,
        max_active: int,
        min_interval: timedelta,
    ) -> TokenInfo:
        asyncpg_mod = _get_asyncpg()
        assert asyncpg_mod is not None  # guarded in __init__
        pool = await self._get_pool()
        stored = dataclasses.replace(info, revoked_at=None)
        now = info.issued_at
        # Raised after the except block: chaining the driver error would carry its
        # detail text, which quotes the conflicting jti or corp token.
        refused: Exception | None = None

        async def issue(conn: Any) -> None:
            await conn.execute(
                "SELECT set_config('lock_timeout', $1, true), "
                "set_config('statement_timeout', $2, true)",
                _ISSUE_LOCK_TIMEOUT,
                _ISSUE_STATEMENT_TIMEOUT,
            )
            await conn.execute(_SUBJECT_LOCK_SQL, f"{issuer}{_SUBJECT_KEY_SEPARATOR}{subject}")
            seen = await conn.fetchval("SELECT 1 FROM corp_tokens WHERE oidc_jti = $1", jti)
            if seen is not None:
                raise IssuancePolicyError(IssuancePolicyError.REPLAY)
            # The interval is an issuance rate: revoked and expired rows count.
            latest = await conn.fetchval(
                "SELECT max(issued_at) FROM corp_tokens "
                "WHERE oidc_issuer = $1 AND oidc_subject = $2",
                issuer,
                subject,
            )
            if latest is not None and now - _ensure_utc(latest) < min_interval:
                raise IssuancePolicyError(IssuancePolicyError.RATE)
            active: Any = await conn.fetch(
                """
                SELECT corp_token
                FROM corp_tokens
                WHERE oidc_issuer = $1 AND oidc_subject = $2
                  AND revoked_at IS NULL AND expires_at > $3
                ORDER BY issued_at, corp_token
                """,
                issuer,
                subject,
                now,
            )
            excess = len(active) - max_active + 1
            if excess > 0:
                await conn.execute(
                    "UPDATE corp_tokens SET revoked_at = $1 WHERE corp_token = ANY($2::text[])",
                    now,
                    [row["corp_token"] for row in active[:excess]],
                )
            await conn.execute(
                """
                INSERT INTO corp_tokens
                    (corp_token, user_id, team_id, scopes, issued_at,
                     expires_at, revoked_at, oidc_issuer, oidc_subject, oidc_jti)
                VALUES ($1, $2, $3, $4, $5, $6, NULL, $7, $8, $9)
                """,
                info.corp_token,
                info.user_id,
                info.team_id,
                list(info.scopes),
                info.issued_at,
                info.expires_at,
                issuer,
                subject,
                jti,
            )

        try:
            # READ COMMITTED: each statement after the lock sees the previous
            # holder's commit; a snapshot taken at the lock would not.
            await run_on_connection(
                pool,
                lambda conn: in_transaction(conn, issue, isolation="read_committed"),
                acquire_timeout=_ACQUIRE_TIMEOUT_S,
                release_budget=_RELEASE_BUDGET_S,
            )
        except asyncpg_mod.exceptions.UniqueViolationError as exc:
            if exc.constraint_name == _JTI_UNIQUE_INDEX:
                refused = IssuancePolicyError(IssuancePolicyError.REPLAY)
            else:
                refused = RuntimeError("corp_tokens unique violation on issuance")
        except (
            asyncpg_mod.exceptions.LockNotAvailableError,
            asyncpg_mod.exceptions.QueryCanceledError,
        ):
            refused = IssuancePolicyError(IssuancePolicyError.BUSY)
        if refused is not None:
            raise refused
        return stored

    async def close(self) -> None:
        """Close the connection pool; no-op if pool was never created."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
