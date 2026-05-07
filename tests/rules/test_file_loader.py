from pathlib import Path

import pytest

from corp_llm_gateway.rules import (
    FileRulesLoader,
    Rule,
    Rules,
    RulesNotFoundError,
)


async def test_loads_team_file(tmp_path: Path) -> None:
    (tmp_path / "team-a.md").write_text("- alice → [N1]\n", encoding="utf-8")
    rules = await FileRulesLoader(tmp_path).load("team-a")
    assert rules == Rules(rules=(Rule("alice", "[N1]"),))


async def test_unknown_team_raises(tmp_path: Path) -> None:
    with pytest.raises(RulesNotFoundError, match="team-zz"):
        await FileRulesLoader(tmp_path).load("team-zz")


async def test_team_file_isolation(tmp_path: Path) -> None:
    (tmp_path / "team-a.md").write_text("- alice → [N1]\n", encoding="utf-8")
    (tmp_path / "team-b.md").write_text("- bob → [N2]\n", encoding="utf-8")
    loader = FileRulesLoader(tmp_path)
    a = await loader.load("team-a")
    b = await loader.load("team-b")
    assert a.rules[0].pattern == "alice"
    assert b.rules[0].pattern == "bob"
