import asyncio
import time
from dataclasses import dataclass

from corp_llm_gateway.rules.loader import RulesLoader
from corp_llm_gateway.rules.models import Rules


@dataclass
class _Entry:
    rules: Rules
    expires_at: float


class CachedRulesLoader(RulesLoader):
    """In-memory TTL cache around an inner RulesLoader.

    Concurrent loads for the same team_id serialize on a per-team lock,
    so only the first caller hits the inner loader. Once the cache is
    populated, subsequent callers within the lock find it on the
    second-check and return without doing inner work.

    Per M1-15: rule updates take effect on cache eviction. Live conversations
    holding pre-update mappings continue to use them until the conversation
    expires; new occurrences in the same conversation pick up new rules.
    """

    def __init__(self, inner: RulesLoader, ttl_seconds: int = 300) -> None:
        self._inner = inner
        self._ttl = float(ttl_seconds)
        self._cache: dict[str, _Entry] = {}
        self._team_locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    async def load(self, team_id: str) -> Rules:
        entry = self._cache.get(team_id)
        if entry is not None and entry.expires_at > time.monotonic():
            return entry.rules

        team_lock = await self._get_team_lock(team_id)
        async with team_lock:
            entry = self._cache.get(team_id)
            if entry is not None and entry.expires_at > time.monotonic():
                return entry.rules
            rules = await self._inner.load(team_id)
            self._cache[team_id] = _Entry(rules=rules, expires_at=time.monotonic() + self._ttl)
            return rules

    async def _get_team_lock(self, team_id: str) -> asyncio.Lock:
        async with self._meta_lock:
            lock = self._team_locks.get(team_id)
            if lock is None:
                lock = asyncio.Lock()
                self._team_locks[team_id] = lock
            return lock
