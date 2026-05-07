from abc import ABC, abstractmethod

from corp_llm_gateway.rules.models import Rules


class RulesNotFoundError(Exception):
    pass


class RulesParseError(Exception):
    pass


class RulesLoader(ABC):
    @abstractmethod
    async def load(self, team_id: str) -> Rules: ...
