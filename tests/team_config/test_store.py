import pytest

from corp_llm_gateway.team_config import (
    DEFAULT_RETENTION_COLD_YEARS,
    DEFAULT_RETENTION_HOT_DAYS,
    FailPolicyOverrides,
    InMemoryTeamConfigStore,
    TeamConfig,
    TeamNotFoundError,
)


def _team(team_id: str = "t1", **overrides: object) -> TeamConfig:
    base: dict[str, object] = {"team_id": team_id, "name": f"Team {team_id}"}
    base.update(overrides)
    return TeamConfig(**base)  # type: ignore[arg-type]


# Defaults --------------------------------------------------------------


def test_team_config_defaults() -> None:
    cfg = _team()
    assert cfg.retention_hot_days == DEFAULT_RETENTION_HOT_DAYS
    assert cfg.retention_cold_years == DEFAULT_RETENTION_COLD_YEARS
    assert cfg.replace_md_path is None
    assert cfg.profile_ids == ()
    assert cfg.fail_policy == FailPolicyOverrides()


def test_default_fail_policy_matches_matrix() -> None:
    fp = FailPolicyOverrides()
    assert fp.pre_pass_down == "continue"
    assert fp.audit_sink_down == "continue"
    assert fp.audit_buffer_full == "fail-closed"


# In-memory CRUD ------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_unknown_team_raises() -> None:
    store = InMemoryTeamConfigStore()
    with pytest.raises(TeamNotFoundError):
        await store.get("missing")


@pytest.mark.asyncio
async def test_upsert_and_get() -> None:
    store = InMemoryTeamConfigStore()
    cfg = _team("t1")
    await store.upsert(cfg)
    assert await store.get("t1") == cfg


@pytest.mark.asyncio
async def test_upsert_overwrites() -> None:
    store = InMemoryTeamConfigStore()
    await store.upsert(_team("t1", name="Original"))
    await store.upsert(_team("t1", name="Updated"))
    cfg = await store.get("t1")
    assert cfg.name == "Updated"


@pytest.mark.asyncio
async def test_list_all_empty() -> None:
    store = InMemoryTeamConfigStore()
    assert await store.list_all() == ()


@pytest.mark.asyncio
async def test_list_all_returns_all() -> None:
    store = InMemoryTeamConfigStore()
    await store.upsert(_team("t1"))
    await store.upsert(_team("t2"))
    teams = await store.list_all()
    assert {t.team_id for t in teams} == {"t1", "t2"}


@pytest.mark.asyncio
async def test_per_team_retention_overrides_persist() -> None:
    store = InMemoryTeamConfigStore()
    await store.upsert(_team("t1", retention_hot_days=30, retention_cold_years=1))
    cfg = await store.get("t1")
    assert cfg.retention_hot_days == 30
    assert cfg.retention_cold_years == 1


@pytest.mark.asyncio
async def test_per_team_fail_policy_overrides_persist() -> None:
    store = InMemoryTeamConfigStore()
    overrides = FailPolicyOverrides(audit_buffer_full="continue")
    await store.upsert(_team("t1", fail_policy=overrides))
    cfg = await store.get("t1")
    assert cfg.fail_policy.audit_buffer_full == "continue"


# PostgresTeamConfigStore is contract-tested against the in-memory store in
# tests/team_config/test_postgres_store.py (Postgres cases skip without asyncpg).
