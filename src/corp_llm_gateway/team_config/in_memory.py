from corp_llm_gateway.team_config.models import TeamConfig
from corp_llm_gateway.team_config.store import TeamConfigStore, TeamNotFoundError


class InMemoryTeamConfigStore(TeamConfigStore):
    def __init__(self) -> None:
        self._configs: dict[str, TeamConfig] = {}

    async def get(self, team_id: str) -> TeamConfig:
        config = self._configs.get(team_id)
        if config is None:
            raise TeamNotFoundError(team_id)
        return config

    async def upsert(self, config: TeamConfig) -> None:
        self._configs[config.team_id] = config

    async def list_all(self) -> tuple[TeamConfig, ...]:
        return tuple(self._configs.values())
