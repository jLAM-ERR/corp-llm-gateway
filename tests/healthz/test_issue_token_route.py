"""`POST /internal/issue-token` on the HealthRouter: header-only bearer, bounded
body read, own in-flight and rate bounds, and the status map. Driven in process;
the real-socket cases live in test_issue_token_served.py."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import httpx
import pytest

from corp_llm_gateway.healthz import build_health_router
from corp_llm_gateway.healthz.checks import (
    ExtensionsCheck,
    HealthStatus,
    LiveCheck,
    ReadyCheck,
    SanitizationCheck,
)
from corp_llm_gateway.healthz.server import HealthRouter, Scope
from corp_llm_gateway.tokens import (
    InMemoryTokenStore,
    IssuancePolicyError,
    JwksUnavailableError,
    OidcClaims,
    OidcTeamMappingError,
    OidcVerificationError,
    TokenIssuer,
)

PATH = "/internal/issue-token"
BEARER = "eyJ.issuance-bearer-9f3c.sig"


async def _ok() -> bool:
    return True


async def _ext() -> dict[str, HealthStatus]:
    return {}


Verifier = Callable[[str], Awaitable[OidcClaims]]


async def _accept(token: str) -> OidcClaims:
    return OidcClaims(user_id="alice", team_id="t1", scopes=("read",))


def _raising(exc: BaseException) -> Verifier:
    async def verify(token: str) -> OidcClaims:
        raise exc

    return verify


def _router(
    verifier: Verifier | None = _accept,
    *,
    store: InMemoryTokenStore | None = None,
    fallthrough: object | None = None,
    **bounds: object,
) -> HealthRouter:
    issuer = (
        TokenIssuer(store if store is not None else InMemoryTokenStore(), verifier)
        if verifier is not None
        else None
    )
    return build_health_router(
        live_check=LiveCheck(),
        ready_check=ReadyCheck(check_redis=_ok, check_postgres=_ok),
        sanitization_check=SanitizationCheck(run_round_trip=_ok),
        extensions_check=ExtensionsCheck(health_all=_ext),
        token_issuer=issuer,
        fallthrough=fallthrough,  # type: ignore[arg-type]
        **bounds,  # type: ignore[arg-type]
    )


def _client(router: HealthRouter) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=router), base_url="http://gw")


async def _post(router: HealthRouter, **kwargs: object) -> httpx.Response:
    headers = kwargs.pop("headers", {"Authorization": f"Bearer {BEARER}"})
    async with _client(router) as client:
        return await client.post(PATH, headers=headers, **kwargs)  # type: ignore[arg-type]


def _scope(headers: list[tuple[bytes, bytes]] | None = None) -> Scope:
    return {
        "type": "http",
        "method": "POST",
        "path": PATH,
        "raw_path": PATH.encode(),
        "headers": headers
        if headers is not None
        else [(b"authorization", f"Bearer {BEARER}".encode())],
    }


async def _drive(
    router: HealthRouter, receive: Callable[[], Awaitable[dict]], scope: Scope | None = None
) -> tuple[int | None, dict | None]:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await router(scope or _scope(), receive, send)
    starts = [m for m in sent if m["type"] == "http.response.start"]
    if not starts:
        return None, None
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return starts[0]["status"], json.loads(body)


# ── 200 ──────────────────────────────────────────────────────────────────────


async def test_a_bodiless_post_with_a_bearer_issues_a_stored_token() -> None:
    store = InMemoryTokenStore()

    resp = await _post(_router(store=store))

    assert resp.status_code == 200
    assert set(resp.json()) == {"corp_token", "expires_at"}
    assert await store.lookup(resp.json()["corp_token"]) is not None
    assert resp.headers["cache-control"] == "no-store"


async def test_the_bearer_scheme_is_case_insensitive() -> None:
    resp = await _post(_router(), headers={"Authorization": f"bearer {BEARER}"})

    assert resp.status_code == 200


async def test_the_verifier_receives_exactly_the_header_token() -> None:
    seen: list[str] = []

    async def verify(token: str) -> OidcClaims:
        seen.append(token)
        return await _accept(token)

    await _post(_router(verify))

    assert seen == [BEARER]


# ── 400 / 408: the body ──────────────────────────────────────────────────────


@pytest.mark.parametrize("content", [b"x", b'{"oidc_token": "x"}', b"\x00" * 1024])
async def test_any_body_byte_is_refused_400_before_verification(content: bytes) -> None:
    called: list[str] = []

    async def verify(token: str) -> OidcClaims:
        called.append(token)
        return await _accept(token)

    resp = await _post(_router(verify), content=content)

    assert resp.status_code == 400
    assert resp.json() == {"error": "E_ISSUE_BODY"}
    assert called == []


async def test_the_json_body_fallback_is_gone() -> None:
    resp = await _post(_router(), headers={}, json={"oidc_token": BEARER})

    assert resp.status_code == 400


async def test_an_endless_body_is_cut_off_at_the_cap() -> None:
    reads = 0

    async def receive() -> dict:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"a" * 512, "more_body": True}

    status, body = await _drive(_router(), receive)

    assert status == 400
    assert body == {"error": "E_ISSUE_BODY"}
    # 1 KiB cap: the third 512-byte chunk crosses it and reading stops there.
    assert reads == 3


async def test_a_stalled_body_is_refused_408_at_the_deadline() -> None:
    async def receive() -> dict:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    status, body = await asyncio.wait_for(
        _drive(_router(issue_body_timeout_s=0.05), receive), timeout=2
    )

    assert status == 408
    assert body == {"error": "E_ISSUE_BODY_TIMEOUT"}


async def test_a_trickling_body_is_bounded_by_the_total_deadline() -> None:
    async def receive() -> dict:
        await asyncio.sleep(0.02)
        return {"type": "http.request", "body": b"", "more_body": True}

    status, _ = await asyncio.wait_for(
        _drive(_router(issue_body_timeout_s=0.1), receive), timeout=2
    )

    assert status == 408


async def test_a_disconnect_while_reading_sends_nothing() -> None:
    async def receive() -> dict:
        return {"type": "http.disconnect"}

    status, _ = await _drive(_router(), receive)

    assert status is None


# ── 401: the bearer ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"authorization", b"Basic dXNlcjpwYXNz")],
        [(b"authorization", b"Bearer ")],
        [(b"authorization", b"Bearer a"), (b"authorization", b"Bearer b")],
    ],
    ids=["absent", "basic", "empty", "duplicated"],
)
async def test_a_missing_or_ambiguous_bearer_is_401(headers: list[tuple[bytes, bytes]]) -> None:
    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    status, body = await _drive(_router(), receive, _scope(headers))

    assert status == 401
    assert body == {"error": "E_OIDC_MISSING"}


# ── the exception → status map ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (OidcVerificationError("E_OIDC_EXPIRED"), 401, "E_OIDC_EXPIRED"),
        (OidcVerificationError("bad oidc token"), 401, "E_OIDC_INVALID"),
        (OidcTeamMappingError("E_ISSUE_NO_TEAM"), 403, "E_ISSUE_NO_TEAM"),
        (IssuancePolicyError(IssuancePolicyError.RATE), 403, "E_ISSUE_RATE"),
        (IssuancePolicyError(IssuancePolicyError.REPLAY), 403, "E_ISSUE_REPLAY"),
        (IssuancePolicyError(IssuancePolicyError.BUSY), 503, "E_ISSUE_BUSY"),
        (JwksUnavailableError("E_JWKS_UNAVAILABLE"), 503, "E_JWKS_UNAVAILABLE"),
        (RuntimeError("corp_tokens unique violation on issuance"), 500, "E_ISSUE_INTERNAL"),
        (ValueError(f"leaky detail {BEARER}"), 500, "E_ISSUE_INTERNAL"),
    ],
)
async def test_each_failure_maps_to_its_status_and_a_code_only_body(
    exc: Exception, status: int, code: str
) -> None:
    resp = await _post(_router(_raising(exc)))

    assert resp.status_code == status
    assert resp.json() == {"error": code}


async def test_an_internal_failure_logs_the_class_name_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        resp = await _post(_router(_raising(RuntimeError(f"collision on {BEARER}"))))

    assert resp.status_code == 500
    assert "RuntimeError" in caplog.text
    assert BEARER not in caplog.text
    assert "collision" not in caplog.text
    assert "Traceback" not in caplog.text


async def test_every_outcome_logs_its_status_and_code_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="corp_llm_gateway.healthz"):
        await _post(_router())
        await _post(_router(_raising(OidcVerificationError("E_OIDC_AUDIENCE"))))

    lines = [
        r.getMessage() for r in caplog.records if r.name.startswith("corp_llm_gateway.healthz")
    ]
    assert lines == [
        "issue_token status=200 code=ok",
        "issue_token status=401 code=E_OIDC_AUDIENCE",
    ]


# ── 429: the route's own bounds ──────────────────────────────────────────────


async def test_requests_over_the_inflight_bound_get_429_without_queueing() -> None:
    entered = asyncio.Semaphore(0)
    release = asyncio.Event()

    async def verify(token: str) -> OidcClaims:
        entered.release()
        await release.wait()
        return await _accept(token)

    router = _router(verify, issue_max_inflight=2)
    async with _client(router) as client:
        held = [
            asyncio.create_task(
                client.post(PATH, headers={"Authorization": f"Bearer {BEARER}-{i}"})
            )
            for i in range(2)
        ]
        for _ in held:
            await asyncio.wait_for(entered.acquire(), timeout=2)
        over = await asyncio.wait_for(
            client.post(PATH, headers={"Authorization": f"Bearer {BEARER}"}), timeout=1
        )
        release.set()
        done = await asyncio.gather(*held)
        after = await client.post(PATH, headers={"Authorization": f"Bearer {BEARER}"})

    assert over.status_code == 429
    assert over.json() == {"error": "E_ISSUE_INFLIGHT"}
    assert [r.status_code for r in done] == [200, 200]
    # The slots are released on the way out.
    assert after.status_code == 200


async def test_a_failing_request_releases_its_inflight_slot() -> None:
    router = _router(_raising(RuntimeError("boom")), issue_max_inflight=1)

    first = await _post(router)
    second = await _post(router)

    assert (first.status_code, second.status_code) == (500, 500)


async def test_the_token_bucket_throttles_and_refills() -> None:
    now = [1000.0]
    router = _router(issue_rate_per_minute=2, issue_clock=lambda: now[0])

    statuses = [(await _post(router)).status_code for _ in range(3)]
    throttled = await _post(router)
    now[0] += 30.0  # one token back at 2/minute
    refilled = await _post(router)

    assert statuses == [200, 200, 429]
    assert throttled.json() == {"error": "E_ISSUE_THROTTLED"}
    assert refilled.status_code == 200


@pytest.mark.parametrize(
    "bounds",
    [
        {"issue_max_inflight": 0},
        {"issue_rate_per_minute": 0},
        {"issue_max_inflight": -1},
    ],
)
def test_non_positive_bounds_are_refused(bounds: dict) -> None:
    with pytest.raises(ValueError):
        _router(**bounds)


# ── 404 / 405: disabled is terminal, configured is POST-only ─────────────────


@pytest.mark.parametrize("method", ["POST", "GET", "PUT", "DELETE", "HEAD"])
async def test_without_an_issuer_the_path_is_404_locally_never_fallen_through(
    method: str,
) -> None:
    reached: list[str] = []

    async def fallthrough(scope: Scope, receive: object, send: object) -> None:
        reached.append(scope["path"])
        raise AssertionError("issuance fell through to the downstream app")

    router = _router(None, fallthrough=fallthrough)
    async with _client(router) as client:
        resp = await client.request(method, PATH, headers={"Authorization": f"Bearer {BEARER}"})

    assert resp.status_code == 404
    if method != "HEAD":
        assert resp.json() == {"error": "E_ISSUE_DISABLED"}
    assert reached == []


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
async def test_a_configured_issuer_answers_405_to_other_methods(method: str) -> None:
    async with _client(_router()) as client:
        resp = await client.request(method, PATH)

    assert resp.status_code == 405
    assert resp.json() == {"error": "E_METHOD_NOT_ALLOWED"}
