"""In-memory TTL cache around an inner ProfileLoader (mirror of rules/cached.py).

Concurrent loads for the same profile_id serialize on a per-key lock, so only
the first caller hits the inner loader.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from corp_llm_gateway.profiles.base import ProfileBundle, ProfileLoader


@dataclass
class _Entry:
    bundle: ProfileBundle
    expires_at: float


class CachedProfileLoader(ProfileLoader):
    def __init__(self, inner: ProfileLoader, ttl_seconds: int = 300) -> None:
        self._inner = inner
        self._ttl = float(ttl_seconds)
        self._cache: dict[str, _Entry] = {}
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    async def load(self, profile_id: str) -> ProfileBundle:
        entry = self._cache.get(profile_id)
        if entry is not None and entry.expires_at > time.monotonic():
            return entry.bundle

        key_lock = await self._get_key_lock(profile_id)
        async with key_lock:
            entry = self._cache.get(profile_id)
            if entry is not None and entry.expires_at > time.monotonic():
                return entry.bundle
            bundle = await self._inner.load(profile_id)
            self._cache[profile_id] = _Entry(bundle=bundle, expires_at=time.monotonic() + self._ttl)
            return bundle

    async def _get_key_lock(self, profile_id: str) -> asyncio.Lock:
        async with self._meta_lock:
            lock = self._key_locks.get(profile_id)
            if lock is None:
                lock = asyncio.Lock()
                self._key_locks[profile_id] = lock
            return lock
