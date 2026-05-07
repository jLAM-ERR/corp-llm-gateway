import asyncio
from pathlib import Path

from corp_llm_gateway.rules.loader import RulesLoader, RulesNotFoundError
from corp_llm_gateway.rules.models import Rules
from corp_llm_gateway.rules.parser import parse


class FileRulesLoader(RulesLoader):
    def __init__(self, directory: Path) -> None:
        self._dir = directory

    async def load(self, team_id: str) -> Rules:
        path = self._dir / f"{team_id}.md"
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except FileNotFoundError as exc:
            raise RulesNotFoundError(f"no rules file for team {team_id!r} at {path}") from exc
        return parse(text)
