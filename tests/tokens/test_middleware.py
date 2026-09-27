import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from corp_llm_gateway.tokens import (
    AuthMiddleware,
    ExpiredTokenError,
    InMemoryTokenStore,
    InvalidTokenError,
    MissingTokenError,
    PostgresTokenStore,
    RevokedTokenError,
    TokenInfo,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _info(
    corp_token: str = "tok-1",
    *,
    revoked_at: datetime | None = None,
    expires_in: timedelta = timedelta(days=30),
    user_id: str = "alice",
    team_id: str = "t1",
) -> TokenInfo:
    now = _now()
    return TokenInfo(
        corp_token=corp_token,
        user_id=user_id,
        team_id=team_id,
        scopes=("read",),
        issued_at=now,
        expires_at=now + expires_in,
        revoked_at=revoked_at,
    )


# Header extraction & stripping ---------------------------------------------


def test_strip_corp_token_removes_header_case_insensitively() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    headers = {"X-Corp-Auth": "tok", "Authorization": "Bearer x", "Accept": "*/*"}
    out = mw.strip_corp_token(headers)
    assert "X-Corp-Auth" not in out
    assert "Authorization" in out
    assert "Accept" in out


def test_strip_corp_token_lower_cased_header() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    headers = {"x-corp-auth": "tok", "Authorization": "Bearer x"}
    out = mw.strip_corp_token(headers)
    assert "x-corp-auth" not in out
    assert "Authorization" in out


def test_strip_corp_token_does_not_mutate_input() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    headers = {"X-Corp-Auth": "tok"}
    mw.strip_corp_token(headers)
    assert "X-Corp-Auth" in headers


# authenticate() — happy & error paths --------------------------------------


@pytest.mark.asyncio
async def test_missing_token_raises() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    with pytest.raises(MissingTokenError):
        await mw.authenticate(None)


@pytest.mark.asyncio
async def test_empty_token_raises() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    with pytest.raises(MissingTokenError):
        await mw.authenticate("")


@pytest.mark.asyncio
async def test_unknown_token_raises_invalid() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    with pytest.raises(InvalidTokenError):
        await mw.authenticate("nonexistent")


@pytest.mark.asyncio
async def test_valid_token_returns_context() -> None:
    store = InMemoryTokenStore()
    store.upsert(_info(corp_token="tok-1"))
    mw = AuthMiddleware(store)
    ctx = await mw.authenticate("tok-1")
    assert ctx.user_id == "alice"
    assert ctx.team_id == "t1"
    assert ctx.scopes == ("read",)


@pytest.mark.asyncio
async def test_expired_token_raises() -> None:
    store = InMemoryTokenStore()
    store.upsert(_info(corp_token="tok-1", expires_in=timedelta(seconds=-1)))
    mw = AuthMiddleware(store)
    with pytest.raises(ExpiredTokenError):
        await mw.authenticate("tok-1")


@pytest.mark.asyncio
async def test_revoked_token_raises() -> None:
    store = InMemoryTokenStore()
    info = _info(corp_token="tok-1")
    store.upsert(info)
    await store.revoke_user("alice")
    mw = AuthMiddleware(store)
    with pytest.raises(RevokedTokenError):
        await mw.authenticate("tok-1")


# Header-based auth ---------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_headers_picks_corp_auth() -> None:
    store = InMemoryTokenStore()
    store.upsert(_info(corp_token="tok-1"))
    mw = AuthMiddleware(store)
    ctx = await mw.authenticate_headers({"X-Corp-Auth": "tok-1"})
    assert ctx.user_id == "alice"


@pytest.mark.asyncio
async def test_authenticate_headers_case_insensitive() -> None:
    store = InMemoryTokenStore()
    store.upsert(_info(corp_token="tok-1"))
    mw = AuthMiddleware(store)
    ctx = await mw.authenticate_headers({"x-corp-auth": "tok-1"})
    assert ctx.user_id == "alice"


@pytest.mark.asyncio
async def test_authenticate_headers_missing_corp_auth_raises() -> None:
    mw = AuthMiddleware(InMemoryTokenStore())
    with pytest.raises(MissingTokenError):
        await mw.authenticate_headers({"Authorization": "Bearer x"})


# Revocation cache lag ------------------------------------------------------


@pytest.mark.asyncio
async def test_revocation_cache_serves_stale_within_window() -> None:
    """Revoking a cached token still validates as OK until cache TTL expires."""
    store = InMemoryTokenStore()
    store.upsert(_info(corp_token="tok-1"))
    mw = AuthMiddleware(store, revocation_cache_seconds=1.0)

    ctx_before = await mw.authenticate("tok-1")
    assert ctx_before.user_id == "alice"

    await store.revoke_user("alice")

    ctx_after = await mw.authenticate("tok-1")
    assert ctx_after.user_id == "alice"


@pytest.mark.asyncio
async def test_revocation_cache_picks_up_revoke_after_ttl() -> None:
    store = InMemoryTokenStore()
    store.upsert(_info(corp_token="tok-1"))
    mw = AuthMiddleware(store, revocation_cache_seconds=0.5)

    await mw.authenticate("tok-1")
    await store.revoke_user("alice")
    await asyncio.sleep(0.7)

    with pytest.raises(RevokedTokenError):
        await mw.authenticate("tok-1")


# PostgresTokenStore import guard -------------------------------------------


def test_postgres_store_without_asyncpg_raises() -> None:
    """When asyncpg is absent, instantiating PostgresTokenStore raises RuntimeError."""
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        with pytest.raises(RuntimeError, match="asyncpg"):
            PostgresTokenStore("postgresql://x")
    else:
        pytest.skip("asyncpg is installed; cannot test the absent-asyncpg path")


# Per-token single-flight ---------------------------------------------------


class _GatedStore(InMemoryTokenStore):
    """Lookups of ``gated`` tokens wait on ``release``; every call is counted."""

    def __init__(self, *gated: str) -> None:
        super().__init__()
        self.gated = set(gated)
        self.release = asyncio.Event()
        self.calls: list[str] = []
        self.fail_with: BaseException | None = None

    async def lookup(self, corp_token: str) -> TokenInfo | None:
        self.calls.append(corp_token)
        if corp_token in self.gated:
            await self.release.wait()
            if self.fail_with is not None:
                raise self.fail_with
        return await super().lookup(corp_token)


@pytest.mark.asyncio
async def test_a_stalled_lookup_does_not_hold_up_a_different_token() -> None:
    store = _GatedStore("tok-stuck")
    store.upsert(_info("tok-stuck"))
    store.upsert(_info("tok-free", user_id="bob"))
    mw = AuthMiddleware(store)

    stuck = asyncio.create_task(mw.authenticate("tok-stuck"))
    await asyncio.sleep(0)
    free = await asyncio.wait_for(mw.authenticate("tok-free"), timeout=1)

    assert free.user_id == "bob"
    assert not stuck.done()
    store.release.set()
    assert (await stuck).user_id == "alice"


@pytest.mark.asyncio
async def test_concurrent_lookups_of_one_token_share_one_store_call() -> None:
    store = _GatedStore("tok-1")
    store.upsert(_info("tok-1"))
    mw = AuthMiddleware(store)

    waiters = [asyncio.create_task(mw.authenticate("tok-1")) for _ in range(5)]
    await asyncio.sleep(0)
    store.release.set()
    results = await asyncio.gather(*waiters)

    assert store.calls == ["tok-1"]
    assert {r.user_id for r in results} == {"alice"}
    # Then served from the cache.
    await mw.authenticate("tok-1")
    assert store.calls == ["tok-1"]


@pytest.mark.asyncio
async def test_a_failed_lookup_reaches_every_waiter_and_is_not_cached() -> None:
    store = _GatedStore("tok-1")
    store.upsert(_info("tok-1"))
    store.fail_with = TimeoutError()
    mw = AuthMiddleware(store)

    waiters = [asyncio.create_task(mw.authenticate("tok-1")) for _ in range(3)]
    await asyncio.sleep(0)
    store.release.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert all(isinstance(r, TimeoutError) for r in results)
    assert store.calls == ["tok-1"]
    store.fail_with = None
    assert (await mw.authenticate("tok-1")).user_id == "alice"
    assert store.calls == ["tok-1", "tok-1"]


@pytest.mark.asyncio
async def test_an_unknown_token_is_looked_up_again_next_time() -> None:
    store = _GatedStore()
    mw = AuthMiddleware(store)

    for _ in range(2):
        with pytest.raises(InvalidTokenError):
            await mw.authenticate("tok-unknown")

    assert store.calls == ["tok-unknown", "tok-unknown"]


@pytest.mark.asyncio
async def test_a_cancelled_waiter_does_not_fail_the_others_sharing_its_lookup() -> None:
    store = _GatedStore("tok-1")
    store.upsert(_info("tok-1"))
    mw = AuthMiddleware(store)

    first = asyncio.create_task(mw.authenticate("tok-1"))
    await asyncio.sleep(0)
    second = asyncio.create_task(mw.authenticate("tok-1"))
    await asyncio.sleep(0)
    first.cancel()
    await asyncio.sleep(0)
    store.release.set()

    assert (await asyncio.wait_for(second, timeout=1)).user_id == "alice"
    with pytest.raises(asyncio.CancelledError):
        await first
    assert store.calls == ["tok-1"]


@pytest.mark.asyncio
async def test_no_lookup_bookkeeping_outlives_its_lookup() -> None:
    store = _GatedStore("tok-1")
    store.upsert(_info("tok-1"))
    store.fail_with = OSError()
    mw = AuthMiddleware(store)
    store.release.set()

    with pytest.raises(OSError):
        await mw.authenticate("tok-1")
    with pytest.raises(InvalidTokenError):
        await mw.authenticate("tok-other")

    assert mw._inflight == {}


@pytest.mark.asyncio
async def test_a_failed_lookup_with_a_cancelled_waiter_never_reaches_the_loop_handler() -> None:
    from tests.loop_errors import describe, loop_errors

    store = _GatedStore("tok-SECRET")
    store.fail_with = ConnectionResetError("tok-SECRET")
    mw = AuthMiddleware(store)

    async with loop_errors(settle_s=0.05) as seen:
        gone = asyncio.create_task(mw.authenticate("tok-SECRET"))
        await asyncio.sleep(0)
        survivor = asyncio.create_task(mw.authenticate("tok-SECRET"))
        await asyncio.sleep(0)
        gone.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gone
        store.release.set()
        with pytest.raises(ConnectionResetError):
            await asyncio.wait_for(survivor, timeout=1)

    assert seen == [], describe(seen)
    assert store.calls == ["tok-SECRET"]


@pytest.mark.asyncio
async def test_a_failed_lookup_every_waiter_abandoned_is_still_retrieved() -> None:
    from tests.loop_errors import describe, loop_errors

    store = _GatedStore("tok-SECRET")
    store.fail_with = ConnectionResetError("tok-SECRET")
    mw = AuthMiddleware(store)

    async with loop_errors(settle_s=0.05) as seen:
        waiters = [asyncio.create_task(mw.authenticate("tok-SECRET")) for _ in range(2)]
        await asyncio.sleep(0)
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        store.release.set()
        await asyncio.sleep(0.01)

    assert seen == [], describe(seen)
    assert mw._inflight == {}


@pytest.mark.asyncio
async def test_the_shared_lookup_belongs_to_no_request() -> None:
    # Started inside one request, awaited by others: that request's disconnect
    # sweep must not be able to cancel it.
    from corp_llm_gateway.route_gate.inflight import (
        _TICKET,
        RequestTicket,
        _tag,
        install_task_factory,
        pending_request_tasks,
    )

    store = _GatedStore("tok-1")
    store.upsert(_info("tok-1"))
    mw = AuthMiddleware(store)
    restore = install_task_factory(asyncio.get_running_loop())
    try:
        ticket = RequestTicket("gateway-id")
        token = _TICKET.set(ticket)
        try:
            waiter = asyncio.create_task(mw.authenticate("tok-1"))
        finally:
            _TICKET.reset(token)
        _tag(waiter, ticket)
        await asyncio.sleep(0)
        (shared,) = mw._inflight.values()

        assert waiter in ticket.pending()
        assert shared not in ticket.pending()
        assert shared not in pending_request_tasks()
        store.release.set()
        assert (await waiter).user_id == "alice"
    finally:
        restore()
