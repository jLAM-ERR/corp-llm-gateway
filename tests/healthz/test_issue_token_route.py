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
from corp_llm_gateway.settings import IssuanceSettings
from corp_llm_gateway.tokens import (
    InMemoryTokenStore,
    IssuancePolicy,
    IssuancePolicyError,
    JwksUnavailableError,
    OidcClaims,
    OidcTeamMappingError,
    OidcVerificationError,
    TokenIssuer,
)
from corp_llm_gateway.tokens.models import TokenInfo

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


# ── the Authorization header, byte for byte ──────────────────────────────────


async def _empty_body() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _recording() -> tuple[list[str], Verifier]:
    seen: list[str] = []

    async def verify(token: str) -> OidcClaims:
        seen.append(token)
        return await _accept(token)

    return seen, verify


@pytest.mark.parametrize(
    "value",
    [b"Bearer tok-1   ", b"BEARER tok-1", b"bEaReR tok-1", b"Bearer    tok-1", b"Bearer tok-1\t"],
    ids=["trailing-spaces", "upper", "mixed-case", "many-spaces", "trailing-tab"],
)
async def test_bearer_whitespace_around_the_token_is_trimmed(value: bytes) -> None:
    seen, verify = _recording()

    status, body = await _drive(_router(verify), _empty_body, _scope([(b"authorization", value)]))

    assert status == 200, body
    assert seen == ["tok-1"]


@pytest.mark.parametrize(
    "value",
    [b"Bearer\ttok-1", b"Bearer", b"Bearer    ", b"Bearertok-1", b" Bearer tok-1", b"Token tok-1"],
    ids=["tab-separator", "no-token", "spaces-only", "no-separator", "leading-space", "scheme"],
)
async def test_a_malformed_bearer_is_401_and_never_verified(value: bytes) -> None:
    seen, verify = _recording()

    status, body = await _drive(_router(verify), _empty_body, _scope([(b"authorization", value)]))

    assert (status, body) == (401, {"error": "E_OIDC_MISSING"})
    assert seen == []


# ── the body read, chunk by chunk ────────────────────────────────────────────


async def test_empty_chunks_are_not_body_bytes() -> None:
    chunks = iter(
        [
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "more_body": True},
            {"type": "http.request", "body": b"", "more_body": False},
        ]
    )

    async def receive() -> dict:
        return next(chunks)

    status, _ = await _drive(_router(), receive)

    assert status == 200


async def test_a_single_late_body_byte_is_refused() -> None:
    chunks = iter(
        [
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": b"x", "more_body": False},
        ]
    )

    async def receive() -> dict:
        return next(chunks)

    seen, verify = _recording()
    status, body = await _drive(_router(verify), receive)

    assert (status, body) == (400, {"error": "E_ISSUE_BODY"})
    assert seen == []


async def test_a_second_chunk_arriving_after_the_deadline_is_408() -> None:
    calls = 0

    async def receive() -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": b"", "more_body": True}
        await asyncio.sleep(0.2)
        return {"type": "http.request", "body": b"", "more_body": False}

    seen, verify = _recording()
    status, body = await asyncio.wait_for(
        _drive(_router(verify, issue_body_timeout_s=0.05), receive), timeout=2
    )

    assert (status, body) == (408, {"error": "E_ISSUE_BODY_TIMEOUT"})
    assert seen == []


async def test_a_disconnect_mid_body_releases_the_inflight_slot() -> None:
    router = _router(issue_max_inflight=1)
    chunks = iter(
        [{"type": "http.request", "body": b"", "more_body": True}, {"type": "http.disconnect"}]
    )

    async def receive() -> dict:
        return next(chunks)

    status, _ = await _drive(router, receive)
    after = await _post(router)

    assert status is None
    assert after.status_code == 200


async def test_a_request_cancelled_mid_verification_releases_its_slot() -> None:
    entered = asyncio.Event()
    calls = 0

    async def verify(token: str) -> OidcClaims:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.sleep(3600)
        return await _accept(token)

    router = _router(verify, issue_max_inflight=1)
    stuck = asyncio.create_task(_drive(router, _empty_body))
    await asyncio.wait_for(entered.wait(), timeout=2)
    blocked = await _post(router)
    stuck.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stuck

    after = await _post(router)

    assert (blocked.status_code, blocked.json()) == (429, {"error": "E_ISSUE_INFLIGHT"})
    assert after.status_code == 200


# ── error codes are codes, never text ────────────────────────────────────────


class _UnprintableError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("__str__ exploded")


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (OidcVerificationError(f"E_OIDC {BEARER}"), 401, "E_OIDC_INVALID"),
        (OidcVerificationError("e_oidc_lowercase"), 401, "E_OIDC_INVALID"),
        (OidcVerificationError("E_" + "A" * 65), 401, "E_OIDC_INVALID"),
        (OidcVerificationError(42), 401, "E_OIDC_INVALID"),
        (OidcVerificationError(), 401, "E_OIDC_INVALID"),
        (OidcTeamMappingError(BEARER), 403, "E_ISSUE_NO_TEAM"),
        (JwksUnavailableError(f"fetch of {BEARER} failed"), 503, "E_JWKS_UNAVAILABLE"),
        (_UnprintableError(BEARER), 500, "E_ISSUE_INTERNAL"),
        (KeyError(BEARER), 500, "E_ISSUE_INTERNAL"),
    ],
    ids=["text", "lowercase", "too-long", "int", "no-args", "team", "jwks", "unprintable", "key"],
)
async def test_an_exception_that_does_not_carry_a_code_gets_the_default(
    exc: Exception, status: int, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    router = _router(_raising(exc), issue_max_inflight=1)

    with caplog.at_level(logging.DEBUG):
        resp = await _post(router)
        again = await _post(router)

    assert (resp.status_code, resp.json()) == (status, {"error": code})
    assert again.status_code == status
    assert BEARER not in resp.text
    assert BEARER not in caplog.text


# ── the token bucket at its edges ────────────────────────────────────────────


async def test_concurrent_requests_at_the_exact_refill_instant_get_exactly_one_token() -> None:
    now = [1000.0]
    router = _router(issue_rate_per_minute=30, issue_max_inflight=16, issue_clock=lambda: now[0])
    for _ in range(30):
        assert (await _post(router)).status_code == 200

    now[0] += 1.999
    early = await _post(router)
    now[0] = 1002.0  # 0.5 token/s: exactly one token back
    async with _client(router) as client:
        burst = await asyncio.gather(
            *(
                client.post(PATH, headers={"Authorization": f"Bearer {BEARER}-{i}"})
                for i in range(6)
            )
        )

    assert (early.status_code, early.json()) == (429, {"error": "E_ISSUE_THROTTLED"})
    assert sorted(r.status_code for r in burst) == [200] + [429] * 5
    assert {r.json()["error"] for r in burst if r.status_code == 429} == {"E_ISSUE_THROTTLED"}


async def test_a_long_idle_refills_to_the_burst_size_and_no_further() -> None:
    now = [1000.0]
    router = _router(issue_rate_per_minute=2, issue_clock=lambda: now[0])
    for _ in range(2):
        await _post(router)

    now[0] += 86_400.0
    statuses = [(await _post(router)).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]


async def test_an_inflight_refusal_spends_no_bucket_token() -> None:
    now = [1000.0]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def verify(token: str) -> OidcClaims:
        entered.set()
        await release.wait()
        return await _accept(token)

    router = _router(
        verify, issue_max_inflight=1, issue_rate_per_minute=2, issue_clock=lambda: now[0]
    )
    async with _client(router) as client:
        held = asyncio.create_task(
            client.post(PATH, headers={"Authorization": f"Bearer {BEARER}-held"})
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        refused = [
            await client.post(PATH, headers={"Authorization": f"Bearer {BEARER}-{i}"})
            for i in range(5)
        ]
        release.set()
        assert (await held).status_code == 200
        last_token = await client.post(PATH, headers={"Authorization": f"Bearer {BEARER}"})
        empty = await client.post(PATH, headers={"Authorization": f"Bearer {BEARER}"})

    assert {r.json()["error"] for r in refused} == {"E_ISSUE_INFLIGHT"}
    assert last_token.status_code == 200
    assert (empty.status_code, empty.json()) == (429, {"error": "E_ISSUE_THROTTLED"})


async def test_health_probes_answer_while_every_issuance_slot_is_held() -> None:
    entered = asyncio.Semaphore(0)
    release = asyncio.Event()

    async def verify(token: str) -> OidcClaims:
        entered.release()
        await release.wait()
        return await _accept(token)

    router = _router(verify, issue_max_inflight=3)
    async with _client(router) as client:
        held = [
            asyncio.create_task(
                client.post(PATH, headers={"Authorization": f"Bearer {BEARER}-{i}"})
            )
            for i in range(3)
        ]
        for _ in held:
            await asyncio.wait_for(entered.acquire(), timeout=2)
        probes = [
            await asyncio.wait_for(client.get(path), timeout=1)
            for path in ("/healthz/live", "/healthz/ready", "/healthz/live")
        ]
        release.set()
        await asyncio.gather(*held)

    assert [p.status_code for p in probes] == [200, 200, 200]


# ── the route gate in front of the router ────────────────────────────────────


def _gated(router: HealthRouter) -> object:
    from corp_llm_gateway.audit import AuditLogger, ListSink
    from corp_llm_gateway.metrics import NoopExporter
    from corp_llm_gateway.route_gate import RouteGateMiddleware

    return RouteGateMiddleware(
        router,
        metrics=NoopExporter(),
        audit_logger=AuditLogger(ListSink(), gateway_version="0.0.0"),
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("HEAD", PATH),
        ("OPTIONS", PATH),
        ("GET", PATH),
        ("POST", PATH + "/"),
        ("POST", "/Internal/Issue-Token"),
        ("POST", "/internal/issue-token/../issue-token"),
        ("POST", "/internal//issue-token"),
    ],
    ids=["head", "options", "get", "trailing-slash", "case", "traversal", "double-slash"],
)
async def test_the_gate_refuses_every_variant_of_the_path_before_the_router(
    method: str, path: str
) -> None:
    seen, verify = _recording()
    reached: list[str] = []

    async def fallthrough(scope: Scope, receive: object, send: object) -> None:
        reached.append(scope["path"])
        raise AssertionError("a refused variant reached the downstream app")

    gate = _gated(_router(verify, fallthrough=fallthrough))
    scope = _scope()
    scope.update({"method": method, "path": path, "raw_path": path.encode()})

    status, _ = await _drive(gate, _empty_body, scope)  # type: ignore[arg-type]

    assert status in (403, 404)
    assert seen == []
    assert reached == []


# ── the bound on the issuer's work, and the store's 503s ─────────────────────


class _HangingStore(InMemoryTokenStore):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def issue_for_subject(self, info: TokenInfo, **kwargs: object) -> TokenInfo:
        self.entered.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _FailingStore(InMemoryTokenStore):
    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc

    async def issue_for_subject(self, info: TokenInfo, **kwargs: object) -> TokenInfo:
        raise self._exc


def _settings() -> IssuanceSettings:
    return IssuanceSettings(
        issuer="https://kc.corp.test/realms/dev",
        audience="corp-gateway-issuance",
        client_id="corp-gateway-cli",
        jwks_url="https://kc.corp.test/realms/dev/protocol/openid-connect/certs",
        team_claim="groups",
        team_map=(("/devs", "t1"),),
        user_claim="preferred_username",
        operator_audience="",
        ca_bundle=None,
        token_ttl_days=30,
        max_active=2,
        min_interval_seconds=600,
        max_inflight=4,
        rate_per_minute=30,
    )


async def _claims(token: str) -> OidcClaims:
    return OidcClaims(
        "alice", "t1", issuer="https://kc.corp.test/realms/dev", subject="s", jti=token
    )


def _policy_router(store: InMemoryTokenStore, **bounds: object) -> HealthRouter:
    issuer = TokenIssuer(store, _claims, policy=IssuancePolicy(store, _settings()))
    return build_health_router(
        live_check=LiveCheck(),
        ready_check=ReadyCheck(check_redis=_ok, check_postgres=_ok),
        sanitization_check=SanitizationCheck(run_round_trip=_ok),
        extensions_check=ExtensionsCheck(health_all=_ext),
        token_issuer=issuer,
        **bounds,  # type: ignore[arg-type]
    )


async def test_a_hung_store_answers_503_at_the_bound_and_frees_its_slot() -> None:
    store = _HangingStore()
    router = _policy_router(store, issue_timeout_s=0.2, issue_max_inflight=1)

    start = asyncio.get_running_loop().time()
    resp = await asyncio.wait_for(_post(router), timeout=5)
    elapsed = asyncio.get_running_loop().time() - start

    assert (resp.status_code, resp.json()) == (503, {"error": "E_ISSUE_STORE_TIMEOUT"})
    assert resp.headers["cache-control"] == "no-store"
    assert 0.2 <= elapsed < 2.0
    assert store.entered.is_set()
    assert router._issue_inflight == 0


async def test_a_hung_verifier_or_team_lookup_is_bounded_too() -> None:
    async def hang(token: str) -> OidcClaims:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    resp = await asyncio.wait_for(_post(_router(hang, issue_timeout_s=0.1)), timeout=5)

    assert (resp.status_code, resp.json()) == (503, {"error": "E_ISSUE_STORE_TIMEOUT"})


async def test_a_disconnect_while_the_store_hangs_still_frees_the_slot_at_the_bound() -> None:
    store = _HangingStore()
    router = _policy_router(store, issue_timeout_s=0.2, issue_max_inflight=1)
    messages = iter(
        [{"type": "http.request", "body": b"", "more_body": False}, {"type": "http.disconnect"}]
    )

    async def receive() -> dict:
        return next(messages, {"type": "http.disconnect"})

    stuck = asyncio.create_task(_drive(router, receive))
    await asyncio.wait_for(store.entered.wait(), timeout=2)
    held = router._issue_inflight
    status, body = await asyncio.wait_for(stuck, timeout=5)

    assert held == 1
    assert (status, body) == (503, {"error": "E_ISSUE_STORE_TIMEOUT"})
    assert router._issue_inflight == 0


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError(111, f"connect to {BEARER} refused"),
        ConnectionResetError(),
        TimeoutError(),  # asyncpg's pool acquire past its timeout
        OSError(f"no route to {BEARER}"),
    ],
    ids=["refused", "reset", "acquire-timeout", "oserror"],
)
async def test_a_connection_class_store_failure_is_503_not_500(
    exc: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    router = _policy_router(_FailingStore(exc), issue_max_inflight=1)

    with caplog.at_level(logging.DEBUG):
        resp = await _post(router)

    assert (resp.status_code, resp.json()) == (503, {"error": "E_ISSUE_STORE_UNAVAILABLE"})
    assert type(exc).__name__ in caplog.text
    assert BEARER not in caplog.text
    assert router._issue_inflight == 0


@pytest.mark.parametrize(
    "name",
    [
        "PostgresConnectionError",
        "InterfaceError",
        "ConnectionDoesNotExistError",
        "CannotConnectNowError",
        "TooManyConnectionsError",
    ],
)
async def test_an_asyncpg_connection_failure_is_503(name: str) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    exc = getattr(asyncpg.exceptions, name)("connection gone")

    resp = await _post(_policy_router(_FailingStore(exc)))

    assert (resp.status_code, resp.json()) == (503, {"error": "E_ISSUE_STORE_UNAVAILABLE"})


async def test_an_asyncpg_query_failure_stays_500() -> None:
    asyncpg = pytest.importorskip("asyncpg")
    exc = asyncpg.exceptions.UndefinedColumnError("column oidc_jti does not exist")

    resp = await _post(_policy_router(_FailingStore(exc)))

    assert (resp.status_code, resp.json()) == (500, {"error": "E_ISSUE_INTERNAL"})


@pytest.mark.parametrize(
    "bounds",
    [
        {"issue_body_timeout_s": 0},
        {"issue_body_timeout_s": -1.0},
        {"issue_body_timeout_s": float("nan")},
        {"issue_timeout_s": 0},
        {"issue_timeout_s": -0.5},
        {"issue_timeout_s": float("nan")},
    ],
)
def test_non_positive_timeouts_are_refused(bounds: dict) -> None:
    with pytest.raises(ValueError):
        _router(**bounds)


# ── HEAD carries no body; every issuance answer is no-store ──────────────────


async def _drive_raw(router: HealthRouter, method: str) -> tuple[int, dict, bytes]:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await router({**_scope(), "method": method}, _empty_body, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


@pytest.mark.parametrize(("issuer", "status"), [(None, 404), (_accept, 405)])
async def test_a_head_to_the_issuance_path_gets_no_body(
    issuer: Verifier | None, status: int
) -> None:
    got, headers, body = await _drive_raw(_router(issuer), "HEAD")

    assert got == status
    assert body == b""
    assert headers[b"cache-control"] == b"no-store"


async def test_a_head_is_served_over_a_real_socket_without_a_protocol_error() -> None:
    uvicorn = pytest.importorskip("uvicorn")
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(_router(None), lifespan="off", log_level="warning", access_log=False)
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            head = await client.head(PATH)
            post = await client.post(PATH)
    finally:
        server.should_exit = True
        await task
        sock.close()

    assert (head.status_code, head.content) == (404, b"")
    assert post.json() == {"error": "E_ISSUE_DISABLED"}


@pytest.mark.parametrize(
    ("router", "kwargs", "status"),
    [
        (lambda: _router(None), {}, 404),
        (lambda: _router(), {"headers": {}}, 401),
        (lambda: _router(), {"content": b"x"}, 400),
        (lambda: _router(_raising(RuntimeError("x"))), {}, 500),
        (lambda: _router(_raising(IssuancePolicyError(IssuancePolicyError.RATE))), {}, 403),
        (lambda: _router(_raising(JwksUnavailableError())), {}, 503),
        (lambda: _router(), {}, 200),
    ],
    ids=["404", "401", "400", "500", "403", "503", "200"],
)
async def test_every_issuance_response_is_no_store(
    router: Callable[[], HealthRouter], kwargs: dict, status: int
) -> None:
    resp = await _post(router(), **kwargs)

    assert resp.status_code == status
    assert resp.headers["cache-control"] == "no-store"


async def test_the_405_and_429_answers_are_no_store() -> None:
    router = _router(issue_rate_per_minute=1)
    async with _client(router) as client:
        wrong_method = await client.get(PATH)
    await _post(router)
    throttled = await _post(router)

    assert (wrong_method.status_code, throttled.status_code) == (405, 429)
    assert wrong_method.headers["cache-control"] == "no-store"
    assert throttled.headers["cache-control"] == "no-store"
