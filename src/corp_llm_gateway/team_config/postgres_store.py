from corp_llm_gateway.team_config.models import TeamConfig
from corp_llm_gateway.team_config.store import TeamConfigStore


class PostgresTeamConfigStore(TeamConfigStore):
    """Stub. Implement against the team_config table from tokens/schema.sql
    once M0-5 Postgres is provisioned."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    async def get(self, team_id: str) -> TeamConfig:
        raise NotImplementedError(
            "PostgresTeamConfigStore stub — implement after M0-5 Postgres is provisioned"
        )

    async def upsert(self, config: TeamConfig) -> None:
        raise NotImplementedError(
            "PostgresTeamConfigStore stub — implement after M0-5 Postgres is provisioned"
        )

    async def list_all(self) -> tuple[TeamConfig, ...]:
        raise NotImplementedError(
            "PostgresTeamConfigStore stub — implement after M0-5 Postgres is provisioned"
        )
