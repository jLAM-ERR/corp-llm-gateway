"""Postgres-backed TeamConfigStore via asyncpg.

asyncpg is optional (install via the 'postgres' extra):
    pip install 'corp-llm-gateway[postgres]'

This module is safe to import AND construct without asyncpg present — bootstrap
selects the class the moment a DSN is configured, well before Python 3.12 / the
'postgres' extra is guaranteed. RuntimeError is raised lazily, on the first
query that actually needs a connection pool.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path
from typing import Any

from corp_llm_gateway.pg_session import (
    KEEPALIVE_SERVER_SETTINGS,
    RELEASE_BUDGET_S,
    run_on_connection,
)
from corp_llm_gateway.team_config.models import FailPolicyOverrides, TeamConfig
from corp_llm_gateway.team_config.store import TeamConfigStore, TeamNotFoundError

_SCHEMA_SQL = Path(__file__).parent / "schema.sql"

_asyncpg_mod: types.ModuleType | None = None
_asyncpg_tried = False

# Waiting for a pooled connection, and opening one; asyncio.TimeoutError past it.
_ACQUIRE_TIMEOUT_S = 5.0
_CONNECT_TIMEOUT_S = 5.0
# Returning a `get` connection to the pool; past it the connection is dropped.
_RELEASE_BUDGET_S = RELEASE_BUDGET_S
# The `get` statement, client-side: it also bounds a server that stopped answering.
_GET_TIMEOUT_S = 5.0

_COLUMNS = (
    "team_id, name, replace_md_path, profile_ids, "
    "retention_hot_days, retention_cold_years, fail_policy"
)


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


def _fail_policy_to_json(fp: FailPolicyOverrides) -> str:
    return json.dumps(
        {
            "pre_pass_down": fp.pre_pass_down,
            "audit_sink_down": fp.audit_sink_down,
            "audit_buffer_full": fp.audit_buffer_full,
        }
    )


def _fail_policy_from_json(raw: Any) -> FailPolicyOverrides:
    if not raw:
        return FailPolicyOverrides()
    data: Any = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict) or not data:
        return FailPolicyOverrides()
    defaults = FailPolicyOverrides()
    return FailPolicyOverrides(
        pre_pass_down=data.get("pre_pass_down", defaults.pre_pass_down),
        audit_sink_down=data.get("audit_sink_down", defaults.audit_sink_down),
        audit_buffer_full=data.get("audit_buffer_full", defaults.audit_buffer_full),
    )


def _row_to_team_config(row: Any) -> TeamConfig:
    return TeamConfig(
        team_id=row["team_id"],
        name=row["name"],
        replace_md_path=row["replace_md_path"],
        profile_ids=tuple(row["profile_ids"] or ()),
        retention_hot_days=row["retention_hot_days"],
        retention_cold_years=row["retention_cold_years"],
        fail_policy=_fail_policy_from_json(row["fail_policy"]),
    )


class PostgresTeamConfigStore(TeamConfigStore):
    """Postgres-backed TeamConfigStore using an asyncpg connection pool.

    The pool is created lazily on first use. Call close() at shutdown.
    DSN config key: CORP_LLM_PG_DSN.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        asyncpg_mod = _get_asyncpg()
        if asyncpg_mod is None:
            raise RuntimeError(
                "PostgresTeamConfigStore requires asyncpg: pip install 'corp-llm-gateway[postgres]'"
            )
        async with self._lock:
            if self._pool is None:
                self._pool = await asyncpg_mod.create_pool(  # type: ignore[attr-defined]
                    self._dsn,
                    min_size=1,
                    max_size=5,
                    timeout=_CONNECT_TIMEOUT_S,
                    server_settings=KEEPALIVE_SERVER_SETTINGS,
                )
        return self._pool

    def _acquire(self, pool: Any) -> Any:
        return pool.acquire(timeout=_ACQUIRE_TIMEOUT_S)

    async def init_schema(self, conn: Any = None) -> None:
        """Apply schema.sql idempotently; safe on an already-initialised DB.

        On ``conn`` when given (the caller owns its session settings and locks),
        else on a pooled connection."""
        sql = _SCHEMA_SQL.read_text()
        if conn is not None:
            await conn.execute(sql)
            return
        pool = await self._get_pool()
        async with self._acquire(pool) as pooled:
            await pooled.execute(sql)

    async def get(self, team_id: str) -> TeamConfig:
        # On the issuance route and the request path: bounded like issuance.
        pool = await self._get_pool()
        row: Any = await run_on_connection(
            pool,
            lambda conn: conn.fetchrow(
                f"SELECT {_COLUMNS} FROM team_config WHERE team_id = $1",
                team_id,
                timeout=_GET_TIMEOUT_S,
            ),
            acquire_timeout=_ACQUIRE_TIMEOUT_S,
            release_budget=_RELEASE_BUDGET_S,
        )
        if row is None:
            raise TeamNotFoundError(team_id)
        return _row_to_team_config(row)

    async def upsert(self, config: TeamConfig) -> None:
        pool = await self._get_pool()
        async with self._acquire(pool) as conn:
            await conn.execute(
                """
                INSERT INTO team_config
                    (team_id, name, replace_md_path, profile_ids,
                     retention_hot_days, retention_cold_years, fail_policy)
                VALUES ($1, $2, $3, $4::text[], $5, $6, $7::jsonb)
                ON CONFLICT (team_id) DO UPDATE SET
                    name                 = EXCLUDED.name,
                    replace_md_path      = EXCLUDED.replace_md_path,
                    profile_ids          = EXCLUDED.profile_ids,
                    retention_hot_days   = EXCLUDED.retention_hot_days,
                    retention_cold_years = EXCLUDED.retention_cold_years,
                    fail_policy          = EXCLUDED.fail_policy
                """,
                config.team_id,
                config.name,
                config.replace_md_path,
                list(config.profile_ids),
                config.retention_hot_days,
                config.retention_cold_years,
                _fail_policy_to_json(config.fail_policy),
            )

    async def create_if_absent(self, config: TeamConfig) -> bool:
        pool = await self._get_pool()
        async with self._acquire(pool) as conn:
            status: str = await conn.execute(
                """
                INSERT INTO team_config
                    (team_id, name, replace_md_path, profile_ids,
                     retention_hot_days, retention_cold_years, fail_policy)
                VALUES ($1, $2, $3, $4::text[], $5, $6, $7::jsonb)
                ON CONFLICT (team_id) DO NOTHING
                """,
                config.team_id,
                config.name,
                config.replace_md_path,
                list(config.profile_ids),
                config.retention_hot_days,
                config.retention_cold_years,
                _fail_policy_to_json(config.fail_policy),
            )
        return status == "INSERT 0 1"

    async def list_all(self) -> tuple[TeamConfig, ...]:
        pool = await self._get_pool()
        async with self._acquire(pool) as conn:
            rows: Any = await conn.fetch(f"SELECT {_COLUMNS} FROM team_config ORDER BY team_id")
        return tuple(_row_to_team_config(r) for r in rows)

    async def close(self) -> None:
        """Close the connection pool; no-op if pool was never created."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
