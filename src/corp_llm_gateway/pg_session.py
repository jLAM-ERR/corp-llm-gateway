"""Bounded use of an asyncpg pool, and what each Postgres failure means.

Shared by the Postgres stores, the issuance route, the auth hot path and the boot's
issuance schema check. asyncpg
is optional: nothing here imports it, and an asyncpg class is only consulted when
something already imported the driver — without it, none of its errors can be in
flight.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

# How long returning a connection to the pool may take before it is dropped. asyncpg
# gets this budget; we wait RELEASE_GRACE_S longer and drop the connection only when
# its own release is still running then.
RELEASE_BUDGET_S = 2.0
RELEASE_GRACE_S = 1.0
# How long a cancel request for an abandoned statement may take before the
# connection is terminated regardless.
CANCEL_BUDGET_S = 0.5

# Server-side TCP keepalives for every pooled connection. A client lost behind a
# partition leaves its backend, and the subject lock its transaction holds, on the
# server; with these the server drops it in ~25 s rather than the OS default (2 h+).
KEEPALIVE_SERVER_SETTINGS = {
    "tcp_keepalives_idle": "10",
    "tcp_keepalives_interval": "5",
    "tcp_keepalives_count": "3",
}


async def run_on_connection[T](
    pool: Any,
    work: Callable[[Any], Awaitable[T]],
    *,
    acquire_timeout: float,
    release_budget: float = RELEASE_BUDGET_S,
) -> T:
    """``work(conn)`` on a pooled connection, unwound within :data:`CANCEL_BUDGET_S`
    of a cancellation however the server behaves.

    ``async with pool.acquire()`` is not bounded that way: after a cancelled query,
    ROLLBACK and the pool's reset wait on asyncpg's cancel request, which opens a new
    connection and waits on it with no timeout. So when ``work`` is cancelled, or a
    statement outlives its client-side ``timeout=``, the connection is abandoned: a
    cancel request gets :data:`CANCEL_BUDGET_S`, then the connection is terminated
    and the pool opens a new one.

    The terminate does not roll back at once. A backend notices the closed socket
    only when its statement ends, so its transaction and every lock it holds live
    until then: at once when the cancel request lands; otherwise until the
    statement's own bound (issuance: lock_timeout 5 s, statement_timeout 8 s). If
    the close never reaches the server (a partition), the idle backend lives until
    the server keepalives drop it (~25 s, :data:`KEEPALIVE_SERVER_SETTINGS`).
    """
    conn = await pool.acquire(timeout=acquire_timeout)
    try:
        return await work(conn)
    except BaseException as exc:
        if isinstance(exc, (asyncio.CancelledError, TimeoutError)):
            try:
                await _cancel_statement(conn)
            finally:
                _terminate(conn)
        elif not isinstance(exc, Exception):
            _terminate(conn)
        raise
    finally:
        await _release(pool, conn, release_budget)


async def in_transaction[T](conn: Any, work: Callable[[Any], Awaitable[T]], **options: Any) -> T:
    """``work(conn)`` in a transaction that commits on success and rolls back on an
    error. On a cancellation or a client-side statement timeout it does neither: that
    ROLLBACK is the unbounded wait, and the caller's :func:`run_on_connection`
    abandons the connection instead."""
    tx = conn.transaction(**options)
    await tx.start()
    try:
        result = await work(conn)
    except TimeoutError:
        # A client-side statement timeout: ROLLBACK would wait on the cancel.
        raise
    except Exception:
        await tx.rollback()
        raise
    await tx.commit()
    return result


# Releases left running when their caller was cancelled; held until they end.
_BACKGROUND: set[asyncio.Task[None]] = set()


async def _cancel_statement(conn: Any) -> None:
    """Ask the server to cancel ``conn``'s running statement, within CANCEL_BUDGET_S.

    Uses asyncpg's ``Connection._cancel`` (private: no public call sends a cancel
    request and returns); a driver without it is skipped.
    """
    con = getattr(conn, "_con", None) or conn  # a pool proxy wraps the Connection
    cancel = getattr(con, "_cancel", None)
    if cancel is None or con.is_closed():
        return
    waiter = asyncio.get_running_loop().create_future()
    waiter.add_done_callback(_retrieve)
    task = asyncio.ensure_future(cancel(waiter))
    try:
        await asyncio.wait((task,), timeout=CANCEL_BUDGET_S)
    finally:
        if not task.done():
            task.cancel()


async def _release(pool: Any, conn: Any, budget: float) -> None:
    # Our own task, never cancelled: asyncpg's release is shielded, and a cancelled
    # waiter would hand its failure to the loop's exception handler.
    release = asyncio.ensure_future(pool.release(conn, timeout=budget))
    try:
        await asyncio.wait((release,), timeout=budget + RELEASE_GRACE_S)
    except asyncio.CancelledError:
        _finish_in_background(release, conn, budget + RELEASE_GRACE_S)
        raise
    _settle(release, conn)


def _settle(release: asyncio.Future[Any], conn: Any) -> None:
    if not release.done():
        # asyncpg's own budget did not end it: drop the connection under it.
        release.add_done_callback(_retrieve)
        _terminate(conn)
    elif release.cancelled() or release.exception() is not None:
        # Not reset at all: drop it; the work's outcome stands.
        _terminate(conn)


def _finish_in_background(release: asyncio.Future[Any], conn: Any, bound: float) -> None:
    async def finish() -> None:
        await asyncio.wait((release,), timeout=bound)
        _settle(release, conn)

    task = asyncio.get_running_loop().create_task(finish())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _retrieve(fut: asyncio.Future[Any]) -> None:
    if not fut.cancelled():
        fut.exception()


def _terminate(conn: Any) -> None:
    # Already released or closed: nothing left to drop.
    with contextlib.suppress(Exception):
        conn.terminate()


def store_unavailable(exc: BaseException) -> bool:
    """A failure to reach or keep a store connection, as opposed to a query fault."""
    if isinstance(exc, OSError):  # refused, reset, TLS, TimeoutError (pool acquire), …
        return True
    asyncpg = sys.modules.get("asyncpg")
    if asyncpg is None:
        return False
    if isinstance(exc, asyncpg.QueryCanceledError):  # 57014: a statement bound fired
        return False
    if isinstance(exc, asyncpg.ClientConfigurationError):  # an InterfaceError, but a config fault
        return False
    return isinstance(
        exc,
        (
            asyncpg.PostgresConnectionError,  # 08xxx
            asyncpg.OperatorInterventionError,  # 57P0x: shutdown, crash, starting up
            asyncpg.TooManyConnectionsError,  # 53300
            asyncpg.InterfaceError,
        ),
    )


BOOT_WARN = "warn"
BOOT_REFUSE = "refuse"
BOOT_REFUSE_TLS = "refuse-tls"
BOOT_REFUSE_PRIVILEGE = "refuse-privilege"

BOOT_PROBE_OUTCOMES: tuple[tuple[str, str], ...] = (
    ("ssl.SSLError", BOOT_REFUSE_TLS),  # an OSError; incl. SSLCertVerificationError
    ("asyncpg.QueryCanceledError", BOOT_REFUSE),  # 57014, an OperatorInterventionError
    ("asyncpg.InsufficientPrivilegeError", BOOT_REFUSE_PRIVILEGE),  # 42501
    ("asyncpg.PostgresConnectionError", BOOT_WARN),  # 08xxx: dropped, failed, rejected
    ("asyncpg.OperatorInterventionError", BOOT_WARN),  # 57P0x: shutdown, crash, starting up
    ("asyncpg.TooManyConnectionsError", BOOT_WARN),  # 53300
    ("builtins.OSError", BOOT_WARN),  # refused, reset, unreachable, TimeoutError
)
"""What the boot's issuance schema probe does with each failure; first match wins,
so a subclass is listed ahead of its base. WARN boots and leaves the fault to
readiness: the database, or the proxy in front of it, may come back without a
config change. Anything unlisted refuses (exit 78): credentials, database name,
DSN syntax and the like do not heal on a restart."""


_STRICT_SSLMODES = frozenset({"require", "verify-ca", "verify-full"})


def boot_probe_outcome(exc: BaseException, dsn: str = "") -> str:
    """The :data:`BOOT_PROBE_OUTCOMES` entry ``exc`` matches; refuse when none does.

    A DSN that demands TLS from a server that declines it fails with a plain
    ``ConnectionError`` (an OSError), which would otherwise warn and boot; it never
    heals, so it refuses as a TLS failure.
    """
    if _requires_tls(dsn) and isinstance(exc, ConnectionError) and "ssl" in str(exc).lower():
        return BOOT_REFUSE_TLS
    for qualified, outcome in BOOT_PROBE_OUTCOMES:
        module, _, name = qualified.rpartition(".")
        cls = getattr(sys.modules.get(module), name, None)
        if isinstance(cls, type) and isinstance(exc, cls):
            return outcome
    return BOOT_REFUSE


def _requires_tls(dsn: str) -> bool:
    try:
        query = parse_qs(urlsplit(dsn).query)
    except ValueError:
        return False
    return any(mode.lower() in _STRICT_SSLMODES for mode in query.get("sslmode", ()))
