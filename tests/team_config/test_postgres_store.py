"""Parametrised TeamConfigStore contract tests.

Runs against InMemoryTeamConfigStore (always) and PostgresTeamConfigStore
(skips when asyncpg is absent or the demo Postgres is unreachable).

Pattern mirrors tests/storage/test_mapping_store.py and
tests/tokens/test_token_store_contract.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio

from corp_llm_gateway.team_config import (
    FailPolicyOverrides,
    InMemoryTeamConfigStore,
    TeamConfig,
    TeamConfigStore,
    TeamNotFoundError,
)

_DEMO_PG_DSN = "postgresql://gateway:gateway@localhost:5432/gateway"

StoreFactory = Callable[[], Awaitable[TeamConfigStore]]


async def _make_in_memory() -> TeamConfigStore:
    return InMemoryTeamConfigStore()


async def _try_make_postgres() -> TeamConfigStore:
    pytest.importorskip("asyncpg", reason="PostgresTeamConfigStore requires the 'postgres' extra")
    from corp_llm_gateway.team_config.postgres_store import PostgresTeamConfigStore

    store = PostgresTeamConfigStore(_DEMO_PG_DSN)
    try:
        await store.init_schema()
        pool = await store._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE team_config")
    except Exception as exc:
        await store.close()
        pytest.skip(f"Postgres unreachable: {exc}")
    return store


@pytest_asyncio.fixture(params=[_make_in_memory, _try_make_postgres], ids=["in_memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[TeamConfigStore]:
    factory: StoreFactory = request.param
    instance = await factory()
    yield instance
    close = getattr(instance, "close", None)
    if close is not None:
        await close()


def _team(team_id: str = "t1", **overrides: object) -> TeamConfig:
    base: dict[str, object] = {"team_id": team_id, "name": f"Team {team_id}"}
    base.update(overrides)
    return TeamConfig(**base)  # type: ignore[arg-type]


# Contract tests -------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_unknown_raises(store: TeamConfigStore) -> None:
    with pytest.raises(TeamNotFoundError):
        await store.get("nobody-has-this-id")


@pytest.mark.asyncio
async def test_upsert_and_get(store: TeamConfigStore) -> None:
    await store.upsert(_team("t1", name="One"))
    got = await store.get("t1")
    assert got.team_id == "t1"
    assert got.name == "One"


@pytest.mark.asyncio
async def test_upsert_overwrites(store: TeamConfigStore) -> None:
    await store.upsert(_team("t1", name="Original"))
    await store.upsert(_team("t1", name="Updated"))
    got = await store.get("t1")
    assert got.name == "Updated"


@pytest.mark.asyncio
async def test_list_all_empty(store: TeamConfigStore) -> None:
    assert await store.list_all() == ()


@pytest.mark.asyncio
async def test_list_all_returns_all(store: TeamConfigStore) -> None:
    await store.upsert(_team("t1"))
    await store.upsert(_team("t2"))
    teams = await store.list_all()
    assert {t.team_id for t in teams} == {"t1", "t2"}


@pytest.mark.asyncio
async def test_retention_overrides_round_trip(store: TeamConfigStore) -> None:
    await store.upsert(_team("t1", retention_hot_days=30, retention_cold_years=1))
    got = await store.get("t1")
    assert got.retention_hot_days == 30
    assert got.retention_cold_years == 1


@pytest.mark.asyncio
async def test_replace_md_path_round_trip(store: TeamConfigStore) -> None:
    await store.upsert(_team("t1", replace_md_path="/etc/rules/t1.md"))
    got = await store.get("t1")
    assert got.replace_md_path == "/etc/rules/t1.md"


@pytest.mark.asyncio
async def test_fail_policy_defaults_round_trip(store: TeamConfigStore) -> None:
    await store.upsert(_team("t1"))
    got = await store.get("t1")
    assert got.fail_policy == FailPolicyOverrides()


@pytest.mark.asyncio
async def test_fail_policy_overrides_round_trip(store: TeamConfigStore) -> None:
    overrides = FailPolicyOverrides(
        pre_pass_down="fail-closed",
        audit_sink_down="fail-closed",
        audit_buffer_full="continue",
    )
    await store.upsert(_team("t1", fail_policy=overrides))
    got = await store.get("t1")
    assert got.fail_policy == overrides
