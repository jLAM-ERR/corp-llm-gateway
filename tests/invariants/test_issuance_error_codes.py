"""The issuance surface speaks in error codes: `E_` + upper-case words, passed
through the route verbatim. Its log lines are code-shaped too, and nothing on the
path prints to stdout.
"""

from __future__ import annotations

import functools
import logging
import re
import time

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
from corp_llm_gateway.tokens import (
    InMemoryTokenStore,
    IssuancePolicy,
    OidcClaims,
    OidcVerificationError,
    TokenIssuer,
)

# Every code the issuance sources raise or return.
CODES = (
    "E_ISSUE_BODY",
    "E_ISSUE_BODY_TIMEOUT",
    "E_ISSUE_BUSY",
    "E_ISSUE_DISABLED",
    "E_ISSUE_INFLIGHT",
    "E_ISSUE_INTERNAL",
    "E_ISSUE_NO_TEAM",
    "E_ISSUE_RATE",
    "E_ISSUE_REFUSED",
    "E_ISSUE_REPLAY",
    "E_ISSUE_SCHEMA",
    "E_ISSUE_STORE_TIMEOUT",
    "E_ISSUE_STORE_UNAVAILABLE",
    "E_ISSUE_THROTTLED",
    "E_ISSUE_UNKNOWN_TEAM",
    "E_JWKS_UNAVAILABLE",
    "E_METHOD_NOT_ALLOWED",
    "E_OIDC_ALG",
    "E_OIDC_AUDIENCE",
    "E_OIDC_AZP",
    "E_OIDC_CLAIMS",
    "E_OIDC_CLAIM_TYPE",
    "E_OIDC_EXPIRED",
    "E_OIDC_INVALID",
    "E_OIDC_ISSUER",
    "E_OIDC_KID",
    "E_OIDC_MALFORMED",
    "E_OIDC_MISSING",
    "E_OIDC_MISSING_CLAIM",
    "E_OIDC_NOT_YET_VALID",
    "E_OIDC_OPERATOR_AUDIENCE",
    "E_OIDC_SIGNATURE",
    "E_OIDC_TYP",
    "E_OIDC_UNKNOWN_KID",
)
PATH = "/internal/issue-token"
_ISSUER = "https://kc.corp.lan/realms/dev"


async def _ok() -> bool:
    return True


async def _ext() -> dict[str, HealthStatus]:
    return {}


def _router(verifier: object, **bounds: object) -> object:
    return build_health_router(
        live_check=LiveCheck(),
        ready_check=ReadyCheck(check_redis=_ok, check_postgres=_ok),
        sanitization_check=SanitizationCheck(run_round_trip=_ok),
        extensions_check=ExtensionsCheck(health_all=_ext),
        token_issuer=TokenIssuer(InMemoryTokenStore(), verifier),  # type: ignore[arg-type]
        **bounds,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("code", CODES)
async def test_every_code_survives_the_route_verbatim(code: str) -> None:
    async def verify(token: str) -> OidcClaims:
        raise OidcVerificationError(code)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_router(verify)), base_url="http://gateway"
    ) as client:
        resp = await client.post(PATH, headers={"Authorization": "Bearer t"})

    assert resp.json() == {"error": code}


_LOG_SHAPES = (
    re.compile(r"issue_token status=\d{3} code=(ok|E_[A-Z_]+)"),
    re.compile(r"issuance token rejected: E_[A-Z_]+"),
    re.compile(r"issuance JWKS fetch failed: [A-Za-z0-9 ]+"),
)


@functools.cache
def _key() -> object:
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _real_router(*, jwks_up: bool, rate_per_minute: int = 30) -> object:
    import jwt

    from corp_llm_gateway.settings import IssuanceSettings
    from corp_llm_gateway.tokens import KeycloakOidcVerifier

    jwk = {
        **jwt.algorithms.RSAAlgorithm.to_jwk(_key().public_key(), as_dict=True),
        "kid": "kid-codes",
        "use": "sig",
        "alg": "RS256",
    }

    def _jwks(request: httpx.Request) -> httpx.Response:
        if not jwks_up:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"keys": [jwk]})

    configured = IssuanceSettings(
        issuer=_ISSUER,
        audience="corp-gateway-issuance",
        client_id="corp-gateway-cli",
        jwks_url=f"{_ISSUER}/certs",
        team_claim="groups",
        team_map=(("/devs", "t1"),),
        user_claim="preferred_username",
        operator_audience="",
        ca_bundle=None,
        token_ttl_days=30,
        max_active=2,
        min_interval_seconds=600,
        max_inflight=4,
        rate_per_minute=rate_per_minute,
    )
    verifier = KeycloakOidcVerifier.from_settings(
        configured, http=httpx.AsyncClient(transport=httpx.MockTransport(_jwks))
    )
    store = InMemoryTokenStore()
    return build_health_router(
        live_check=LiveCheck(),
        ready_check=ReadyCheck(check_redis=_ok, check_postgres=_ok),
        sanitization_check=SanitizationCheck(run_round_trip=_ok),
        extensions_check=ExtensionsCheck(health_all=_ext),
        token_issuer=TokenIssuer(store, verifier, policy=IssuancePolicy(store, configured)),
        issue_rate_per_minute=rate_per_minute,
    )


def _bearer(jti: str, **claims: object) -> str:
    import jwt

    now = int(time.time())
    body = {
        "iss": _ISSUER,
        "aud": "corp-gateway-issuance",
        "azp": "corp-gateway-cli",
        "sub": "sub-codes",
        "jti": jti,
        "iat": now,
        "exp": now + 300,
        "groups": ["/devs"],
        **claims,
    }
    return jwt.encode(body, _key(), "RS256", headers={"kid": "kid-codes", "typ": "JWT"})


async def _post(router: object, token: str | None, **kwargs: object) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=router), base_url="http://gateway"
    ) as client:
        resp = await client.post(PATH, headers=headers, **kwargs)  # type: ignore[arg-type]
    return resp.status_code


async def test_log_lines_are_code_shaped_and_stdout_stays_empty_on_every_path(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("cryptography")
    up = _real_router(jwks_up=True, rate_per_minute=9)
    down = _real_router(jwks_up=False)

    with caplog.at_level(logging.DEBUG, logger="corp_llm_gateway"):
        statuses = [
            await _post(up, _bearer("j1")),
            await _post(up, _bearer("j1")),
            await _post(up, _bearer("j2")),
            await _post(up, _bearer("j3", groups=["/other"])),
            await _post(up, _bearer("j4", aud="someone-else")),
            await _post(up, "not-a-jwt"),
            await _post(up, None),
            await _post(up, _bearer("j5"), content=b"x"),
            await _post(down, _bearer("j6")),
            await _post(up, _bearer("j7")),
            await _post(up, _bearer("j8")),
        ]

    assert statuses == [200, 403, 403, 403, 401, 401, 401, 400, 503, 403, 429]
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("corp_llm_gateway")]
    assert len(lines) >= len(statuses)
    for line in lines:
        assert any(shape.fullmatch(line) for shape in _LOG_SHAPES), line
    assert capsys.readouterr().out == ""
