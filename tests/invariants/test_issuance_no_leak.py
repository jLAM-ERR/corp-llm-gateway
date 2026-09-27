"""M1-14 on the issuance surface: `POST /internal/issue-token`.

The OIDC bearer, the minted corp token, and the `sub`, groups and username the
bearer carries must never reach a log line, an error body, a metric label, an
audit record or stdout — on success and on every refusal. Driven through the
same chain the entrypoint serves (route gate -> HealthRouter).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.healthz import build_health_router
from corp_llm_gateway.healthz.checks import (
    ExtensionsCheck,
    HealthStatus,
    LiveCheck,
    ReadyCheck,
    SanitizationCheck,
)
from corp_llm_gateway.metrics import MetricsExporter
from corp_llm_gateway.route_gate import RouteGateMiddleware
from corp_llm_gateway.tokens import (
    InMemoryTokenStore,
    IssuancePolicyError,
    JwksUnavailableError,
    OidcClaims,
    OidcTeamMappingError,
    OidcVerificationError,
    TokenIssuer,
)

SUB = "sub-7c1e-leakcheck"
USERNAME = "alice.leakcheck"
GROUP = "/secret-group/leakcheck"
BEARER = "eyJhbGciOiJSUzI1NiJ9.bearer-leakcheck-5e2a.sig-leakcheck"
CORP_TOKEN = "ct_minted-leakcheck-0b9d"
SECRETS = (SUB, USERNAME, GROUP, BEARER, CORP_TOKEN)
PATH = "/internal/issue-token"


class _RecordingMetrics(MetricsExporter):
    def __init__(self) -> None:
        self.labels: list[str] = []

    def record_block(self, block_reason: str) -> None:
        self.labels.append(block_reason)

    def record_failure(self, component: str) -> None:
        self.labels.append(component)

    def observe_request_latency(self, seconds: float, *, status: str) -> None:
        self.labels.append(status)


async def _ok() -> bool:
    return True


async def _ext() -> dict[str, HealthStatus]:
    return {}


def _leaky(exc: BaseException) -> Callable[[str], Awaitable[OidcClaims]]:
    """A verifier whose failure quotes everything it was handed."""

    async def verify(token: str) -> OidcClaims:
        raise exc

    return verify


async def _accept(token: str) -> OidcClaims:
    return OidcClaims(
        user_id=USERNAME,
        team_id="t1",
        issuer="https://kc.corp.lan/realms/dev",
        subject=SUB,
        jti="jti-leakcheck",
    )


_DETAIL = f"token={BEARER} sub={SUB} user={USERNAME} groups={GROUP}"

CASES: list[tuple[str, Callable[[str], Awaitable[OidcClaims]], dict[str, Any], int]] = [
    ("issued", _accept, {}, 200),
    ("body", _accept, {"content": f'{{"oidc_token": "{BEARER}"}}'}, 400),
    ("unauthorized", _leaky(OidcVerificationError(_DETAIL)), {}, 401),
    ("no-team", _leaky(OidcTeamMappingError(_DETAIL)), {}, 403),
    ("rate", _leaky(IssuancePolicyError(IssuancePolicyError.RATE)), {}, 403),
    ("replay", _leaky(IssuancePolicyError(IssuancePolicyError.REPLAY)), {}, 403),
    ("busy", _leaky(IssuancePolicyError(IssuancePolicyError.BUSY)), {}, 503),
    ("jwks-down", _leaky(JwksUnavailableError(_DETAIL)), {}, 503),
    ("internal", _leaky(RuntimeError(_DETAIL)), {}, 500),
]


def _stack(
    verifier: Callable[[str], Awaitable[OidcClaims]] | None, **bounds: Any
) -> tuple[RouteGateMiddleware, ListSink, _RecordingMetrics]:
    issuer = (
        TokenIssuer(InMemoryTokenStore(), verifier, token_factory=lambda: CORP_TOKEN)
        if verifier is not None
        else None
    )
    router = build_health_router(
        live_check=LiveCheck(),
        ready_check=ReadyCheck(check_redis=_ok, check_postgres=_ok),
        sanitization_check=SanitizationCheck(run_round_trip=_ok),
        extensions_check=ExtensionsCheck(health_all=_ext),
        token_issuer=issuer,
        **bounds,
    )
    sink = ListSink()
    metrics = _RecordingMetrics()
    gate = RouteGateMiddleware(
        router, metrics=metrics, audit_logger=AuditLogger(sink, gateway_version="0.0.0")
    )
    return gate, sink, metrics


async def _post(app: Any, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        return await client.post(PATH, headers={"Authorization": f"Bearer {BEARER}"}, **kwargs)


def _assert_clean(
    *,
    body: str,
    log_text: str,
    stdout: str,
    sink: ListSink,
    metrics: _RecordingMetrics,
    body_may_carry_the_token: bool = False,
) -> None:
    for secret in SECRETS:
        if not (body_may_carry_the_token and secret == CORP_TOKEN):
            assert secret not in body, f"{secret!r} leaked into the response body"
        assert secret not in log_text, f"{secret!r} leaked into a log line"
        assert secret not in stdout, f"{secret!r} leaked into stdout"
        assert secret not in json.dumps(sink.records), f"{secret!r} leaked into an audit record"
        assert secret not in json.dumps(metrics.labels), f"{secret!r} leaked into a metric label"
    assert "Traceback" not in log_text


@pytest.mark.parametrize(
    ("verifier", "kwargs", "status"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
async def test_no_issuance_path_leaks_a_credential_or_an_identity(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    verifier: Callable[[str], Awaitable[OidcClaims]],
    kwargs: dict[str, Any],
    status: int,
) -> None:
    gate, sink, metrics = _stack(verifier)

    with caplog.at_level(logging.DEBUG):
        resp = await _post(gate, **kwargs)

    assert resp.status_code == status
    if status == 200:
        assert resp.json()["corp_token"] == CORP_TOKEN
    else:
        assert list(resp.json()) == ["error"]
    captured = capsys.readouterr()
    _assert_clean(
        body=resp.text,
        log_text=caplog.text,
        stdout=captured.out + captured.err,
        sink=sink,
        metrics=metrics,
        body_may_carry_the_token=status == 200,
    )


async def test_the_429_bounds_leak_nothing(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    gate, sink, metrics = _stack(_accept, issue_rate_per_minute=1)

    with caplog.at_level(logging.DEBUG):
        await _post(gate)
        resp = await _post(gate)

    assert resp.status_code == 429
    captured = capsys.readouterr()
    _assert_clean(
        body=resp.text,
        log_text=caplog.text,
        stdout=captured.out + captured.err,
        sink=sink,
        metrics=metrics,
    )


async def test_disabled_issuance_leaks_nothing(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    gate, sink, metrics = _stack(None)

    with caplog.at_level(logging.DEBUG):
        resp = await _post(gate)

    assert resp.status_code == 404
    captured = capsys.readouterr()
    _assert_clean(
        body=resp.text,
        log_text=caplog.text,
        stdout=captured.out + captured.err,
        sink=sink,
        metrics=metrics,
    )


# ── the real verifier: its own log lines and refusals ────────────────────────


@pytest.mark.parametrize(
    ("groups", "audience", "jwks_up", "status"),
    [
        ([GROUP], "corp-gateway-issuance", True, 200),
        ([GROUP], "someone-else", True, 401),
        (["/unmapped"], "corp-gateway-issuance", True, 403),
        ([GROUP], "corp-gateway-issuance", False, 503),
    ],
    ids=["issued", "wrong-audience", "no-team", "jwks-down"],
)
async def test_the_real_verifier_leaks_nothing_on_any_path(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    groups: list[str],
    audience: str,
    jwks_up: bool,
    status: int,
) -> None:
    pytest.importorskip("cryptography")
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from corp_llm_gateway.tokens import JwksClient, KeycloakOidcVerifier

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer_url = "https://kc.corp.lan/realms/dev"
    jwk = {
        **jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
        "kid": "kid-leak",
        "use": "sig",
        "alg": "RS256",
    }

    def _jwks(request: httpx.Request) -> httpx.Response:
        if not jwks_up:
            return httpx.Response(503, text=_DETAIL)
        return httpx.Response(200, json={"keys": [jwk]})

    verifier = KeycloakOidcVerifier(
        issuer=issuer_url,
        audience="corp-gateway-issuance",
        client_id="corp-gateway-cli",
        team_map=((GROUP, "t1"),),
        jwks=JwksClient(
            f"{issuer_url}/certs", http=httpx.AsyncClient(transport=httpx.MockTransport(_jwks))
        ),
    )
    now = int(time.time())
    bearer = jwt.encode(
        {
            "iss": issuer_url,
            "aud": audience,
            "azp": "corp-gateway-cli",
            "sub": SUB,
            "jti": "jti-real-leakcheck",
            "iat": now,
            "exp": now + 300,
            "preferred_username": USERNAME,
            "groups": groups,
        },
        key,
        "RS256",
        headers={"kid": "kid-leak", "typ": "JWT"},
    )
    gate, sink, metrics = _stack(verifier)

    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gate), base_url="http://gateway"
        ) as client:
            resp = await client.post(PATH, headers={"Authorization": f"Bearer {bearer}"})

    assert resp.status_code == status
    captured = capsys.readouterr()
    for secret in (bearer, *SECRETS):
        if not (status == 200 and secret == CORP_TOKEN):
            assert secret not in resp.text
        assert secret not in caplog.text
        assert secret not in captured.out + captured.err
        assert secret not in json.dumps(sink.records)
        assert secret not in json.dumps(metrics.labels)
