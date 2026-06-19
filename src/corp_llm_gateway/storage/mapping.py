from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class PlaceholderMapping:
    pairs: tuple[tuple[str, str], ...]


class MappingStore(ABC):
    """Two-level cache for sanitization mappings.

    Cache A (dedup):     content_hash → full PlaceholderMapping. Fixed TTL.
    Cache B (per-conv):  conversation_id x original ↔ placeholder. Sliding TTL —
                         each get refreshes expiry.
    """

    @abstractmethod
    async def get_dedup(self, content_hash: str) -> PlaceholderMapping | None: ...

    @abstractmethod
    async def set_dedup(
        self,
        content_hash: str,
        mapping: PlaceholderMapping,
        *,
        ttl_seconds: int,
    ) -> None: ...

    @abstractmethod
    async def get_placeholder(self, conversation_id: str, original: str) -> str | None: ...

    @abstractmethod
    async def get_original(self, conversation_id: str, placeholder: str) -> str | None: ...

    @abstractmethod
    async def put(
        self,
        conversation_id: str,
        original: str,
        placeholder: str,
        *,
        sliding_ttl_seconds: int,
    ) -> None: ...
