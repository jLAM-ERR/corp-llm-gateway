import json
from typing import TYPE_CHECKING

from corp_llm_gateway.storage.mapping import MappingStore, PlaceholderMapping

if TYPE_CHECKING:
    from redis.asyncio import Redis


_DEDUP_PREFIX = "dedup:"
_O2P_PREFIX = "conv:o2p:"
_P2O_PREFIX = "conv:p2o:"
_TTL_SUFFIX = ":ttl"


class RedisMappingStore(MappingStore):
    def __init__(self, redis: "Redis") -> None:
        self._r = redis

    async def get_dedup(self, content_hash: str) -> PlaceholderMapping | None:
        raw = await self._r.get(_DEDUP_PREFIX + content_hash)
        if raw is None:
            return None
        return _decode(raw)

    async def set_dedup(
        self,
        content_hash: str,
        mapping: PlaceholderMapping,
        *,
        ttl_seconds: int,
    ) -> None:
        await self._r.set(
            _DEDUP_PREFIX + content_hash,
            _encode(mapping),
            ex=ttl_seconds,
        )

    async def get_placeholder(self, conversation_id: str, original: str) -> str | None:
        return await self._sliding_get(_o2p_key(conversation_id, original))

    async def get_original(self, conversation_id: str, placeholder: str) -> str | None:
        return await self._sliding_get(_p2o_key(conversation_id, placeholder))

    async def put(
        self,
        conversation_id: str,
        original: str,
        placeholder: str,
        *,
        sliding_ttl_seconds: int,
    ) -> None:
        o2p = _o2p_key(conversation_id, original)
        p2o = _p2o_key(conversation_id, placeholder)
        ttl_str = str(sliding_ttl_seconds)
        async with self._r.pipeline(transaction=False) as pipe:
            pipe.set(o2p, placeholder, ex=sliding_ttl_seconds)
            pipe.set(p2o, original, ex=sliding_ttl_seconds)
            pipe.set(o2p + _TTL_SUFFIX, ttl_str, ex=sliding_ttl_seconds)
            pipe.set(p2o + _TTL_SUFFIX, ttl_str, ex=sliding_ttl_seconds)
            await pipe.execute()

    async def _sliding_get(self, key: str) -> str | None:
        raw = await self._r.get(key)
        if raw is None:
            return None
        ttl_key = key + _TTL_SUFFIX
        ttl_raw = await self._r.get(ttl_key)
        if ttl_raw is not None:
            ttl = int(_to_str(ttl_raw))
            await self._r.expire(key, ttl)
            await self._r.expire(ttl_key, ttl)
        return _to_str(raw)


def _encode(mapping: PlaceholderMapping) -> str:
    return json.dumps([list(p) for p in mapping.pairs])


def _decode(raw: object) -> PlaceholderMapping:
    text = _to_str(raw)
    pairs = json.loads(text)
    return PlaceholderMapping(pairs=tuple((a, b) for a, b in pairs))


def _to_str(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bytes):
        return raw.decode()
    raise TypeError(f"unexpected redis value type: {type(raw).__name__}")


def _o2p_key(conversation_id: str, original: str) -> str:
    return f"{_O2P_PREFIX}{conversation_id}:{original}"


def _p2o_key(conversation_id: str, placeholder: str) -> str:
    return f"{_P2O_PREFIX}{conversation_id}:{placeholder}"
