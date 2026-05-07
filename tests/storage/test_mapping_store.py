import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio
from fakeredis import aioredis as fakeredis_aio

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
