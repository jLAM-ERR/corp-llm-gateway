import time
from dataclasses import dataclass
from typing import Generic, TypeVar

from corp_llm_gateway.storage.mapping import MappingStore, PlaceholderMapping

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    value: T
    expires_at: float
    sliding_ttl_seconds: float | None = None


def _now() -> float:
    return time.monotonic()


class InMemoryMappingStore(MappingStore):
    def __init__(self) -> None:
        self._dedup: dict[str, _Entry[PlaceholderMapping]] = {}
        self._o2p: dict[tuple[str, str], _Entry[str]] = {}
        self._p2o: dict[tuple[str, str], _Entry[str]] = {}

    async def get_dedup(self, content_hash: str) -> PlaceholderMapping | None:
        entry = self._dedup.get(content_hash)
        if entry is None:
            return None
        if entry.expires_at <= _now():
            del self._dedup[content_hash]
            return None
        return entry.value

    async def set_dedup(
        self,
        content_hash: str,
        mapping: PlaceholderMapping,
        *,
        ttl_seconds: int,
    ) -> None:
        self._dedup[content_hash] = _Entry(mapping, _now() + ttl_seconds)

    async def get_placeholder(self, conversation_id: str, original: str) -> str | None:
        return self._get_sliding(self._o2p, (conversation_id, original))

    async def get_original(self, conversation_id: str, placeholder: str) -> str | None:
        return self._get_sliding(self._p2o, (conversation_id, placeholder))

    async def put(
        self,
        conversation_id: str,
        original: str,
        placeholder: str,
        *,
        sliding_ttl_seconds: int,
    ) -> None:
        ttl = float(sliding_ttl_seconds)
        expires = _now() + ttl
        self._o2p[(conversation_id, original)] = _Entry(placeholder, expires, ttl)
        self._p2o[(conversation_id, placeholder)] = _Entry(original, expires, ttl)

    def _get_sliding(
        self,
        store: dict[tuple[str, str], _Entry[str]],
        key: tuple[str, str],
    ) -> str | None:
        entry = store.get(key)
        if entry is None:
            return None
        now = _now()
        if entry.expires_at <= now:
            del store[key]
            return None
        if entry.sliding_ttl_seconds is not None:
            entry.expires_at = now + entry.sliding_ttl_seconds
        return entry.value
