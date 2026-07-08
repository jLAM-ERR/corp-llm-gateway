import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeredis_aio

from corp_llm_gateway.rules.models import Rules
from corp_llm_gateway.sanitizer.orchestrator import _content_hash
from corp_llm_gateway.storage import (
    InMemoryMappingStore,
    MappingStore,
    PlaceholderMapping,
    RedisMappingStore,
)

StoreFactory = Callable[[], Awaitable[MappingStore]]


async def _make_in_memory() -> MappingStore:
    return InMemoryMappingStore()


async def _make_redis() -> MappingStore:
    return RedisMappingStore(fakeredis_aio.FakeRedis(decode_responses=True))


@pytest_asyncio.fixture(params=[_make_in_memory, _make_redis], ids=["in_memory", "redis"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[MappingStore]:
    factory: StoreFactory = request.param
    instance = await factory()
    yield instance


# Cache A — content-hash dedup ----------------------------------------------


@pytest.mark.asyncio
async def test_dedup_miss_returns_none(store: MappingStore) -> None:
    assert await store.get_dedup("nonexistent") is None


@pytest.mark.asyncio
async def test_dedup_round_trip(store: MappingStore) -> None:
    mapping = PlaceholderMapping(pairs=(("alice", "[NAME_001]"), ("bob", "[NAME_002]")))
    await store.set_dedup("hash-1", mapping, ttl_seconds=10)
    assert await store.get_dedup("hash-1") == mapping


@pytest.mark.asyncio
async def test_dedup_overwrite(store: MappingStore) -> None:
    a = PlaceholderMapping(pairs=(("alice", "[NAME_001]"),))
    b = PlaceholderMapping(pairs=(("bob", "[NAME_002]"),))
    await store.set_dedup("hash-1", a, ttl_seconds=10)
    await store.set_dedup("hash-1", b, ttl_seconds=10)
    assert await store.get_dedup("hash-1") == b


@pytest.mark.asyncio
async def test_dedup_isolation_across_hashes(store: MappingStore) -> None:
    a = PlaceholderMapping(pairs=(("alice", "[NAME_001]"),))
    b = PlaceholderMapping(pairs=(("bob", "[NAME_002]"),))
    await store.set_dedup("h-a", a, ttl_seconds=10)
    await store.set_dedup("h-b", b, ttl_seconds=10)
    assert await store.get_dedup("h-a") == a
    assert await store.get_dedup("h-b") == b


# Cache A — D3 profile-fingerprint isolation (both backends) -----------------


@pytest.mark.asyncio
async def test_dedup_isolated_across_profile_fingerprints(store: MappingStore) -> None:
    """A profile fingerprint folded into the key isolates cross-profile results.

    Same team/rules/text, two profiles: the permissive (empty) mapping and the
    strict (redacting) mapping never collide, so a strict request cannot be
    served the permissive result (the D3 cross-jurisdiction bleed).
    """
    rules = Rules(rules=())
    key_us = _content_hash("t1", rules, "Deploy Sistema", "fp-us-base")
    key_ru = _content_hash("t1", rules, "Deploy Sistema", "fp-ru-152fz")
    assert key_us != key_ru

    us_map = PlaceholderMapping(pairs=())
    ru_map = PlaceholderMapping(pairs=(("Sistema", "[PRODUCT_001]"),))
    await store.set_dedup(key_us, us_map, ttl_seconds=10)
    await store.set_dedup(key_ru, ru_map, ttl_seconds=10)

    assert await store.get_dedup(key_ru) == ru_map
    assert await store.get_dedup(key_us) == us_map


@pytest.mark.asyncio
async def test_dedup_shared_within_same_profile_fingerprint(store: MappingStore) -> None:
    """Same profile fingerprint re-derives the identical key → dedup still hits."""
    rules = Rules(rules=())
    key = _content_hash("t1", rules, "Deploy Sistema", "fp-ru-152fz")
    mapping = PlaceholderMapping(pairs=(("Sistema", "[PRODUCT_001]"),))
    await store.set_dedup(key, mapping, ttl_seconds=10)
    assert _content_hash("t1", rules, "Deploy Sistema", "fp-ru-152fz") == key
    assert await store.get_dedup(key) == mapping


@pytest.mark.asyncio
async def test_dedup_none_fingerprint_matches_legacy_key(store: MappingStore) -> None:
    """A None fingerprint keys the SAME entry as the pre-D3 (no-arg) call."""
    rules = Rules(rules=())
    legacy = _content_hash("t1", rules, "Deploy Sistema")
    assert _content_hash("t1", rules, "Deploy Sistema", None) == legacy
    mapping = PlaceholderMapping(pairs=())
    await store.set_dedup(legacy, mapping, ttl_seconds=10)
    assert await store.get_dedup(_content_hash("t1", rules, "Deploy Sistema", None)) == mapping


# Cache B — per-conversation mapping ----------------------------------------


@pytest.mark.asyncio
async def test_per_conv_miss_returns_none(store: MappingStore) -> None:
    assert await store.get_placeholder("conv-1", "alice") is None
    assert await store.get_original("conv-1", "[NAME_001]") is None


@pytest.mark.asyncio
async def test_per_conv_round_trip(store: MappingStore) -> None:
    await store.put("conv-1", "alice", "[NAME_001]", sliding_ttl_seconds=60)
    assert await store.get_placeholder("conv-1", "alice") == "[NAME_001]"
    assert await store.get_original("conv-1", "[NAME_001]") == "alice"


@pytest.mark.asyncio
async def test_per_conv_isolation(store: MappingStore) -> None:
    await store.put("conv-1", "alice", "[NAME_001]", sliding_ttl_seconds=60)
    await store.put("conv-2", "alice", "[NAME_999]", sliding_ttl_seconds=60)
    assert await store.get_placeholder("conv-1", "alice") == "[NAME_001]"
    assert await store.get_placeholder("conv-2", "alice") == "[NAME_999]"


@pytest.mark.asyncio
async def test_per_conv_unknown_in_one_does_not_leak_to_another(store: MappingStore) -> None:
    await store.put("conv-1", "alice", "[NAME_001]", sliding_ttl_seconds=60)
    assert await store.get_placeholder("conv-2", "alice") is None


# TTL behavior --------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_expires(store: MappingStore) -> None:
    mapping = PlaceholderMapping(pairs=(("alice", "[NAME_001]"),))
    await store.set_dedup("h", mapping, ttl_seconds=1)
    await asyncio.sleep(1.2)
    assert await store.get_dedup("h") is None


@pytest.mark.asyncio
async def test_per_conv_expires(store: MappingStore) -> None:
    await store.put("conv-1", "alice", "[NAME_001]", sliding_ttl_seconds=1)
    await asyncio.sleep(1.2)
    assert await store.get_placeholder("conv-1", "alice") is None
    assert await store.get_original("conv-1", "[NAME_001]") is None


@pytest.mark.asyncio
async def test_per_conv_sliding_ttl_extends_on_access(store: MappingStore) -> None:
    await store.put("conv-1", "alice", "[NAME_001]", sliding_ttl_seconds=2)
    await asyncio.sleep(1.0)
    assert await store.get_placeholder("conv-1", "alice") == "[NAME_001]"
    await asyncio.sleep(1.5)
    assert await store.get_placeholder("conv-1", "alice") == "[NAME_001]"
