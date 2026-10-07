"""Parametrised TokenStore contract tests.

Runs against InMemoryTokenStore (always) and PostgresTokenStore (skips when
asyncpg is absent or the demo Postgres is unreachable; fails instead on CI,
see tests/postgres_support.py).

Pattern mirrors tests/storage/test_mapping_store.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import yaml

from corp_llm_gateway.tokens import InMemoryTokenStore, TokenInfo
from corp_llm_gateway.tokens.errors import IssuancePolicyError
from corp_llm_gateway.tokens.store import TokenStore
from tests.postgres_support import (
    DEMO_PG_DSN,
    PG_DSN_ENV_VAR,
    pg_dsn,
    require_asyncpg,
    skip_or_fail,
)

StoreFactory = Callable[[], Awaitable[TokenStore]]

_RACE_WIDTH = 10
# Room for the race tests to hold _RACE_WIDTH connections at once.
_RACE_POOL_SIZE = 12


async def _make_in_memory() -> TokenStore:
    return InMemoryTokenStore()


async def _try_make_postgres() -> TokenStore:
    require_asyncpg()
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    store = PostgresTokenStore(pg_dsn(), pool_max_size=_RACE_POOL_SIZE)
    try:
        await store.init_schema()
        pool = await store._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("TRUNCATE corp_tokens")
        # Connect every race connection up front; lazy connects would stagger the
        # racers enough to serialise them and hide a missing lock.
        warm = [await pool.acquire() for _ in range(_RACE_WIDTH)]
        for conn in warm:
            await pool.release(conn)
    except Exception as exc:
        await store.close()
        skip_or_fail(f"Postgres unreachable: {exc}")
    return store


@pytest_asyncio.fixture(params=[_make_in_memory, _try_make_postgres], ids=["in_memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[TokenStore]:
    factory: StoreFactory = request.param
    instance = await factory()
    yield instance
    if hasattr(instance, "close"):
        await instance.close()  # type: ignore[union-attr]


def _info(corp_token: str, user_id: str = "alice", team_id: str = "t1") -> TokenInfo:
    now = datetime.now(UTC)
    return TokenInfo(
        corp_token=corp_token,
        user_id=user_id,
        team_id=team_id,
        scopes=("read",),
        issued_at=now,
        expires_at=now + timedelta(days=30),
    )


async def _upsert(store: TokenStore, info: TokenInfo) -> None:
    """Dispatch upsert for both sync (InMemory) and async (Postgres) impls."""
    result: Any = store.upsert(info)  # type: ignore[attr-defined]
    if asyncio.iscoroutine(result):
        await result


# Contract tests -------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_unknown_returns_none(store: TokenStore) -> None:
    assert await store.lookup("ct-contract-unknown") is None


@pytest.mark.asyncio
async def test_upsert_and_lookup(store: TokenStore) -> None:
    info = _info("ct-contract-1")
    await _upsert(store, info)
    got = await store.lookup("ct-contract-1")
    assert got is not None
    assert got.user_id == "alice"
    assert got.team_id == "t1"
    assert got.scopes == ("read",)
    assert got.revoked_at is None


@pytest.mark.asyncio
async def test_upsert_overwrite(store: TokenStore) -> None:
    info = _info("ct-contract-over")
    await _upsert(store, info)
    now = datetime.now(UTC)
    revoked = TokenInfo(
        corp_token="ct-contract-over",
        user_id="alice",
        team_id="t1",
        scopes=("read",),
        issued_at=info.issued_at,
        expires_at=info.expires_at,
        revoked_at=now,
    )
    await _upsert(store, revoked)
    got = await store.lookup("ct-contract-over")
    assert got is not None and got.revoked_at is not None


@pytest.mark.asyncio
async def test_revoke_user_marks_all_their_tokens(store: TokenStore) -> None:
    await _upsert(store, _info("ct-c-tok1", user_id="alice"))
    await _upsert(store, _info("ct-c-tok2", user_id="alice"))
    await _upsert(store, _info("ct-c-tok3", user_id="bob"))

    n = await store.revoke_user("alice")
    assert n == 2

    a1 = await store.lookup("ct-c-tok1")
    a2 = await store.lookup("ct-c-tok2")
    b3 = await store.lookup("ct-c-tok3")
    assert a1 is not None and a1.revoked_at is not None
    assert a2 is not None and a2.revoked_at is not None
    assert b3 is not None and b3.revoked_at is None


@pytest.mark.asyncio
async def test_revoke_user_idempotent(store: TokenStore) -> None:
    await _upsert(store, _info("ct-c-idem1", user_id="alice"))
    n1 = await store.revoke_user("alice")
    n2 = await store.revoke_user("alice")
    assert n1 == 1
    assert n2 == 0


@pytest.mark.asyncio
async def test_revoke_unknown_user_returns_zero(store: TokenStore) -> None:
    n = await store.revoke_user("nobody-has-this-id")
    assert n == 0


# Per-subject issuance (issue_for_subject) ------------------------------------

_ISS = "https://kc.corp.test/realms/dev"
_T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
_TTL = timedelta(days=30)
_INTERVAL = timedelta(minutes=10)
_MAX_ACTIVE = 2


def _oidc_info(corp_token: str, now: datetime, *, ttl: timedelta = _TTL) -> TokenInfo:
    return TokenInfo(
        corp_token=corp_token,
        user_id="alice",
        team_id="t1",
        scopes=("read",),
        issued_at=now,
        expires_at=now + ttl,
    )


async def _issue(
    store: TokenStore,
    corp_token: str,
    *,
    now: datetime,
    jti: str,
    subject: str = "sub-alice",
    max_active: int = _MAX_ACTIVE,
    min_interval: timedelta = _INTERVAL,
    ttl: timedelta = _TTL,
) -> TokenInfo:
    return await store.issue_for_subject(
        _oidc_info(corp_token, now, ttl=ttl),
        issuer=_ISS,
        subject=subject,
        jti=jti,
        max_active=max_active,
        min_interval=min_interval,
    )


async def _revoked(store: TokenStore, corp_token: str) -> bool:
    info = await store.lookup(corp_token)
    assert info is not None, "expected a stored row"
    return info.revoked_at is not None


async def _stored(store: TokenStore, tokens: list[str]) -> list[TokenInfo]:
    found = [await store.lookup(t) for t in tokens]
    return [info for info in found if info is not None]


def _assert_no_driver_detail(exc: BaseException, *values: str) -> None:
    # The driver's message quotes the conflicting key; it must not ride along.
    assert exc.__cause__ is None
    assert exc.__context__ is None
    rendered = f"{exc!s} {exc!r}"
    for value in values:
        assert value not in rendered


def _codes(results: list[Any]) -> list[str]:
    codes = []
    for result in results:
        if isinstance(result, BaseException):
            assert isinstance(result, IssuancePolicyError), repr(result)
            codes.append(result.code)
    return codes


@pytest.mark.asyncio
async def test_issue_for_subject_stores_an_active_row(store: TokenStore) -> None:
    got = await _issue(store, "ct-iss-1", now=_T0, jti="jti-1")
    assert got.corp_token == "ct-iss-1"
    stored = await store.lookup("ct-iss-1")
    assert stored is not None
    assert (stored.user_id, stored.team_id, stored.scopes) == ("alice", "t1", ("read",))
    assert stored.issued_at == _T0
    assert stored.expires_at == _T0 + _TTL
    assert stored.revoked_at is None


@pytest.mark.asyncio
async def test_issue_beyond_cap_revokes_the_oldest(store: TokenStore) -> None:
    await _issue(store, "ct-cap-a", now=_T0, jti="jti-a")
    await _issue(store, "ct-cap-b", now=_T0 + timedelta(minutes=11), jti="jti-b")
    await _issue(store, "ct-cap-c", now=_T0 + timedelta(minutes=22), jti="jti-c")

    assert await _revoked(store, "ct-cap-a")
    assert not await _revoked(store, "ct-cap-b")
    assert not await _revoked(store, "ct-cap-c")


@pytest.mark.asyncio
async def test_issue_with_cap_of_one_rotates(store: TokenStore) -> None:
    await _issue(store, "ct-one-a", now=_T0, jti="jti-a", max_active=1)
    await _issue(store, "ct-one-b", now=_T0 + timedelta(hours=1), jti="jti-b", max_active=1)

    assert await _revoked(store, "ct-one-a")
    assert not await _revoked(store, "ct-one-b")


@pytest.mark.asyncio
async def test_issue_within_interval_is_rate_limited(store: TokenStore) -> None:
    await _issue(store, "ct-rate-a", now=_T0, jti="jti-a")

    with pytest.raises(IssuancePolicyError) as exc_info:
        await _issue(store, "ct-rate-b", now=_T0 + timedelta(minutes=5), jti="jti-b")

    assert exc_info.value.code == "E_ISSUE_RATE"
    assert exc_info.value.args == ("E_ISSUE_RATE",)
    assert await store.lookup("ct-rate-b") is None
    # The interval boundary itself is allowed.
    await _issue(store, "ct-rate-c", now=_T0 + _INTERVAL, jti="jti-c")
    assert not await _revoked(store, "ct-rate-c")


@pytest.mark.asyncio
async def test_interval_counts_a_revoked_latest_token(store: TokenStore) -> None:
    await _issue(store, "ct-rrev-a", now=_T0, jti="jti-a")
    assert await store.revoke_user("alice") == 1

    with pytest.raises(IssuancePolicyError) as exc_info:
        await _issue(store, "ct-rrev-b", now=_T0 + timedelta(minutes=1), jti="jti-b")

    assert exc_info.value.code == "E_ISSUE_RATE"
    assert await store.lookup("ct-rrev-b") is None
    await _issue(store, "ct-rrev-c", now=_T0 + _INTERVAL, jti="jti-c")
    assert not await _revoked(store, "ct-rrev-c")


@pytest.mark.asyncio
async def test_interval_counts_an_expired_latest_token(store: TokenStore) -> None:
    await _issue(store, "ct-rexp-a", now=_T0, jti="jti-a", ttl=timedelta(minutes=1))

    with pytest.raises(IssuancePolicyError) as exc_info:
        await _issue(store, "ct-rexp-b", now=_T0 + timedelta(minutes=5), jti="jti-b")

    assert exc_info.value.code == "E_ISSUE_RATE"
    assert await store.lookup("ct-rexp-b") is None
    await _issue(store, "ct-rexp-c", now=_T0 + _INTERVAL, jti="jti-c")
    assert not await _revoked(store, "ct-rexp-c")


@pytest.mark.asyncio
async def test_issued_row_is_stored_unrevoked(store: TokenStore) -> None:
    info = dataclasses.replace(_oidc_info("ct-unrev", _T0), revoked_at=_T0)

    got = await store.issue_for_subject(
        info,
        issuer=_ISS,
        subject="sub-alice",
        jti="jti-unrev",
        max_active=_MAX_ACTIVE,
        min_interval=_INTERVAL,
    )

    assert got.revoked_at is None
    assert not await _revoked(store, "ct-unrev")


@pytest.mark.asyncio
async def test_corp_token_collision_is_an_opaque_runtime_error(store: TokenStore) -> None:
    await _upsert(store, _info("ct-collide-7f3a", user_id="bob"))

    with pytest.raises(RuntimeError) as exc_info:
        await _issue(store, "ct-collide-7f3a", now=_T0, jti="jti-collide-9c1e")

    _assert_no_driver_detail(
        exc_info.value, "ct-collide-7f3a", "jti-collide-9c1e", "Key (", "duplicate key"
    )
    kept = await store.lookup("ct-collide-7f3a")
    assert kept is not None and kept.user_id == "bob"
    # The failed attempt consumed neither the jti nor the interval.
    await _issue(store, "ct-collide-next", now=_T0, jti="jti-collide-9c1e")


@pytest.mark.asyncio
async def test_same_jti_twice_is_a_replay(store: TokenStore) -> None:
    await _issue(store, "ct-replay-a", now=_T0, jti="jti-same")

    with pytest.raises(IssuancePolicyError) as exc_info:
        await _issue(store, "ct-replay-b", now=_T0 + timedelta(hours=1), jti="jti-same")

    assert exc_info.value.code == "E_ISSUE_REPLAY"
    assert exc_info.value.args == ("E_ISSUE_REPLAY",)
    assert await store.lookup("ct-replay-b") is None
    assert not await _revoked(store, "ct-replay-a")


@pytest.mark.asyncio
async def test_replay_is_reported_before_the_interval(store: TokenStore) -> None:
    await _issue(store, "ct-order-a", now=_T0, jti="jti-same")

    with pytest.raises(IssuancePolicyError) as exc_info:
        await _issue(store, "ct-order-b", now=_T0 + timedelta(seconds=1), jti="jti-same")

    assert exc_info.value.code == "E_ISSUE_REPLAY"


@pytest.mark.asyncio
async def test_jti_is_single_use_across_subjects(store: TokenStore) -> None:
    await _issue(store, "ct-xsub-a", now=_T0, jti="jti-shared", subject="sub-alice")

    with pytest.raises(IssuancePolicyError) as exc_info:
        await _issue(store, "ct-xsub-b", now=_T0, jti="jti-shared", subject="sub-bob")

    assert exc_info.value.code == "E_ISSUE_REPLAY"


@pytest.mark.asyncio
async def test_subjects_have_independent_caps_and_intervals(store: TokenStore) -> None:
    await _issue(store, "ct-ind-a1", now=_T0, jti="jti-a1", subject="sub-alice", max_active=1)
    await _issue(store, "ct-ind-b1", now=_T0, jti="jti-b1", subject="sub-bob", max_active=1)

    assert not await _revoked(store, "ct-ind-a1")
    assert not await _revoked(store, "ct-ind-b1")


@pytest.mark.asyncio
async def test_expired_unrevoked_tokens_do_not_count_toward_the_cap(store: TokenStore) -> None:
    await _issue(store, "ct-exp-a", now=_T0, jti="jti-a", max_active=1, ttl=timedelta(days=1))
    await _issue(store, "ct-exp-b", now=_T0 + timedelta(days=2), jti="jti-b", max_active=1)

    # The expired row was not the one "rotated out": nothing was revoked.
    assert not await _revoked(store, "ct-exp-a")
    assert not await _revoked(store, "ct-exp-b")


@pytest.mark.asyncio
async def test_cli_issued_rows_are_ignored_by_the_cap(store: TokenStore) -> None:
    await _upsert(store, _info("ct-cli-1", user_id="alice"))
    await _upsert(store, _info("ct-cli-2", user_id="alice"))

    await _issue(store, "ct-mix-a", now=_T0, jti="jti-a")
    await _issue(store, "ct-mix-b", now=_T0 + timedelta(minutes=11), jti="jti-b")
    await _issue(store, "ct-mix-c", now=_T0 + timedelta(minutes=22), jti="jti-c")

    assert not await _revoked(store, "ct-cli-1")
    assert not await _revoked(store, "ct-cli-2")
    assert await _revoked(store, "ct-mix-a")
    assert not await _revoked(store, "ct-mix-c")


@pytest.mark.asyncio
async def test_parallel_identical_requests_yield_exactly_one_token(store: TokenStore) -> None:
    tokens = [f"ct-same-{i}" for i in range(_RACE_WIDTH)]
    results = await asyncio.gather(
        *(_issue(store, t, now=_T0, jti="jti-same") for t in tokens),
        return_exceptions=True,
    )

    assert sorted(_codes(results)) == ["E_ISSUE_REPLAY"] * (_RACE_WIDTH - 1)
    assert len(await _stored(store, tokens)) == 1


@pytest.mark.asyncio
async def test_parallel_same_jti_across_subjects_yields_exactly_one_token(
    store: TokenStore,
) -> None:
    # Distinct subjects take distinct locks, so only the jti uniqueness backstop
    # can stop the second mint.
    tokens = [f"ct-xrace-{i}" for i in range(_RACE_WIDTH)]
    results = await asyncio.gather(
        *(
            _issue(store, t, now=_T0, jti="jti-shared", subject=f"sub-{i}")
            for i, t in enumerate(tokens)
        ),
        return_exceptions=True,
    )

    assert sorted(_codes(results)) == ["E_ISSUE_REPLAY"] * (_RACE_WIDTH - 1)
    for result in results:
        if isinstance(result, IssuancePolicyError):
            assert result.args == ("E_ISSUE_REPLAY",)
            _assert_no_driver_detail(result, "jti-shared", *tokens)
    assert len(await _stored(store, tokens)) == 1


@pytest.mark.asyncio
async def test_parallel_distinct_jtis_at_empty_capacity(store: TokenStore) -> None:
    tokens = [f"ct-empty-{i}" for i in range(_RACE_WIDTH)]
    results = await asyncio.gather(
        *(_issue(store, t, now=_T0, jti=f"jti-empty-{i}") for i, t in enumerate(tokens)),
        return_exceptions=True,
    )

    assert sorted(_codes(results)) == ["E_ISSUE_RATE"] * (_RACE_WIDTH - 1)
    stored = await _stored(store, tokens)
    assert len(stored) == 1
    assert all(info.revoked_at is None for info in stored)


@pytest.mark.asyncio
async def test_parallel_distinct_jtis_at_full_capacity(store: TokenStore) -> None:
    await _issue(store, "ct-full-old", now=_T0, jti="jti-old")
    await _issue(store, "ct-full-new", now=_T0 + timedelta(minutes=11), jti="jti-new")
    later = _T0 + timedelta(hours=1)
    tokens = [f"ct-full-{i}" for i in range(_RACE_WIDTH)]

    results = await asyncio.gather(
        *(_issue(store, t, now=later, jti=f"jti-full-{i}") for i, t in enumerate(tokens)),
        return_exceptions=True,
    )

    assert sorted(_codes(results)) == ["E_ISSUE_RATE"] * (_RACE_WIDTH - 1)
    rows = await _stored(store, ["ct-full-old", "ct-full-new", *tokens])
    active = [info for info in rows if info.revoked_at is None]
    revoked = [info for info in rows if info.revoked_at is not None]
    assert len(active) == _MAX_ACTIVE
    assert [info.corp_token for info in revoked] == ["ct-full-old"]


@pytest.mark.asyncio
async def test_parallel_issuance_without_interval_never_exceeds_the_cap(store: TokenStore) -> None:
    await _issue(store, "ct-noint-old", now=_T0, jti="jti-old")
    await _issue(store, "ct-noint-new", now=_T0 + timedelta(minutes=11), jti="jti-new")
    later = _T0 + timedelta(hours=1)
    tokens = [f"ct-noint-{i}" for i in range(_RACE_WIDTH)]

    results = await asyncio.gather(
        *(
            _issue(store, t, now=later, jti=f"jti-noint-{i}", min_interval=timedelta(0))
            for i, t in enumerate(tokens)
        ),
        return_exceptions=True,
    )

    assert _codes(results) == []
    rows = await _stored(store, ["ct-noint-old", "ct-noint-new", *tokens])
    assert len(rows) == _RACE_WIDTH + 2
    assert sum(info.revoked_at is None for info in rows) == _MAX_ACTIVE


@pytest.mark.asyncio
async def test_a_subject_at_cap_with_only_expired_and_revoked_rows_revokes_nothing(
    store: TokenStore,
) -> None:
    expired = await _issue(store, "ct-dead-exp", now=_T0, jti="jti-a", ttl=timedelta(minutes=5))
    gone = await _issue(store, "ct-dead-rev", now=_T0 + timedelta(minutes=11), jti="jti-b")
    revoked_at = _T0 + timedelta(minutes=12)
    await _upsert(store, dataclasses.replace(gone, revoked_at=revoked_at))

    await _issue(store, "ct-dead-c", now=_T0 + timedelta(minutes=22), jti="jti-c")
    await _issue(store, "ct-dead-d", now=_T0 + timedelta(minutes=33), jti="jti-d")

    assert not await _revoked(store, expired.corp_token)
    kept = await store.lookup(gone.corp_token)
    assert kept is not None and kept.revoked_at == revoked_at
    assert not await _revoked(store, "ct-dead-c")
    assert not await _revoked(store, "ct-dead-d")

    await _issue(store, "ct-dead-e", now=_T0 + timedelta(minutes=44), jti="jti-e")
    assert await _revoked(store, "ct-dead-c")
    assert not await _revoked(store, expired.corp_token)
    kept = await store.lookup(gone.corp_token)
    assert kept is not None and kept.revoked_at == revoked_at


@pytest.mark.asyncio
async def test_a_token_expiring_exactly_now_is_not_active(store: TokenStore) -> None:
    await _issue(store, "ct-edge-a", now=_T0, jti="jti-a", ttl=_INTERVAL, max_active=1)

    await _issue(store, "ct-edge-b", now=_T0 + _INTERVAL, jti="jti-b", max_active=1)

    # expires_at == now is not active, so it is not "rotated out" either.
    assert not await _revoked(store, "ct-edge-a")
    assert not await _revoked(store, "ct-edge-b")


@pytest.mark.asyncio
async def test_a_clock_step_back_never_bypasses_the_interval(store: TokenStore) -> None:
    await _issue(store, "ct-skew-a", now=_T0, jti="jti-a")

    with pytest.raises(IssuancePolicyError) as exc_info:
        await _issue(store, "ct-skew-b", now=_T0 - timedelta(hours=1), jti="jti-b")

    assert exc_info.value.code == "E_ISSUE_RATE"
    assert await store.lookup("ct-skew-b") is None


@pytest.mark.asyncio
async def test_a_refused_issuance_consumes_neither_the_jti_nor_the_interval(
    store: TokenStore,
) -> None:
    await _issue(store, "ct-keep-a", now=_T0, jti="jti-a")
    with pytest.raises(IssuancePolicyError):
        await _issue(store, "ct-keep-b", now=_T0 + timedelta(minutes=1), jti="jti-b")

    # jti-b was refused on RATE, so it is still unused once the interval passes.
    await _issue(store, "ct-keep-c", now=_T0 + _INTERVAL, jti="jti-b")
    assert not await _revoked(store, "ct-keep-c")


@pytest.mark.asyncio
async def test_jti_is_single_use_across_issuers(store: TokenStore) -> None:
    await _issue(store, "ct-xiss-a", now=_T0, jti="jti-shared")

    with pytest.raises(IssuancePolicyError) as exc_info:
        await store.issue_for_subject(
            _oidc_info("ct-xiss-b", _T0),
            issuer="https://kc.other.test/realms/ops",
            subject="sub-alice",
            jti="jti-shared",
            max_active=_MAX_ACTIVE,
            min_interval=_INTERVAL,
        )

    assert exc_info.value.code == "E_ISSUE_REPLAY"
    assert await store.lookup("ct-xiss-b") is None


@pytest.mark.asyncio
async def test_the_same_subject_under_two_issuers_is_two_identities(store: TokenStore) -> None:
    other = "https://kc.other.test/realms/dev"
    await _issue(store, "ct-2iss-a", now=_T0, jti="jti-a", max_active=1)
    await store.issue_for_subject(
        _oidc_info("ct-2iss-b", _T0),
        issuer=other,
        subject="sub-alice",
        jti="jti-b",
        max_active=1,
        min_interval=_INTERVAL,
    )

    assert not await _revoked(store, "ct-2iss-a")
    assert not await _revoked(store, "ct-2iss-b")


@pytest.mark.asyncio
async def test_identities_that_join_to_the_same_lock_key_never_share_rows(
    store: TokenStore,
) -> None:
    # issuer + separator + subject is the same string for both, so on Postgres
    # they take the same advisory lock; their rows must still stay apart.
    first = ("https://kc.corp.test", "realm\x1fsub")
    second = ("https://kc.corp.test\x1frealm", "sub")
    for i, (issuer, subject) in enumerate((first, second)):
        await store.issue_for_subject(
            _oidc_info(f"ct-join-{i}", _T0),
            issuer=issuer,
            subject=subject,
            jti=f"jti-join-{i}",
            max_active=1,
            min_interval=_INTERVAL,
        )

    assert not await _revoked(store, "ct-join-0")
    assert not await _revoked(store, "ct-join-1")


@pytest.mark.asyncio
async def test_unicode_and_long_subjects_are_keyed_exactly(store: TokenStore) -> None:
    subject = "пользователь-用户-🔑-" + "ж" * 500
    near = subject[:-1] + "з"
    await _issue(store, "ct-uni-a", now=_T0, jti="jti-uni-a", subject=subject, max_active=1)
    await _issue(store, "ct-uni-near", now=_T0, jti="jti-uni-near", subject=near, max_active=1)

    with pytest.raises(IssuancePolicyError) as exc_info:
        await _issue(
            store,
            "ct-uni-b",
            now=_T0 + timedelta(minutes=1),
            jti="jti-uni-b",
            subject=subject,
            max_active=1,
        )
    assert exc_info.value.code == "E_ISSUE_RATE"

    await _issue(
        store, "ct-uni-c", now=_T0 + _INTERVAL, jti="jti-uni-c", subject=subject, max_active=1
    )
    assert await _revoked(store, "ct-uni-a")
    assert not await _revoked(store, "ct-uni-near")
    assert not await _revoked(store, "ct-uni-c")


@pytest.mark.asyncio
async def test_parallel_issuance_for_many_subjects_is_not_serialised_into_refusals(
    store: TokenStore,
) -> None:
    tokens = [f"ct-many-{i}" for i in range(_RACE_WIDTH)]
    results = await asyncio.gather(
        *(
            _issue(store, t, now=_T0, jti=f"jti-many-{i}", subject=f"sub-many-{i}")
            for i, t in enumerate(tokens)
        ),
        return_exceptions=True,
    )

    assert [r for r in results if isinstance(r, BaseException)] == []
    assert len(await _stored(store, tokens)) == _RACE_WIDTH


# CI wiring -------------------------------------------------------------------

_CI_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"


def test_ci_test_job_runs_the_postgres_the_contract_tests_need() -> None:
    """Without the service the Postgres half of this file would fail on CI; this
    pins the wiring so the service cannot be dropped for a skip-shaped green."""
    job = yaml.safe_load(_CI_WORKFLOW.read_text())["jobs"]["test"]
    service = job["services"]["postgres"]

    assert service["image"].startswith("postgres:16")
    assert service["env"] == {
        "POSTGRES_USER": "gateway",
        "POSTGRES_PASSWORD": "gateway",
        "POSTGRES_DB": "gateway",
    }
    assert "5432:5432" in service["ports"]
    assert "pg_isready" in service["options"]

    # scripts/test-gates.sh runs the suite with the job's step env.
    steps = [
        step
        for step in job["steps"]
        if any(marker in (step.get("run") or "") for marker in ("pytest", "test-gates.sh"))
    ]
    assert steps
    for step in steps:
        env = {**(job.get("env") or {}), **(step.get("env") or {})}
        assert env.get(PG_DSN_ENV_VAR) == DEMO_PG_DSN
