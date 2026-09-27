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
    BOOT_REFUSE_STARTUP_PARAMETER,
    BOOT_REFUSE_TLS,
    BOOT_WARN,
    KEEPALIVE_SERVER_SETTINGS,
    STARTUP_PARAMETER_REJECTED,
    StartupParameterRejectedError,
    boot_probe_outcome,
    connect_with_keepalives,
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
        while self.release_hangs and not conn.terminated:
            await asyncio.sleep(0.01)


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


async def test_a_release_past_its_budget_drops_the_connection_and_keeps_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pg_session, "RELEASE_GRACE_S", 0.2)
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


class _BudgetedPool(_Pool):
    """Releases like asyncpg: runs to its own timeout, then drops the connection."""

    def __init__(self) -> None:
        super().__init__()
        self.cut_short = False
        self.terminated_under_it = False

    async def release(self, conn: _Conn, *, timeout: float | None = None) -> None:
        self.released.append(timeout)
        try:
            await asyncio.sleep(timeout or 0)
        except asyncio.CancelledError:
            self.cut_short = True
            raise
        self.terminated_under_it = conn.terminated > 0
        conn.terminate()
        raise TimeoutError


async def test_asyncpg_gets_the_release_budget_and_is_not_cut_short_under_it() -> None:
    pool = _BudgetedPool()

    async def work(conn: _Conn) -> str:
        return "committed"

    result, elapsed = await _elapsed(
        run_on_connection(pool, work, acquire_timeout=1, release_budget=0.2)
    )

    assert result == "committed"
    assert pool.released == [0.2]
    assert not pool.cut_short
    assert not pool.terminated_under_it
    assert elapsed < 0.2 + pg_session.RELEASE_GRACE_S


async def test_a_cancel_during_the_release_leaves_it_bounded_in_the_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pg_session, "RELEASE_GRACE_S", 0.1)
    pool = _Pool(release_hangs=True)
    released = asyncio.Event()

    class _SignallingPool(_Pool):
        async def release(self, conn: _Conn, *, timeout: float | None = None) -> None:
            released.set()
            await pool.release(conn, timeout=timeout)

    wrapper = _SignallingPool()
    wrapper.conn = pool.conn

    async def work(conn: _Conn) -> str:
        return "committed"

    task = asyncio.create_task(
        run_on_connection(wrapper, work, acquire_timeout=1, release_budget=0.1)
    )
    await released.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert pool.conn.terminated == 0

    await asyncio.sleep(0.4)
    assert pool.conn.terminated == 1
    assert not pg_session._BACKGROUND


class _CancellableConn(_Conn):
    """A Connection with asyncpg's cancel request; ``cancel_hangs`` models a server
    that never answers it."""

    def __init__(self, *, cancel_hangs: bool = False) -> None:
        super().__init__()
        self.cancel_hangs = cancel_hangs

    def is_closed(self) -> bool:
        return self.terminated > 0

    def terminate(self) -> None:
        self.events.append("terminate")
        super().terminate()

    async def _cancel(self, waiter: asyncio.Future[None]) -> None:
        self.events.append("cancel")
        if self.cancel_hangs:
            await asyncio.sleep(3600)
        waiter.set_result(None)


@pytest.mark.parametrize(
    "fault",
    [asyncio.CancelledError, TimeoutError],
    ids=["cancelled", "statement-timeout"],
)
async def test_an_abandoned_statement_is_cancelled_before_the_connection_is_dropped(
    fault: type[BaseException],
) -> None:
    pool = _Pool()
    pool.conn = _CancellableConn()

    async def work(conn: _Conn) -> None:
        raise fault

    with pytest.raises(fault):
        await run_on_connection(pool, work, acquire_timeout=1)

    assert pool.conn.events[:2] == ["cancel", "terminate"]


async def test_a_cancel_request_nobody_answers_is_given_up_within_its_budget() -> None:
    pool = _Pool()
    pool.conn = _CancellableConn(cancel_hangs=True)
    entered = asyncio.Event()

    async def work(conn: _Conn) -> None:
        entered.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(run_on_connection(pool, work, acquire_timeout=1))
    await entered.wait()
    task.cancel()
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert loop.time() - start < pg_session.CANCEL_BUDGET_S + 0.5
    assert pool.conn.events[:2] == ["cancel", "terminate"]


async def test_a_query_error_is_not_an_abandoned_statement() -> None:
    pool = _Pool()
    pool.conn = _CancellableConn()

    async def work(conn: _Conn) -> None:
        raise LookupError("query fault")

    with pytest.raises(LookupError):
        await run_on_connection(pool, work, acquire_timeout=1)

    assert pool.conn.events == []


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


async def test_a_statement_timeout_in_a_transaction_does_not_wait_on_a_rollback() -> None:
    conn = _Conn()

    async def timed_out(c: _Conn) -> None:
        raise TimeoutError

    with pytest.raises(TimeoutError):
        await in_transaction(conn, timed_out)

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


def test_a_client_configuration_fault_is_not_unavailable() -> None:
    # An InterfaceError subclass, but a config fault: 500, as the boot refuses it.
    asyncpg = pytest.importorskip("asyncpg")
    assert not store_unavailable(asyncpg.exceptions.ClientConfigurationError("x"))
    assert store_unavailable(
        asyncpg.exceptions.InterfaceError(
            "cannot perform operation: another operation is in progress"
        )
    )


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


_SSL_REJECTED = ConnectionError('PostgreSQL server at "db:5432" rejected SSL upgrade')


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full", "REQUIRE"])
def test_a_server_declining_the_tls_the_dsn_demands_refuses_the_boot(mode: str) -> None:
    dsn = f"postgresql://gw:pw@db:5432/gw?sslmode={mode}"
    assert boot_probe_outcome(_SSL_REJECTED, dsn) == BOOT_REFUSE_TLS


@pytest.mark.parametrize(
    ("exc", "dsn"),
    [
        (_SSL_REJECTED, "postgresql://gw:pw@db:5432/gw"),
        (_SSL_REJECTED, "postgresql://gw:pw@db:5432/gw?sslmode=prefer"),
        (_SSL_REJECTED, ""),
        (ConnectionRefusedError("connect call failed"), "postgresql://db/gw?sslmode=require"),
        (ConnectionResetError(), "postgresql://db/gw?sslmode=verify-full"),
    ],
    ids=["no-sslmode", "prefer", "no-dsn", "refused-require", "reset-verify-full"],
)
def test_a_connection_failure_that_can_heal_still_warns(exc: BaseException, dsn: str) -> None:
    assert boot_probe_outcome(exc, dsn) == BOOT_WARN


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


# ── the startup-parameter rejection (PgBouncer) ──────────────────────────────

_PGBOUNCER_DSN = "postgresql://gw:pgb-password-3a9e@pgbouncer:6432/gw"


def _connect_raising(exc: BaseException) -> tuple[list[dict[str, object]], object]:
    calls: list[dict[str, object]] = []

    async def connect(dsn: str, **kwargs: object) -> object:
        calls.append({"dsn": dsn, **kwargs})
        raise exc

    return calls, connect


async def test_the_probe_connects_with_the_pools_startup_parameters() -> None:
    calls: list[dict[str, object]] = []

    async def connect(dsn: str, **kwargs: object) -> str:
        calls.append({"dsn": dsn, **kwargs})
        return "conn"

    assert await connect_with_keepalives(connect, _PGBOUNCER_DSN, timeout=3.0) == "conn"
    assert calls == [
        {"dsn": _PGBOUNCER_DSN, "timeout": 3.0, "server_settings": KEEPALIVE_SERVER_SETTINGS}
    ]


async def test_a_protocol_violation_at_connect_is_the_startup_parameter_rejection() -> None:
    asyncpg = pytest.importorskip("asyncpg")
    rejected = asyncpg.ProtocolViolationError(
        f"unsupported startup parameter: tcp_keepalives_idle ({_PGBOUNCER_DSN})"
    )
    _, connect = _connect_raising(rejected)

    with pytest.raises(StartupParameterRejectedError) as caught:
        await connect_with_keepalives(connect, _PGBOUNCER_DSN, timeout=1.0)

    assert str(caught.value) == STARTUP_PARAMETER_REJECTED
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert "pgb-password-3a9e" not in str(caught.value)
    assert boot_probe_outcome(caught.value, _PGBOUNCER_DSN) == BOOT_REFUSE_STARTUP_PARAMETER


def test_the_rejection_names_every_startup_parameter_to_ignore() -> None:
    assert STARTUP_PARAMETER_REJECTED == (
        "Postgres/PgBouncer rejected a startup parameter; add tcp_keepalives_idle,"
        "tcp_keepalives_interval,tcp_keepalives_count to ignore_startup_parameters"
    )
    assert all(name in STARTUP_PARAMETER_REJECTED for name in KEEPALIVE_SERVER_SETTINGS)


def test_a_protocol_violation_mid_query_still_warns_and_boots() -> None:
    asyncpg = pytest.importorskip("asyncpg")
    assert boot_probe_outcome(asyncpg.ProtocolViolationError("x")) == BOOT_WARN


@pytest.mark.parametrize(
    "exc",
    [ConnectionRefusedError("refused"), RuntimeError("x")],
    ids=["refused", "unexpected"],
)
async def test_any_other_connect_failure_passes_through_unchanged(exc: BaseException) -> None:
    _, connect = _connect_raising(exc)

    with pytest.raises(type(exc)) as caught:
        await connect_with_keepalives(connect, _PGBOUNCER_DSN, timeout=1.0)

    assert caught.value is exc


async def test_other_asyncpg_connect_failures_pass_through_unchanged() -> None:
    asyncpg = pytest.importorskip("asyncpg")
    exc = asyncpg.InvalidPasswordError("x")
    _, connect = _connect_raising(exc)

    with pytest.raises(asyncpg.InvalidPasswordError) as caught:
        await connect_with_keepalives(connect, _PGBOUNCER_DSN, timeout=1.0)

    assert caught.value is exc
