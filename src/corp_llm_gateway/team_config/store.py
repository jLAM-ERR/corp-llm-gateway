from abc import ABC, abstractmethod

from corp_llm_gateway.team_config.models import TeamConfig


class TeamNotFoundError(Exception):
    pass


class TeamConfigStore(ABC):
    @abstractmethod
    async def get(self, team_id: str) -> TeamConfig: ...

    @abstractmethod
    async def upsert(self, config: TeamConfig) -> None: ...

    @abstractmethod
    async def list_all(self) -> tuple[TeamConfig, ...]: ...
