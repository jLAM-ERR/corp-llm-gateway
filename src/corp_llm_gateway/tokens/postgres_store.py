"""Postgres-backed TokenStore via asyncpg.

asyncpg is optional (install via the 'postgres' extra):
    pip install 'corp-llm-gateway[postgres]'

This module is safe to import without asyncpg present. RuntimeError is
raised at instantiation time when asyncpg is absent.
"""

from __future__ import annotations

import asyncio
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from corp_llm_gateway.tokens.models import TokenInfo
from corp_llm_gateway.tokens.store import TokenStore

_SCHEMA_SQL = Path(__file__).parent / "schema.sql"

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

    def __init__(self, dsn: str) -> None:
        if _get_asyncpg() is None:
            raise RuntimeError(
                "PostgresTokenStore requires asyncpg: pip install 'corp-llm-gateway[postgres]'"
            )
        self._dsn = dsn
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
                    max_size=5,
                )
        return self._pool

    async def init_schema(self) -> None:
        """Apply schema.sql idempotently; safe on an already-initialised DB."""
        pool = await self._get_pool()
        sql = _SCHEMA_SQL.read_text()
        async with pool.acquire() as conn:
            await conn.execute(sql)

    async def upsert(self, info: TokenInfo) -> None:
        """Insert or replace a token record."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
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
        async with pool.acquire() as conn:
            row: Any = await conn.fetchrow(
                """
                SELECT corp_token, user_id, team_id, scopes,
                       issued_at, expires_at, revoked_at
                FROM corp_tokens
                WHERE corp_token = $1
                """,
                corp_token,
            )
        if row is None:
            return None
        return _row_to_token_info(row)

    async def revoke_user(self, user_id: str) -> int:
        pool = await self._get_pool()
        now = datetime.now(UTC)
        async with pool.acquire() as conn:
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

    async def close(self) -> None:
        """Close the connection pool; no-op if pool was never created."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
