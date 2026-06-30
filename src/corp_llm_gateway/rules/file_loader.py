import asyncio
from pathlib import Path

from corp_llm_gateway.rules.loader import RulesLoader, RulesNotFoundError
from corp_llm_gateway.rules.models import Rules
from corp_llm_gateway.rules.parser import parse

# Path to the bundled gateway-default term files (products / regulated / markings)
DEFAULTS_DIR = Path(__file__).parent / "defaults"


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


def load_defaults_dir() -> Path:
    """Return the path to the bundled gateway-default term directory.

    Callers can pass this to ``Gazetteer.from_dir()`` or read individual
    category files (products.txt / regulated.txt / markings.txt) directly.
    The defaults ship with the package; ops teams extend them via the
    per-team replace.md mechanism or by overriding this path.
    """
    return DEFAULTS_DIR
