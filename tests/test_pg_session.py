"""pg_session: a pooled connection is unwound within a budget however the server
behaves, and each Postgres failure class means what the route, the auth path and
the boot say it means. The real-server stall cases live in
tests/healthz/test_issue_token_pg_stall.py."""

from __future__ import annotations

import asyncio
import ssl

import pytest

from corp_llm_gateway import pg_session
from corp_llm_gateway.pg_session import (
    BOOT_REFUSE,
    BOOT_REFUSE_PRIVILEGE,
    BOOT_REFUSE_TLS,
    BOOT_WARN,
    boot_probe_outcome,
    in_transaction,
    run_on_connection,
    store_unavailable,
)


class _Conn:
    def __init__(self) -> None:
        self.terminated = 0
        self.events: list[str] = []

    def terminate(self) -> None:
        self.terminated += 1

    def transaction(self, **options: object) -> _Tx:
        self.events.append(f"tx {options}")
        return _Tx(self)


class _Tx:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def start(self) -> None:
        self._conn.events.append("start")

    async def commit(self) -> None:
        self._conn.events.append("commit")

    async def rollback(self) -> None:
        self._conn.events.append("rollback")


class _Pool:
    def __init__(self, *, release_hangs: bool = False) -> None:
        self.conn = _Conn()
        self.release_hangs = release_hangs
        self.acquire_timeouts: list[float] = []
        self.released: list[float | None] = []

    async def acquire(self, *, timeout: float) -> _Conn:
        self.acquire_timeouts.append(timeout)
        return self.conn

    async def release(self, conn: _Conn, *, timeout: float | None = None) -> None:
        self.released.append(timeout)
        if self.release_hangs and not conn.terminated:
            await asyncio.sleep(3600)


async def _elapsed(coro: object) -> tuple[object, float]:
    loop = asyncio.get_running_loop()
    start = loop.time()
    result = await asyncio.wait_for(coro, timeout=5)  # type: ignore[arg-type]
    return result, loop.time() - start


async def test_the_work_runs_on_a_connection_acquired_within_the_bound() -> None:
    pool = _Pool()

    async def work(conn: _Conn) -> str:
        assert conn is pool.conn
        return "done"

    assert await run_on_connection(pool, work, acquire_timeout=1.5, release_budget=0.3) == "done"
    assert pool.acquire_timeouts == [1.5]
    assert pool.released == [0.3]
    assert pool.conn.terminated == 0


async def test_a_release_past_its_budget_drops_the_connection_and_keeps_the_result() -> None:
    pool = _Pool(release_hangs=True)

    async def work(conn: _Conn) -> str:
        return "committed"

    result, elapsed = await _elapsed(
        run_on_connection(pool, work, acquire_timeout=1, release_budget=0.2)
    )

    assert result == "committed"
    assert elapsed < 1.0
    assert pool.conn.terminated >= 1


async def test_a_cancelled_work_terminates_the_connection_without_waiting_on_it() -> None:
    pool = _Pool(release_hangs=True)
    entered = asyncio.Event()

    async def work(conn: _Conn) -> None:
        entered.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(run_on_connection(pool, work, acquire_timeout=1, release_budget=60))
    await entered.wait()
    task.cancel()
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert loop.time() - start < 1.0
    assert pool.conn.terminated >= 1


async def test_an_error_in_the_work_propagates_and_the_connection_goes_back() -> None:
    pool = _Pool()

    async def work(conn: _Conn) -> None:
        raise LookupError("query fault")

    with pytest.raises(LookupError):
        await run_on_connection(pool, work, acquire_timeout=1)

    assert pool.released == [pg_session.RELEASE_BUDGET_S]
    assert pool.conn.terminated == 0


async def test_a_release_that_fails_never_replaces_the_works_error() -> None:
    class _BrokenPool(_Pool):
        async def release(self, conn: _Conn, *, timeout: float | None = None) -> None:
            raise ConnectionResetError

    pool = _BrokenPool()

    async def work(conn: _Conn) -> None:
        raise LookupError("query fault")

    with pytest.raises(LookupError):
        await run_on_connection(pool, work, acquire_timeout=1)
    assert pool.conn.terminated == 1


async def test_the_transaction_commits_on_success_and_rolls_back_on_an_error() -> None:
    conn = _Conn()

    async def ok(c: _Conn) -> int:
        return 1

    async def fails(c: _Conn) -> int:
        raise LookupError

    assert await in_transaction(conn, ok, isolation="read_committed") == 1
    with pytest.raises(LookupError):
        await in_transaction(conn, fails)

    assert conn.events == [
        "tx {'isolation': 'read_committed'}",
        "start",
        "commit",
        "tx {}",
        "start",
        "rollback",
    ]


async def test_a_cancelled_transaction_neither_commits_nor_waits_on_a_rollback() -> None:
    conn = _Conn()

    async def cancelled(c: _Conn) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await in_transaction(conn, cancelled)

    assert conn.events == ["tx {}", "start"]


# ── store_unavailable ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [TimeoutError(), ConnectionRefusedError(), ConnectionResetError(), OSError()],
    ids=["timeout", "refused", "reset", "oserror"],
)
def test_socket_class_failures_are_unavailable(exc: BaseException) -> None:
    assert store_unavailable(exc)


@pytest.mark.parametrize(
    "name",
    [
        "PostgresConnectionError",
        "ConnectionDoesNotExistError",
        "ConnectionFailureError",
        "ConnectionRejectionError",
        "AdminShutdownError",
        "CrashShutdownError",
        "CannotConnectNowError",
        "TooManyConnectionsError",
        "InterfaceError",
    ],
)
def test_asyncpg_connection_classes_are_unavailable(name: str) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    assert store_unavailable(getattr(asyncpg.exceptions, name)("x"))


@pytest.mark.parametrize(
    "name", ["QueryCanceledError", "UndefinedColumnError", "UniqueViolationError"]
)
def test_asyncpg_query_faults_are_not_unavailable(name: str) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    assert not store_unavailable(getattr(asyncpg.exceptions, name)("x"))


def test_a_plain_error_is_not_unavailable() -> None:
    assert not store_unavailable(RuntimeError("x"))


# ── the boot probe matrix ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "exc",
    [
        ssl.SSLError(1, "x"),
        ssl.SSLCertVerificationError(1, "x"),
        ssl.SSLZeroReturnError(1, "x"),
    ],
    ids=["SSLError", "SSLCertVerificationError", "SSLZeroReturnError"],
)
def test_a_tls_failure_refuses_the_boot_though_it_is_an_oserror(exc: BaseException) -> None:
    assert isinstance(exc, OSError)
    assert boot_probe_outcome(exc) == BOOT_REFUSE_TLS


@pytest.mark.parametrize(
    "exc",
    [ConnectionRefusedError(), ConnectionResetError(), TimeoutError(), OSError()],
    ids=["refused", "reset", "timeout", "oserror"],
)
def test_a_network_failure_warns_and_boots(exc: BaseException) -> None:
    assert boot_probe_outcome(exc) == BOOT_WARN


@pytest.mark.parametrize(
    ("name", "outcome"),
    [
        ("PostgresConnectionError", BOOT_WARN),
        ("ConnectionDoesNotExistError", BOOT_WARN),
        ("ConnectionFailureError", BOOT_WARN),
        ("ConnectionRejectionError", BOOT_WARN),
        ("OperatorInterventionError", BOOT_WARN),
        ("AdminShutdownError", BOOT_WARN),
        ("CrashShutdownError", BOOT_WARN),
        ("CannotConnectNowError", BOOT_WARN),
        ("TooManyConnectionsError", BOOT_WARN),
        ("QueryCanceledError", BOOT_REFUSE),
        ("InsufficientPrivilegeError", BOOT_REFUSE_PRIVILEGE),
        ("InvalidPasswordError", BOOT_REFUSE),
        ("InvalidCatalogNameError", BOOT_REFUSE),
        ("InvalidAuthorizationSpecificationError", BOOT_REFUSE),
        ("ClientConfigurationError", BOOT_REFUSE),
        ("InterfaceError", BOOT_REFUSE),
    ],
)
def test_each_asyncpg_class_has_its_boot_outcome(name: str, outcome: str) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    assert boot_probe_outcome(getattr(asyncpg.exceptions, name)("x")) == outcome


def test_an_unlisted_failure_refuses_the_boot() -> None:
    assert boot_probe_outcome(RuntimeError("x")) == BOOT_REFUSE
    assert boot_probe_outcome(ValueError("x")) == BOOT_REFUSE


def test_each_subclass_in_the_matrix_precedes_its_base() -> None:
    import builtins
    import sys

    pytest.importorskip("asyncpg")
    classes = []
    for qualified, _ in pg_session.BOOT_PROBE_OUTCOMES:
        module, _, name = qualified.rpartition(".")
        cls = getattr(builtins if module == "builtins" else sys.modules[module], name)
        classes.append(cls)
    for i, earlier in enumerate(classes):
        for later in classes[i + 1 :]:
            assert not issubclass(later, earlier), (later, earlier)
