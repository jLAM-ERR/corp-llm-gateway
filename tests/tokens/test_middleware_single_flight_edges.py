"""AuthMiddleware's per-token single-flight at scale and at the cache TTL edge."""

from __future__ import annotations

import asyncio
import types

import pytest

from corp_llm_gateway.tokens import AuthMiddleware, InvalidTokenError
from corp_llm_gateway.tokens import middleware as middleware_module
from tests.tokens.test_middleware import _GatedStore, _info


@pytest.mark.asyncio
async def test_a_hundred_waiters_on_a_failing_lookup_share_one_call_and_leave_no_noise() -> None:
    from tests.loop_errors import describe, loop_errors

    store = _GatedStore("tok-SECRET")
    store.fail_with = ConnectionResetError("tok-SECRET")
    mw = AuthMiddleware(store)

    async with loop_errors(settle_s=0.05) as seen:
        waiters = [asyncio.create_task(mw.authenticate("tok-SECRET")) for _ in range(100)]
        await asyncio.sleep(0)
        store.release.set()
        results = await asyncio.gather(*waiters, return_exceptions=True)

    assert all(isinstance(r, ConnectionResetError) for r in results)
    assert store.calls == ["tok-SECRET"]
    assert mw._inflight == {}
    assert seen == [], describe(seen)


@pytest.mark.asyncio
async def test_a_lookup_every_waiter_gave_up_on_still_fills_the_cache() -> None:
    store = _GatedStore("tok-1")
    store.upsert(_info("tok-1"))
    mw = AuthMiddleware(store)

    waiters = [asyncio.create_task(mw.authenticate("tok-1")) for _ in range(3)]
    await asyncio.sleep(0)
    for waiter in waiters:
        waiter.cancel()
    await asyncio.gather(*waiters, return_exceptions=True)
    store.release.set()
    await asyncio.sleep(0.01)

    assert (await mw.authenticate("tok-1")).user_id == "alice"
    assert store.calls == ["tok-1"]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.mark.asyncio
@pytest.mark.parametrize(("elapsed", "lookups"), [(9.999, 1), (10.0, 2), (10.001, 2)])
async def test_the_cache_serves_strictly_inside_its_ttl(
    monkeypatch: pytest.MonkeyPatch, elapsed: float, lookups: int
) -> None:
    clock = _Clock()
    monkeypatch.setattr(middleware_module, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    store = _GatedStore()
    store.upsert(_info("tok-1"))
    mw = AuthMiddleware(store, revocation_cache_seconds=10.0)

    await mw.authenticate("tok-1")
    clock.now += elapsed
    await mw.authenticate("tok-1")

    assert len(store.calls) == lookups


@pytest.mark.asyncio
async def test_a_revocation_is_seen_once_the_ttl_has_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from corp_llm_gateway.tokens import RevokedTokenError

    clock = _Clock()
    monkeypatch.setattr(middleware_module, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    store = _GatedStore()
    store.upsert(_info("tok-1"))
    mw = AuthMiddleware(store, revocation_cache_seconds=10.0)
    await mw.authenticate("tok-1")

    store.upsert(_info("tok-1", revoked_at=datetime.now(UTC)))
    clock.now += 5
    assert (await mw.authenticate("tok-1")).user_id == "alice"
    clock.now += 5
    with pytest.raises(RevokedTokenError):
        await mw.authenticate("tok-1")


@pytest.mark.asyncio
async def test_distinct_unknown_tokens_leave_no_bookkeeping() -> None:
    store = _GatedStore()
    mw = AuthMiddleware(store)

    for index in range(200):
        with pytest.raises(InvalidTokenError):
            await mw.authenticate(f"tok-{index}")

    assert mw._inflight == {}
    assert mw._cache == {}
