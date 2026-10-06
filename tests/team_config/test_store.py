import pytest

from corp_llm_gateway.team_config import (
    DEFAULT_RETENTION_COLD_YEARS,
    DEFAULT_RETENTION_HOT_DAYS,
    FailPolicyOverrides,
    InMemoryTeamConfigStore,
    TeamConfig,
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
async def test_upsert_and_get() -> None:
    store = InMemoryTeamConfigStore()
    cfg = _team("t1")
    await store.upsert(cfg)
    assert await store.get("t1") == cfg


# PostgresTeamConfigStore is contract-tested against the in-memory store in
# tests/team_config/test_postgres_store.py (Postgres cases skip without asyncpg).
