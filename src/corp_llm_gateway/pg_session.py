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

# How long returning a connection to the pool may take before it is dropped.
RELEASE_BUDGET_S = 2.0

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
    """``work(conn)`` on a pooled connection, unwound within ``release_budget`` of a
    cancellation however the server behaves.

    ``async with pool.acquire()`` is not bounded that way: after a cancelled query,
    ROLLBACK and the pool's reset wait on asyncpg's cancel request, which opens a new
    connection and waits on it with no timeout. So a cancelled ``work`` terminates
    the connection instead (the server rolls back, the pool opens a new one), and a
    release past its budget does the same.
    """
    conn = await pool.acquire(timeout=acquire_timeout)
    try:
        return await work(conn)
    except BaseException as exc:
        if not isinstance(exc, Exception):
            _terminate(conn)
        raise
    finally:
        await _release(pool, conn, release_budget)


async def in_transaction[T](conn: Any, work: Callable[[Any], Awaitable[T]], **options: Any) -> T:
    """``work(conn)`` in a transaction that commits on success and rolls back on an
    error. On a cancellation it does neither: that ROLLBACK is the unbounded wait,
    and the caller's :func:`run_on_connection` terminates the connection instead."""
    tx = conn.transaction(**options)
    await tx.start()
    try:
        result = await work(conn)
    except Exception:
        await tx.rollback()
        raise
    await tx.commit()
    return result


async def _release(pool: Any, conn: Any, budget: float) -> None:
    try:
        await asyncio.wait_for(pool.release(conn, timeout=budget), budget)
    except asyncio.CancelledError:
        _terminate(conn)
        raise
    except Exception:
        # Not reset in time, or not at all: drop it; the work's outcome stands.
        _terminate(conn)


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


def boot_probe_outcome(exc: BaseException) -> str:
    """The :data:`BOOT_PROBE_OUTCOMES` entry ``exc`` matches; refuse when none does."""
    for qualified, outcome in BOOT_PROBE_OUTCOMES:
        module, _, name = qualified.rpartition(".")
        cls = getattr(sys.modules.get(module), name, None)
        if isinstance(cls, type) and isinstance(exc, cls):
            return outcome
    return BOOT_REFUSE
