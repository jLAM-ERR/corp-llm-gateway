"""Readiness reaches Postgres the way the stores do: through the shared token store's
pool once it is built, else one connection asking for the pools' startup parameters.
A PgBouncer that rejects those parameters must turn the pod unready, not leave it
Ready while every store call fails."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import httpx
import pytest

from corp_llm_gateway import bootstrap, config, pg_session
from tests.postgres_support import RejectingPgBouncer, pg_dsn, require_asyncpg, skip_or_fail

PASSWORD = "pgb-ready-5c7e"


@pytest.fixture(autouse=True)
def _fresh_stores(hermetic_gateway_config: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(bootstrap, "_token_store", None)
    monkeypatch.setattr(bootstrap, "_team_config_store", None)
    yield


@pytest.fixture
def bouncer() -> Iterator[RejectingPgBouncer]:
    require_asyncpg()
    server = RejectingPgBouncer()
    try:
        yield server
    finally:
        server.close()


async def _ready() -> httpx.Response:
    router = bootstrap.build_health_router()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router), base_url="http://gw"
    ) as client:
        return await client.get("/healthz/ready")


async def _close_store() -> None:
    store = bootstrap._token_store
    if store is not None:
        await store.close()


@pytest.mark.parametrize("store_built", [False, True], ids=["no-store-yet", "shared-store"])
async def test_readiness_fails_behind_a_pgbouncer_that_rejects_the_keepalive_parameters(
    store_built: bool,
    bouncer: RejectingPgBouncer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    dsn = f"postgresql://gateway:{PASSWORD}@127.0.0.1:{bouncer.port}/gateway"
    monkeypatch.setenv("CORP_LLM_PG_DSN", dsn)
    config.reset_cache()
    if store_built:
        bootstrap.get_token_store()

    try:
        resp = await _ready()
    finally:
        await _close_store()

    assert resp.status_code == 503
    assert resp.json()["detail"] == "postgres_error:StartupParameterRejectedError"
    assert bouncer.startups and b"tcp_keepalives_idle" in bouncer.startups[0]
    out = capsys.readouterr()
    for surface in (resp.text, caplog.text, out.out, out.err):
        assert PASSWORD not in surface
        assert f"127.0.0.1:{bouncer.port}" not in surface


async def _require_postgres(dsn: str) -> None:
    import asyncpg

    try:
        conn = await asyncpg.connect(dsn, timeout=5.0)
    except Exception as exc:
        skip_or_fail(f"Postgres unreachable: {type(exc).__name__}")
    await conn.close()


@pytest.mark.parametrize("store_built", [False, True], ids=["no-store-yet", "shared-store"])
async def test_readiness_is_200_on_a_reachable_postgres(
    store_built: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_asyncpg()
    dsn = pg_dsn()
    await _require_postgres(dsn)
    monkeypatch.setenv("CORP_LLM_PG_DSN", dsn)
    config.reset_cache()
    store = bootstrap.get_token_store() if store_built else None

    try:
        resp = await _ready()
        pool = getattr(store, "_pool", None)
    finally:
        await _close_store()

    assert resp.status_code == 200, resp.text
    if store_built:
        # Probed through the store's own pool, not a side connection.
        assert pool is not None
    else:
        assert bootstrap._token_store is None


async def test_the_shared_store_probe_is_bounded_by_the_pool_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pool with every connection taken answers within the acquire bound, not never.
    require_asyncpg()
    dsn = pg_dsn()
    await _require_postgres(dsn)
    monkeypatch.setenv("CORP_LLM_PG_DSN", dsn)
    config.reset_cache()
    store = bootstrap.get_token_store()
    monkeypatch.setattr("corp_llm_gateway.tokens.postgres_store._ACQUIRE_TIMEOUT_S", 0.2)
    pool = await store._get_pool()  # type: ignore[attr-defined]
    held = [await pool.acquire() for _ in range(pool.get_max_size())]

    try:
        resp = await asyncio.wait_for(_ready(), timeout=5.0)
    finally:
        for conn in held:
            await pool.release(conn)
        await _close_store()

    assert resp.status_code == 503
    assert resp.json()["detail"] == "postgres_error:TimeoutError"


def test_the_probe_never_dials_a_bare_connect() -> None:
    # Every readiness connection carries the pools' startup parameters.
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(bootstrap))
    bare = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncpg"
    ]

    assert bare == []


# ── the token schema, when the boot could not check it ───────────────────────

ROOT = Path(__file__).resolve().parents[2]
TOKENS_SCHEMA = ROOT / "src/corp_llm_gateway/tokens/schema.sql"


@pytest.fixture
async def schema_dsn() -> AsyncIterator[tuple[str, Callable[[str], Awaitable[None]]]]:
    """A DSN whose search_path is a throwaway schema, and an executor inside it."""
    import secrets

    require_asyncpg()
    import asyncpg

    base = pg_dsn()
    await _require_postgres(base)
    schema = f"ready_{secrets.token_hex(4)}"

    async def execute(sql: str) -> None:
        conn = await asyncpg.connect(base, timeout=5.0)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    await execute(f"CREATE SCHEMA {schema}")

    async def in_schema(sql: str) -> None:
        await execute(f"SET search_path TO {schema}; {sql}")

    separator = "&" if "?" in base else "?"
    try:
        yield f"{base}{separator}search_path={schema}", in_schema
    finally:
        await execute(f"DROP SCHEMA {schema} CASCADE")


def _enable_issuance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, dsn: str) -> None:
    pytest.importorskip("cryptography")
    cfg = tmp_path / "issuance.toml"
    cfg.write_text('[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]\n"/devs" = "t1"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", "https://kc.corp.lan/realms/dev")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_AUDIENCE", "corp-gateway-issuance")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID", "corp-gateway-cli")
    monkeypatch.setenv("CORP_LLM_PG_DSN", dsn)
    config.reset_cache()


class _Clock:
    now = 5000.0

    def __call__(self) -> float:
        return self.now


async def test_readiness_waits_for_the_token_schema_then_turns_ready(
    schema_dsn: tuple, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from corp_llm_gateway.healthz.checks import ISSUANCE_SCHEMA_RECHECK_S

    dsn, in_schema = schema_dsn
    _enable_issuance(monkeypatch, tmp_path, dsn)
    router = bootstrap.build_health_router(issuance_schema_verified=False)
    clock = _Clock()
    router._issuance_schema._clock = clock  # type: ignore[union-attr]

    async def call(method: str, path: str) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router), base_url="http://gw"
        ) as client:
            return await client.request(
                method, path, headers={"Authorization": "Bearer eyJ.not-a-jwt.sig"}
            )

    try:
        missing = await call("GET", "/healthz/ready")
        refused = await call("POST", "/internal/issue-token")
        await in_schema(TOKENS_SCHEMA.read_text())
        cached = await call("GET", "/healthz/ready")
        clock.now += ISSUANCE_SCHEMA_RECHECK_S
        ready = await call("GET", "/healthz/ready")
        past_the_gate = await call("POST", "/internal/issue-token")
    finally:
        await router.aclose()
        await _close_store()

    assert missing.status_code == 503
    assert missing.json()["detail"] == f"issuance_schema: {pg_session.TOKEN_SCHEMA_MISSING}"
    assert (refused.status_code, refused.json()) == (503, {"error": "E_ISSUE_SCHEMA"})
    assert cached.status_code == 503
    assert ready.status_code == 200, ready.text
    # The route now verifies the bearer: the schema gate no longer answers.
    assert past_the_gate.status_code == 401


async def test_a_schema_verified_at_boot_is_not_queried_by_readiness(
    schema_dsn: tuple, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dsn, in_schema = schema_dsn
    await in_schema(TOKENS_SCHEMA.read_text())
    _enable_issuance(monkeypatch, tmp_path, dsn)
    calls = 0
    real = pg_session.token_schema_problem

    async def counting(conn: object, **kwargs: object) -> str | None:
        nonlocal calls
        calls += 1
        return await real(conn, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pg_session, "token_schema_problem", counting)
    router = bootstrap.build_health_router(issuance_schema_verified=True)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router), base_url="http://gw"
        ) as client:
            statuses = [(await client.get("/healthz/ready")).status_code for _ in range(3)]
    finally:
        await router.aclose()
        await _close_store()

    assert statuses == [200, 200, 200]
    assert calls == 0


async def test_the_readiness_schema_check_runs_on_the_shared_store(
    schema_dsn: tuple, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dsn, in_schema = schema_dsn
    await in_schema(TOKENS_SCHEMA.read_text())
    _enable_issuance(monkeypatch, tmp_path, dsn)
    router = bootstrap.build_health_router()

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=router), base_url="http://gw"
        ) as client:
            resp = await client.get("/healthz/ready")
        pool = bootstrap._token_store._pool  # type: ignore[union-attr]
    finally:
        await router.aclose()
        await _close_store()

    assert resp.status_code == 200, resp.text
    assert pool is not None
