"""The issuance route's bound holds against a real Postgres that stops answering.

A stalled server is not a refused one: the driver's cancel handshake opens a fresh
connection and waits on it with no timeout, so an unbounded unwind (ROLLBACK,
pool release) outlives the route's bound. Driven through a relay that black-holes
on demand (tests/stalling_proxy.py), against the test Postgres.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio

from corp_llm_gateway.healthz import build_health_router
from corp_llm_gateway.healthz.checks import (
    ExtensionsCheck,
    HealthStatus,
    LiveCheck,
    ReadyCheck,
    SanitizationCheck,
)
from corp_llm_gateway.healthz.server import HealthRouter
from corp_llm_gateway.tokens import IssuancePolicy, OidcClaims, TokenIssuer
from tests.healthz.test_issue_token_route import _settings
from tests.postgres_support import pg_dsn, require_asyncpg, skip_or_fail
from tests.stalling_proxy import StallingProxy

PATH = "/internal/issue-token"
ISS = "https://kc.corp.test/realms/dev"
ROUTE_BOUND_S = 2.0
MARGIN_S = 1.0


async def _ok() -> bool:
    return True


async def _ext() -> dict[str, HealthStatus]:
    return {}


@dataclass
class _Stack:
    proxy: StallingProxy
    via_proxy: str
    direct: str


@pytest_asyncio.fixture
async def stack() -> AsyncIterator[_Stack]:
    require_asyncpg()
    import asyncpg

    direct = pg_dsn()
    try:
        conn = await asyncpg.connect(direct, timeout=5)
        await conn.close()
    except Exception as exc:
        skip_or_fail(f"Postgres unreachable: {type(exc).__name__}")
    proxy = StallingProxy(direct)
    via_proxy = await proxy.start()
    try:
        yield _Stack(proxy, via_proxy, direct)
    finally:
        await proxy.close()


def _router(issuer: TokenIssuer) -> HealthRouter:
    return build_health_router(
        live_check=LiveCheck(),
        ready_check=ReadyCheck(check_redis=_ok, check_postgres=_ok),
        sanitization_check=SanitizationCheck(run_round_trip=_ok),
        extensions_check=ExtensionsCheck(health_all=_ext),
        token_issuer=issuer,
        issue_timeout_s=ROUTE_BOUND_S,
    )


async def _post(router: HealthRouter) -> httpx.Response:
    transport = httpx.ASGITransport(app=router)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
        return await client.post(PATH, headers={"Authorization": "Bearer eyJ.stall.sig"})


async def _wait_for_lock_waiter(conn: object) -> None:
    for _ in range(500):
        waiting = await conn.fetchval(  # type: ignore[attr-defined]
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND wait_event_type = 'Lock'"
        )
        if waiting:
            return
        await asyncio.sleep(0.01)
    pytest.fail("issuance never blocked on the subject lock")


async def test_a_postgres_that_stalls_mid_issuance_is_unwound_within_the_route_bound(
    stack: _Stack,
) -> None:
    import asyncpg

    from corp_llm_gateway.pg_session import RELEASE_BUDGET_S
    from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

    store = PostgresTokenStore(stack.via_proxy, pool_max_size=1)
    await store.init_schema()
    first = f"sub-stall-{secrets.token_hex(4)}"
    subjects = iter([first, f"sub-after-{secrets.token_hex(4)}"])

    async def verify(token: str) -> OidcClaims:
        return OidcClaims(
            "pg-test-stall",
            "t1",
            issuer=ISS,
            subject=next(subjects),
            jti=f"jti-stall-{secrets.token_hex(6)}",
        )

    router = _router(TokenIssuer(store, verify, policy=IssuancePolicy(store, _settings())))
    holder = await asyncpg.connect(stack.direct, timeout=5)
    tx = holder.transaction()
    await tx.start()
    try:
        await holder.execute("SELECT pg_advisory_xact_lock(hashtext($1))", f"{ISS}\x1f{first}")
        loop = asyncio.get_running_loop()
        start = loop.time()
        request = asyncio.create_task(_post(router))
        await _wait_for_lock_waiter(holder)
        stack.proxy.stall()
        resp = await asyncio.wait_for(request, timeout=15)
        elapsed = loop.time() - start

        assert (resp.status_code, resp.json()) == (503, {"error": "E_ISSUE_STORE_TIMEOUT"})
        assert elapsed < ROUTE_BOUND_S + RELEASE_BUDGET_S + MARGIN_S
        assert router._issue_inflight == 0
    finally:
        stack.proxy.resume()
        await tx.rollback()
        await holder.close()

    try:
        # The stalled connection was dropped; the pool opens a new one.
        resp = await asyncio.wait_for(_post(router), timeout=15)
        assert resp.status_code == 200, resp.text
        assert await store.lookup(resp.json()["corp_token"]) is not None
    finally:
        pool = await store._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM corp_tokens WHERE user_id = 'pg-test-stall'")
        await store.close()


async def test_a_team_lookup_on_a_stalled_postgres_is_unwound_within_the_route_bound(
    stack: _Stack,
) -> None:
    from corp_llm_gateway.pg_session import RELEASE_BUDGET_S
    from corp_llm_gateway.team_config import TeamConfig
    from corp_llm_gateway.team_config.postgres_store import PostgresTeamConfigStore
    from corp_llm_gateway.tokens import InMemoryTokenStore

    teams = PostgresTeamConfigStore(stack.via_proxy)
    team_id = f"t-stall-{secrets.token_hex(4)}"
    await teams.init_schema()
    await teams.upsert(TeamConfig(team_id=team_id, name="stall"))

    async def verify(token: str) -> OidcClaims:
        await teams.get(team_id)
        return OidcClaims(
            "pg-test-stall",
            team_id,
            issuer=ISS,
            subject=f"sub-{secrets.token_hex(4)}",
            jti=f"jti-{secrets.token_hex(6)}",
        )

    tokens = InMemoryTokenStore()
    router = _router(TokenIssuer(tokens, verify, policy=IssuancePolicy(tokens, _settings())))
    try:
        stack.proxy.stall()
        loop = asyncio.get_running_loop()
        start = loop.time()
        resp = await asyncio.wait_for(_post(router), timeout=15)
        elapsed = loop.time() - start

        assert (resp.status_code, resp.json()) == (503, {"error": "E_ISSUE_STORE_TIMEOUT"})
        assert elapsed < ROUTE_BOUND_S + RELEASE_BUDGET_S + MARGIN_S
        assert router._issue_inflight == 0

        stack.proxy.resume()
        resp = await asyncio.wait_for(_post(router), timeout=15)
        assert resp.status_code == 200, resp.text
    finally:
        stack.proxy.resume()
        pool = await teams._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM team_config WHERE team_id = $1", team_id)
        await teams.close()


@pytest.mark.parametrize("which", ["tokens", "team_config"])
async def test_pooled_connections_ask_the_server_for_tcp_keepalives(
    stack: _Stack, which: str
) -> None:
    # A partitioned client leaves its backend (and the subject lock its transaction
    # holds) behind; server keepalives drop that backend in ~25 s.
    if which == "tokens":
        from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

        store: object = PostgresTokenStore(stack.direct)
    else:
        from corp_llm_gateway.team_config.postgres_store import PostgresTeamConfigStore

        store = PostgresTeamConfigStore(stack.direct)
    try:
        pool = await store._get_pool()  # type: ignore[attr-defined]
        async with pool.acquire() as conn:
            names = ("tcp_keepalives_idle", "tcp_keepalives_interval", "tcp_keepalives_count")
            got = [await conn.fetchval(f"SHOW {name}") for name in names]
    finally:
        await store.close()  # type: ignore[attr-defined]

    assert got == ["10", "5", "3"]
