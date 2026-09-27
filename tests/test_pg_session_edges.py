"""pg_session at its edges: an acquire that fails or is cancelled, a cancel
request that fails, a connection already closed, a release that lands inside
the grace, and startup-parameter rejections at boot."""

from __future__ import annotations

import asyncio

import pytest

from corp_llm_gateway import pg_session
from corp_llm_gateway.pg_session import (
    BOOT_REFUSE,
    boot_probe_outcome,
    run_on_connection,
    store_unavailable,
)
from tests.test_pg_session import _CancellableConn, _Conn, _Pool

SECRET = "pg-secret-AKIAIOSFODNN7EXAMPLE"


class _AcquireFails(_Pool):
    def __init__(self, fault: BaseException) -> None:
        super().__init__()
        self.fault = fault

    async def acquire(self, *, timeout: float) -> _Conn:
        self.acquire_timeouts.append(timeout)
        raise self.fault


async def test_an_acquire_that_fails_runs_no_work_and_releases_nothing() -> None:
    pool = _AcquireFails(ConnectionRefusedError(f"connect {SECRET}"))
    ran: list[bool] = []

    async def work(conn: _Conn) -> None:
        ran.append(True)

    with pytest.raises(ConnectionRefusedError) as caught:
        await run_on_connection(pool, work, acquire_timeout=1)

    assert store_unavailable(caught.value)
    assert ran == []
    assert pool.released == []
    assert pool.conn.terminated == 0


async def test_an_acquire_timeout_is_a_store_outage() -> None:
    pool = _AcquireFails(TimeoutError())

    async def work(conn: _Conn) -> None:
        raise AssertionError("unreachable")

    with pytest.raises(TimeoutError) as caught:
        await run_on_connection(pool, work, acquire_timeout=0.1)

    assert store_unavailable(caught.value)
    assert pool.released == []


async def test_a_cancel_during_acquire_runs_no_work_and_releases_nothing() -> None:
    class _SlowPool(_Pool):
        def __init__(self) -> None:
            super().__init__()
            self.waiting = asyncio.Event()

        async def acquire(self, *, timeout: float) -> _Conn:
            self.waiting.set()
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    pool = _SlowPool()
    ran: list[bool] = []

    async def work(conn: _Conn) -> None:
        ran.append(True)

    task = asyncio.create_task(run_on_connection(pool, work, acquire_timeout=1))
    await asyncio.wait_for(pool.waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)

    assert ran == []
    assert pool.released == []
    assert pool.conn.terminated == 0


async def test_an_already_closed_connection_is_not_sent_a_cancel_but_is_still_dropped() -> None:
    pool = _Pool()
    conn = _CancellableConn()
    conn.terminated = 1  # closed under us, e.g. by the server
    pool.conn = conn

    async def work(c: _Conn) -> None:
        raise TimeoutError

    with pytest.raises(TimeoutError):
        await run_on_connection(pool, work, acquire_timeout=1)

    assert "cancel" not in conn.events
    assert "terminate" in conn.events


async def test_a_release_that_lands_inside_the_grace_keeps_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pg_session, "RELEASE_GRACE_S", 0.4)

    class _LatePool(_Pool):
        async def release(self, conn: _Conn, *, timeout: float | None = None) -> None:
            self.released.append(timeout)
            # Past asyncpg's budget, inside ours.
            await asyncio.sleep(0.25)

    pool = _LatePool()

    async def work(conn: _Conn) -> str:
        return "done"

    result = await run_on_connection(pool, work, acquire_timeout=1, release_budget=0.1)

    assert result == "done"
    assert pool.released == [0.1]
    assert pool.conn.terminated == 0


@pytest.mark.parametrize("name", ["InvalidParameterValueError", "CantChangeRuntimeParamError"])
def test_a_server_that_rejects_the_keepalive_settings_refuses_the_boot(name: str) -> None:
    # A Postgres that refuses a startup setting will refuse it on every restart.
    asyncpg = pytest.importorskip("asyncpg")
    exc = getattr(asyncpg.exceptions, name)("invalid value for tcp_keepalives_idle")

    assert boot_probe_outcome(exc) == BOOT_REFUSE
    assert not store_unavailable(exc)


class _CancelRefused(_CancellableConn):
    """asyncpg's ``_cancel`` hands every failure to its waiter, never raises."""

    async def _cancel(self, waiter: asyncio.Future[None]) -> None:
        self.events.append("cancel")
        if not waiter.done():
            waiter.set_exception(ConnectionRefusedError(f"cancel to {SECRET} refused"))


@pytest.mark.parametrize("fault", [asyncio.CancelledError, TimeoutError])
async def test_a_refused_cancel_request_still_drops_the_connection_and_reaches_no_handler(
    fault: type[BaseException],
) -> None:
    from tests.loop_errors import describe, loop_errors

    pool = _Pool()
    pool.conn = _CancelRefused()

    async def work(conn: _Conn) -> None:
        raise fault

    async with loop_errors(settle_s=0.05) as seen:
        with pytest.raises(fault):
            await run_on_connection(pool, work, acquire_timeout=1)

    assert pool.conn.events[:2] == ["cancel", "terminate"]
    assert seen == [], describe(seen)
