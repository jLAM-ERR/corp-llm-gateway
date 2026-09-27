"""Readiness and the issuance route when the boot could not prove the token schema
current (Postgres was unreachable then): readiness re-checks it, rate-limited, and
the route refuses 503 `E_ISSUE_SCHEMA` from the cached result until it passes."""

from __future__ import annotations

import httpx
import pytest

from corp_llm_gateway import pg_session
from corp_llm_gateway.healthz import build_health_router
from corp_llm_gateway.healthz.checks import (
    ISSUANCE_SCHEMA_RECHECK_S,
    ExtensionsCheck,
    HealthStatus,
    IssuanceSchemaGate,
    LiveCheck,
    ReadyCheck,
    SanitizationCheck,
)
from corp_llm_gateway.healthz.server import HealthRouter
from corp_llm_gateway.tokens import InMemoryTokenStore, OidcClaims, TokenIssuer

PATH = "/internal/issue-token"
DSN_SECRET = "pg-secret-6e1d"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _SchemaCheck:
    """Answers each call with the next outcome; the last one repeats."""

    def __init__(self, *outcomes: str | BaseException | None) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self) -> str | None:
        self.calls += 1
        outcome = self._outcomes[min(self.calls, len(self._outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def _ok() -> bool:
    return True


async def _ext() -> dict[str, HealthStatus]:
    return {}


async def _accept(token: str) -> OidcClaims:
    return OidcClaims(user_id="alice", team_id="t1", scopes=("read",))


def _router(gate: IssuanceSchemaGate, *, check_postgres=_ok) -> HealthRouter:  # type: ignore[no-untyped-def]
    return build_health_router(
        live_check=LiveCheck(),
        ready_check=ReadyCheck(
            check_redis=_ok, check_postgres=check_postgres, check_issuance_schema=gate.problem
        ),
        sanitization_check=SanitizationCheck(run_round_trip=_ok),
        extensions_check=ExtensionsCheck(health_all=_ext),
        token_issuer=TokenIssuer(InMemoryTokenStore(), _accept),
        issuance_schema=gate,
    )


async def _ready(router: HealthRouter) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router), base_url="http://gw"
    ) as client:
        return await client.get("/healthz/ready")


async def _issue(router: HealthRouter) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router), base_url="http://gw"
    ) as client:
        return await client.post(PATH, headers={"Authorization": "Bearer eyJ.gate.sig"})


async def test_a_schema_verified_at_boot_is_never_checked_again() -> None:
    check = _SchemaCheck(pg_session.TOKEN_SCHEMA_MISSING)
    router = _router(IssuanceSchemaGate(check, verified=True))

    for _ in range(3):
        assert (await _ready(router)).status_code == 200
    assert (await _issue(router)).status_code == 200
    assert check.calls == 0


async def test_an_unverified_schema_keeps_the_pod_unready_and_refuses_issuance() -> None:
    check = _SchemaCheck(pg_session.TOKEN_SCHEMA_MISSING)
    router = _router(IssuanceSchemaGate(check, verified=False, clock=_Clock()))

    ready = await _ready(router)
    issue = await _issue(router)

    assert ready.status_code == 503
    assert ready.json()["detail"] == f"issuance_schema: {pg_session.TOKEN_SCHEMA_MISSING}"
    assert "tokens/schema.sql" in ready.json()["detail"]
    assert (issue.status_code, issue.json()) == (503, {"error": "E_ISSUE_SCHEMA"})
    assert issue.headers["cache-control"] == "no-store"
    # The route reads the cached result; only readiness runs the query.
    assert check.calls == 1


async def test_the_route_never_runs_the_schema_query() -> None:
    check = _SchemaCheck(pg_session.TOKEN_SCHEMA_MISSING)
    router = _router(IssuanceSchemaGate(check, verified=False, clock=_Clock()))

    for _ in range(5):
        assert (await _issue(router)).json() == {"error": "E_ISSUE_SCHEMA"}
    assert check.calls == 0


async def test_an_invalid_jti_index_names_its_remedy() -> None:
    check = _SchemaCheck(pg_session.JTI_INDEX_INVALID)
    router = _router(IssuanceSchemaGate(check, verified=False, clock=_Clock()))

    detail = (await _ready(router)).json()["detail"]

    assert detail == f"issuance_schema: {pg_session.JTI_INDEX_INVALID}"
    assert "DROP INDEX corp_tokens_oidc_jti_key" in detail


async def test_a_failing_check_is_rerun_at_most_once_per_interval_then_cached_for_good() -> None:
    clock = _Clock()
    check = _SchemaCheck(pg_session.TOKEN_SCHEMA_MISSING, None)
    router = _router(IssuanceSchemaGate(check, verified=False, clock=clock))

    for _ in range(3):
        assert (await _ready(router)).status_code == 503
    assert check.calls == 1

    clock.now += ISSUANCE_SCHEMA_RECHECK_S - 0.1
    assert (await _ready(router)).status_code == 503
    assert (await _issue(router)).json() == {"error": "E_ISSUE_SCHEMA"}
    assert check.calls == 1

    clock.now += 0.1
    assert (await _ready(router)).status_code == 200
    assert check.calls == 2
    assert (await _issue(router)).status_code == 200

    clock.now += 10 * ISSUANCE_SCHEMA_RECHECK_S
    assert (await _ready(router)).status_code == 200
    assert check.calls == 2


def test_the_recheck_interval_is_fifteen_seconds() -> None:
    assert ISSUANCE_SCHEMA_RECHECK_S == 15.0


async def test_a_check_that_raises_reports_the_class_only_and_is_rate_limited() -> None:
    clock = _Clock()
    check = _SchemaCheck(ConnectionResetError(f"postgresql://gw:{DSN_SECRET}@pg:5432/gw"))
    router = _router(IssuanceSchemaGate(check, verified=False, clock=clock))

    first = await _ready(router)
    second = await _ready(router)

    assert first.status_code == 503
    assert first.json()["detail"] == "issuance_schema_error:ConnectionResetError"
    assert DSN_SECRET not in first.text
    assert second.json() == first.json()
    assert check.calls == 1
    assert (await _issue(router)).json() == {"error": "E_ISSUE_SCHEMA"}


async def test_the_schema_is_checked_only_once_postgres_answers() -> None:
    async def down() -> bool:
        raise ConnectionRefusedError

    check = _SchemaCheck(None)
    router = _router(IssuanceSchemaGate(check, verified=False, clock=_Clock()), check_postgres=down)

    ready = await _ready(router)

    assert ready.json()["detail"] == "postgres_error:ConnectionRefusedError"
    assert check.calls == 0


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT"])
async def test_a_non_post_method_is_still_405_while_the_schema_is_unverified(method: str) -> None:
    router = _router(IssuanceSchemaGate(_SchemaCheck(None), verified=False, clock=_Clock()))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router), base_url="http://gw"
    ) as client:
        resp = await client.request(method, PATH)

    assert resp.status_code == 405
