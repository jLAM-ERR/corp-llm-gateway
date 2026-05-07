import asyncio

import pytest

from corp_llm_gateway.rules import (
    CachedRulesLoader,
    Rule,
    Rules,
    RulesLoader,
    RulesNotFoundError,
)


class _CountingLoader(RulesLoader):
    def __init__(self, rules_by_team: dict[str, Rules]) -> None:
        self._rules = rules_by_team
        self.call_count: dict[str, int] = {}

    async def load(self, team_id: str) -> Rules:
        self.call_count[team_id] = self.call_count.get(team_id, 0) + 1
        if team_id not in self._rules:
            raise RulesNotFoundError(team_id)
        return self._rules[team_id]


class _SlowLoader(RulesLoader):
    def __init__(self, rules: Rules, delay: float) -> None:
        self._rules = rules
        self._delay = delay
        self.call_count = 0

    async def load(self, team_id: str) -> Rules:
        self.call_count += 1
        await asyncio.sleep(self._delay)
        return self._rules


async def test_cache_hit_within_ttl() -> None:
    inner = _CountingLoader({"a": Rules(rules=(Rule("alice", "[N1]"),))})
    cached = CachedRulesLoader(inner, ttl_seconds=60)
    await cached.load("a")
    await cached.load("a")
    await cached.load("a")
    assert inner.call_count["a"] == 1


async def test_cache_isolated_per_team() -> None:
    inner = _CountingLoader(
        {
            "a": Rules(rules=(Rule("alice", "[N1]"),)),
            "b": Rules(rules=(Rule("bob", "[N2]"),)),
        }
    )
    cached = CachedRulesLoader(inner, ttl_seconds=60)
    a = await cached.load("a")
    b = await cached.load("b")
    assert a.rules[0].pattern == "alice"
    assert b.rules[0].pattern == "bob"
    assert inner.call_count == {"a": 1, "b": 1}


async def test_cache_refresh_after_ttl() -> None:
    inner = _CountingLoader({"a": Rules(rules=(Rule("alice", "[N1]"),))})
    cached = CachedRulesLoader(inner, ttl_seconds=1)
    await cached.load("a")
    await asyncio.sleep(1.2)
    await cached.load("a")
    assert inner.call_count["a"] == 2


async def test_concurrent_loads_dedupe() -> None:
    inner = _SlowLoader(Rules(rules=(Rule("alice", "[N1]"),)), delay=0.2)
    cached = CachedRulesLoader(inner, ttl_seconds=60)
    results = await asyncio.gather(*(cached.load("a") for _ in range(10)))
    assert all(r == results[0] for r in results)
    assert inner.call_count == 1


async def test_inner_error_propagates_and_does_not_cache() -> None:
    inner = _CountingLoader({})
    cached = CachedRulesLoader(inner, ttl_seconds=60)
    with pytest.raises(RulesNotFoundError):
        await cached.load("missing")
    with pytest.raises(RulesNotFoundError):
        await cached.load("missing")
    assert inner.call_count["missing"] == 2
