"""Shared Postgres plumbing for the store contract tests.

Locally a missing asyncpg or an unreachable Postgres skips. On CI (``CI=true``,
set by GitHub Actions) the test job runs a ``postgres`` service, so the same
fault FAILS: the issuance race tests must never go green by skipping.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import socket
import struct
import threading
from collections.abc import AsyncIterator
from typing import Any, NoReturn

import pytest

from corp_llm_gateway.settings import parse_flag

PG_DSN_ENV_VAR = "CORP_TEST_PG_DSN"
DEMO_PG_DSN = "postgresql://gateway:gateway@localhost:5432/gateway"


def pg_dsn() -> str:
    return os.environ.get(PG_DSN_ENV_VAR) or DEMO_PG_DSN


def postgres_is_required() -> bool:
    return parse_flag(os.environ.get("CI"))


def skip_or_fail(reason: str) -> NoReturn:
    """Skip on a machine without Postgres, fail on CI where it must run."""
    if postgres_is_required():
        pytest.fail(f"CI is set but the Postgres store tests cannot run: {reason}")
    pytest.skip(reason)


def require_asyncpg() -> None:
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        skip_or_fail("asyncpg not installed")


@contextlib.asynccontextmanager
async def scratch_schema_connection() -> AsyncIterator[tuple[Any, str]]:
    """(connection, schema): an asyncpg connection whose search_path is a fresh
    schema on the test Postgres, dropped afterwards."""
    require_asyncpg()
    import asyncpg

    schema = f"scratch_{secrets.token_hex(6)}"
    try:
        admin = await asyncpg.connect(pg_dsn(), timeout=5.0)
    except Exception as exc:
        skip_or_fail(f"Postgres unreachable: {exc}")
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        conn = await asyncpg.connect(pg_dsn(), timeout=5.0, server_settings={"search_path": schema})
        try:
            yield conn, schema
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()


async def schema_tables(conn: Any, schema: str) -> set[str]:
    rows = await conn.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = $1", schema
    )
    return {r["table_name"] for r in rows}


_SSL_REQUEST = 80877103


class RejectingPgBouncer:
    """Answers every startup message the way PgBouncer answers one carrying a
    parameter it does not know: ErrorResponse 08P01, then close."""

    def __init__(self) -> None:
        self.startups: list[bytes] = []
        self._sock = socket.create_server(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                self._answer(conn)

    def _answer(self, conn: socket.socket) -> None:
        def read_message() -> bytes:
            (length,) = struct.unpack("!I", _read_exactly(conn, 4))
            return _read_exactly(conn, length - 4)

        try:
            message = read_message()
            if struct.unpack("!I", message[:4])[0] == _SSL_REQUEST:
                conn.sendall(b"N")
                message = read_message()
        except (OSError, EOFError):
            return
        self.startups.append(message)
        fields = b"SFATAL\0VFATAL\0C08P01\0Munsupported startup parameter: tcp_keepalives_idle\0\0"
        conn.sendall(b"E" + struct.pack("!I", len(fields) + 4) + fields)

    def close(self) -> None:
        self._sock.close()


def _read_exactly(conn: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data
