"""Shared Postgres plumbing for the store contract tests.

Locally a missing asyncpg or an unreachable Postgres skips. On CI (``CI=true``,
set by GitHub Actions) the test job runs a ``postgres`` service, so the same
fault FAILS: the issuance race tests must never go green by skipping.
"""

from __future__ import annotations

import os
from typing import NoReturn

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
