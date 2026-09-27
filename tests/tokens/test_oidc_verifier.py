"""KeycloakOidcVerifier + JwksClient — the issuance-side OIDC contract.

Signs real RS256 tokens and serves the JWKS through ``httpx.MockTransport``, so
the module needs `cryptography` (the 'oidc' extra) — it skips on the
graceful-degradation venv that lacks it.
"""

from __future__ import annotations

import asyncio
import base64
import gc
import hashlib
import hmac
import json
import logging
import sys
import time
import traceback
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import httpx
import jwt
import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from corp_llm_gateway import config, settings
from corp_llm_gateway.settings import ConfigError, IssuanceSettings
from corp_llm_gateway.tokens import oidc_verifier
from corp_llm_gateway.tokens.issuance import (
    JwksUnavailableError,
    OidcClaims,
    OidcTeamMappingError,
    OidcVerificationError,
)
from corp_llm_gateway.tokens.oidc_verifier import JwksClient, KeycloakOidcVerifier

_ISSUER = "https://keycloak.corp.lan/realms/dev"
_JWKS_URL = f"{_ISSUER}/protocol/openid-connect/certs"
_AUDIENCE = "corp-gateway-issuance"
_OPERATOR_AUDIENCE = "corp-llm-gateway"
_CLIENT_ID = "corp-gateway-cli"
_TEAM_MAP = (("/devs/payments", "payments"), ("/devs/core", "core"))
_SUB = "5f2b1c9e-sub-fixture-7d41"
_USERNAME = "alice.fixture"
_GROUP = "/devs/payments"


def _rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


_KEY_A = _rsa_key()
_KEY_B = _rsa_key()
_ROGUE = _rsa_key()


def _jwk(key: rsa.RSAPrivateKey, kid: str, **extra: Any) -> dict[str, Any]:
    data = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    data.update({"kid": kid, "use": "sig", "alg": "RS256", **extra})
    return data


def _claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "azp": _CLIENT_ID,
        "sub": _SUB,
        "jti": "jti-fixture-1",
        "iat": now,
        "exp": now + 300,
        "typ": "Bearer",
        "preferred_username": _USERNAME,
        "groups": [_GROUP],
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not _DROP}


_DROP = object()


def _sign(
    claims: dict[str, Any] | None = None,
    *,
    key: rsa.RSAPrivateKey = _KEY_A,
    kid: str | None = "kid-a",
    typ: str | None = "JWT",
) -> str:
    # PyJWT drops a falsy typ, so typ=None yields a validly signed token without one.
    headers: dict[str, Any] = {"typ": typ}
    if kid is not None:
        headers["kid"] = kid
    return jwt.encode(claims if claims is not None else _claims(), key, "RS256", headers=headers)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unsigned(header: dict[str, Any], claims: dict[str, Any], sig: bytes = b"") -> str:
    head = _b64(json.dumps(header).encode())
    body = _b64(json.dumps(claims).encode())
    return f"{head}.{body}.{_b64(sig)}"


class _Jwks:
    """MockTransport-backed JWKS endpoint that counts requests."""

    def __init__(self, keys: list[dict[str, Any]] | None = None) -> None:
        self.keys = keys if keys is not None else [_jwk(_KEY_A, "kid-a")]
        self.calls = 0
        self.delay = 0.0
        self.respond: Callable[[httpx.Request], Awaitable[httpx.Response]] | None = None

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.respond is not None:
            return await self.respond(request)
        return httpx.Response(200, json={"keys": self.keys})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _verifier(
    jwks: _Jwks,
    *,
    clock: _Clock | None = None,
    timeout_s: float = 3.0,
    operator_audience: str = _OPERATOR_AUDIENCE,
    **kwargs: Any,
) -> KeycloakOidcVerifier:
    client = JwksClient(_JWKS_URL, http=jwks.client(), timeout_s=timeout_s, clock=clock or _Clock())
    return KeycloakOidcVerifier(
        issuer=_ISSUER,
        audience=_AUDIENCE,
        client_id=_CLIENT_ID,
        team_map=_TEAM_MAP,
        operator_audience=operator_audience,
        jwks=client,
        **kwargs,
    )


def _assert_no_leak(
    caplog: pytest.LogCaptureFixture, exc: BaseException, token: str, *extra: str
) -> None:
    rendered = "".join(traceback.format_exception(exc)) + repr(exc.args)
    for secret in (token, _SUB, _GROUP, _USERNAME, *extra):
        assert secret not in caplog.text
        assert secret not in rendered


async def _rejects(
    verifier: KeycloakOidcVerifier,
    token: str,
    caplog: pytest.LogCaptureFixture,
    error: type[Exception] = OidcVerificationError,
) -> Exception:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(error) as info:
        await verifier(token)
    _assert_no_leak(caplog, info.value, token)
    return info.value


# ── happy path ───────────────────────────────────────────────────────────────


async def test_valid_keycloak_token_yields_claims() -> None:
    jwks = _Jwks()
    claims = await _verifier(jwks)(_sign())
    assert claims == OidcClaims(
        user_id=_USERNAME,
        team_id="payments",
        issuer=_ISSUER,
        subject=_SUB,
        jti="jti-fixture-1",
    )
    assert jwks.calls == 1


async def test_at_jwt_typ_is_accepted() -> None:
    claims = await _verifier(_Jwks())(_sign(typ="at+jwt"))
    assert claims.subject == _SUB


async def test_audience_list_containing_ours_is_accepted() -> None:
    claims = await _verifier(_Jwks())(_sign(_claims(aud=[_AUDIENCE, "account"])))
    assert claims.team_id == "payments"


async def test_keys_are_cached_between_calls() -> None:
    jwks = _Jwks()
    verifier = _verifier(jwks)
    await verifier(_sign())
    await verifier(_sign(_claims(jti="jti-2")))
    assert jwks.calls == 1


async def test_user_id_falls_back_to_sub() -> None:
    claims = await _verifier(_Jwks())(_sign(_claims(preferred_username=_DROP)))
    assert claims.user_id == _SUB


async def test_user_id_falls_back_to_sub_on_a_non_string_user_claim() -> None:
    claims = await _verifier(_Jwks())(_sign(_claims(preferred_username=["x"])))
    assert claims.user_id == _SUB


async def test_custom_user_and_team_claims() -> None:
    verifier = _verifier(_Jwks(), user_claim="email", team_claim="teams")
    token = _sign(_claims(email="alice@corp.lan", teams=["/devs/core"], groups=_DROP))
    claims = await verifier(token)
    assert (claims.user_id, claims.team_id) == ("alice@corp.lan", "core")


async def test_first_mapped_group_in_map_order_wins() -> None:
    token = _sign(_claims(groups=["/devs/core", "/other", "/devs/payments"]))
    claims = await _verifier(_Jwks())(token)
    assert claims.team_id == "payments"


async def test_leeway_accepts_a_just_expired_token() -> None:
    now = int(time.time())
    claims = await _verifier(_Jwks())(_sign(_claims(iat=now - 400, exp=now - 30)))
    assert claims.subject == _SUB


# ── rejected tokens (401) ────────────────────────────────────────────────────


@pytest.mark.parametrize("claim", ["exp", "iat", "iss", "aud", "sub", "jti", "azp"])
async def test_missing_required_claim_is_rejected(
    claim: str, caplog: pytest.LogCaptureFixture
) -> None:
    await _rejects(_verifier(_Jwks()), _sign(_claims(**{claim: _DROP})), caplog)


@pytest.mark.parametrize(
    "override",
    [
        {"iss": "https://keycloak.corp.lan/realms/other"},
        {"aud": "some-other-client"},
        {"azp": "some-other-client"},
    ],
    ids=["wrong-iss", "wrong-aud", "wrong-azp"],
)
async def test_wrong_iss_aud_azp_is_rejected(
    override: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    await _rejects(_verifier(_Jwks()), _sign(_claims(**override)), caplog)


async def test_expired_token_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    now = int(time.time())
    exc = await _rejects(_verifier(_Jwks()), _sign(_claims(iat=now - 900, exp=now - 600)), caplog)
    assert str(exc) == "E_OIDC_EXPIRED"


async def test_nbf_in_the_future_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    now = int(time.time())
    await _rejects(_verifier(_Jwks()), _sign(_claims(nbf=now + 600)), caplog)


async def test_iat_in_the_future_beyond_the_leeway_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = int(time.time())
    token = _sign(_claims(iat=now + 600, exp=now + 900))
    exc = await _rejects(_verifier(_Jwks()), token, caplog)
    assert str(exc) == "E_OIDC_NOT_YET_VALID"


async def test_iat_in_the_future_within_the_leeway_is_accepted() -> None:
    now = int(time.time())
    claims = await _verifier(_Jwks())(_sign(_claims(iat=now + 30)))
    assert claims.subject == _SUB


async def test_a_trailing_slash_on_the_configured_issuer_is_ignored() -> None:
    client = JwksClient(_JWKS_URL, http=_Jwks().client(), clock=_Clock())
    verifier = KeycloakOidcVerifier(
        issuer=_ISSUER + "/",
        audience=_AUDIENCE,
        client_id=_CLIENT_ID,
        team_map=_TEAM_MAP,
        jwks=client,
    )
    assert (await verifier(_sign())).issuer == _ISSUER


async def test_a_trailing_slash_on_the_token_issuer_maps_to_the_same_issuer() -> None:
    claims = await _verifier(_Jwks())(_sign(_claims(iss=_ISSUER + "/")))
    assert claims.issuer == _ISSUER


async def test_a_token_at_the_length_cap_is_verified() -> None:
    cap = oidc_verifier.MAX_TOKEN_LENGTH
    jwks = _Jwks([_jwk(_KEY_A, kid) for kid in ("kid-a", "kid-aa", "kid-aaa")])
    token = _signed_token_of_length(cap)
    assert len(token) == cap
    assert (await _verifier(jwks)(token)).subject == _SUB


async def test_a_token_over_the_length_cap_is_malformed(caplog: pytest.LogCaptureFixture) -> None:
    jwks = _Jwks()
    token = _signed_token_of_length(oidc_verifier.MAX_TOKEN_LENGTH + 1)
    exc = await _rejects(_verifier(jwks), token, caplog)
    assert str(exc) == "E_OIDC_MALFORMED"
    assert jwks.calls == 0


def _signed_token_of_length(length: int) -> str:
    # base64url never yields a length of 1 mod 4, so vary the header too.
    for kid in ("kid-a", "kid-aa", "kid-aaa"):
        base = len(_sign(_claims(pad=""), kid=kid))
        estimate = (length - base) * 3 // 4
        for pad in range(max(0, estimate - 4), estimate + 5):
            token = _sign(_claims(pad="x" * pad), kid=kid)
            if len(token) == length:
                return token
    raise AssertionError(f"no signed token of length {length}")


async def test_operator_rbac_jwt_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    token = _sign(_claims(aud=_OPERATOR_AUDIENCE))
    await _rejects(_verifier(_Jwks()), token, caplog)


async def test_dual_audience_token_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    token = _sign(_claims(aud=[_AUDIENCE, _OPERATOR_AUDIENCE]))
    exc = await _rejects(_verifier(_Jwks()), token, caplog)
    assert str(exc) == "E_OIDC_OPERATOR_AUDIENCE"


async def test_dual_audience_is_accepted_only_without_an_operator_audience() -> None:
    token = _sign(_claims(aud=[_AUDIENCE, _OPERATOR_AUDIENCE]))
    claims = await _verifier(_Jwks(), operator_audience="")(token)
    assert claims.subject == _SUB


async def test_missing_azp_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    await _rejects(_verifier(_Jwks()), _sign(_claims(azp=_DROP)), caplog)


async def test_wrong_azp_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    exc = await _rejects(_verifier(_Jwks()), _sign(_claims(azp="admin-console")), caplog)
    assert str(exc) == "E_OIDC_AZP"


async def test_forged_signature_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    await _rejects(_verifier(_Jwks()), _sign(key=_ROGUE), caplog)


async def test_hs256_signed_with_the_jwks_key_material_is_rejected(
    caplog: pytest.LogCaptureFixture,
) -> None:
    jwks = _Jwks()
    pem = _KEY_A.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header = {"alg": "HS256", "typ": "JWT", "kid": "kid-a"}
    head = _b64(json.dumps(header).encode())
    body = _b64(json.dumps(_claims()).encode())
    sig = hmac.new(pem, f"{head}.{body}".encode(), hashlib.sha256).digest()
    exc = await _rejects(_verifier(jwks), f"{head}.{body}.{_b64(sig)}", caplog)
    assert str(exc) == "E_OIDC_ALG"
    assert jwks.calls == 0


@pytest.mark.parametrize("alg", ["none", "None", "NONE"])
async def test_alg_none_is_rejected(alg: str, caplog: pytest.LogCaptureFixture) -> None:
    jwks = _Jwks()
    token = _unsigned({"alg": alg, "typ": "JWT", "kid": "kid-a"}, _claims())
    exc = await _rejects(_verifier(jwks), token, caplog)
    assert str(exc) == "E_OIDC_ALG"
    assert jwks.calls == 0


@pytest.mark.parametrize("typ", ["JWE", "id+jwt", None, 7])
async def test_unexpected_typ_is_rejected(typ: Any, caplog: pytest.LogCaptureFixture) -> None:
    jwks = _Jwks()
    token = _sign(typ=typ)
    header = jwt.get_unverified_header(token)
    assert header.get("typ") == typ
    exc = await _rejects(_verifier(jwks), token, caplog)
    assert str(exc) == "E_OIDC_TYP"
    assert jwks.calls == 0


async def test_missing_kid_is_rejected(caplog: pytest.LogCaptureFixture) -> None:
    jwks = _Jwks()
    await _rejects(_verifier(jwks), _sign(kid=None), caplog)
    assert jwks.calls == 0


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "....."])
async def test_malformed_token_is_rejected(token: str, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(OidcVerificationError):
        await _verifier(_Jwks())(token)


@pytest.mark.parametrize("groups", ["/devs/payments", [1, 2], {"a": 1}])
async def test_team_claim_must_be_a_list_of_strings(
    groups: Any, caplog: pytest.LogCaptureFixture
) -> None:
    exc = await _rejects(_verifier(_Jwks()), _sign(_claims(groups=groups)), caplog)
    assert not isinstance(exc, OidcTeamMappingError)


# ── team mapping (403) ───────────────────────────────────────────────────────


async def test_unmapped_groups_raise_a_team_mapping_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = _sign(_claims(groups=["/devs/unknown"]))
    await _rejects(_verifier(_Jwks()), token, caplog, OidcTeamMappingError)


async def test_empty_groups_raise_a_team_mapping_error(caplog: pytest.LogCaptureFixture) -> None:
    await _rejects(_verifier(_Jwks()), _sign(_claims(groups=[])), caplog, OidcTeamMappingError)


async def test_missing_team_claim_raises_a_team_mapping_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _rejects(_verifier(_Jwks()), _sign(_claims(groups=_DROP)), caplog, OidcTeamMappingError)


def test_error_classes_are_distinct() -> None:
    assert not issubclass(OidcTeamMappingError, OidcVerificationError)
    assert not issubclass(JwksUnavailableError, OidcVerificationError)
    assert not issubclass(JwksUnavailableError, OidcTeamMappingError)


# ── JWKS refresh: unknown kid, cooldown, single-flight ───────────────────────


async def test_unknown_kid_refreshes_once_then_finds_a_rotated_key() -> None:
    jwks = _Jwks()
    clock = _Clock()
    verifier = _verifier(jwks, clock=clock)
    await verifier(_sign())
    jwks.keys = [_jwk(_KEY_A, "kid-a"), _jwk(_KEY_B, "kid-b")]
    clock.now += 61
    claims = await verifier(_sign(key=_KEY_B, kid="kid-b"))
    assert claims.subject == _SUB
    assert jwks.calls == 2


async def test_unknown_kid_within_cooldown_does_not_refetch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    jwks = _Jwks()
    clock = _Clock()
    verifier = _verifier(jwks, clock=clock)
    await verifier(_sign())
    clock.now += 61
    await _rejects(verifier, _sign(key=_KEY_B, kid="kid-b"), caplog)
    assert jwks.calls == 2
    clock.now += 30
    await _rejects(verifier, _sign(key=_KEY_B, kid="kid-c"), caplog)
    assert jwks.calls == 2
    clock.now += 31
    jwks.keys = [_jwk(_KEY_B, "kid-c")]
    claims = await verifier(_sign(key=_KEY_B, kid="kid-c"))
    assert claims.subject == _SUB
    assert jwks.calls == 3


async def test_known_kid_is_served_during_cooldown() -> None:
    jwks = _Jwks()
    clock = _Clock()
    verifier = _verifier(jwks, clock=clock)
    await verifier(_sign())
    with pytest.raises(OidcVerificationError):
        await verifier(_sign(kid="kid-zzz"))
    assert (await verifier(_sign())).subject == _SUB
    assert jwks.calls == 1


async def test_concurrent_unknown_kid_requests_share_one_fetch() -> None:
    jwks = _Jwks()
    clock = _Clock()
    verifier = _verifier(jwks, clock=clock)
    await verifier(_sign())
    assert jwks.calls == 1
    jwks.keys = [_jwk(_KEY_A, "kid-a"), _jwk(_KEY_B, "kid-b")]
    jwks.delay = 0.05
    clock.now += 61
    tokens = [_sign(_claims(jti=f"j{i}"), key=_KEY_B, kid="kid-b") for i in range(10)]
    results = await asyncio.gather(*(verifier(t) for t in tokens))
    assert {r.jti for r in results} == {f"j{i}" for i in range(10)}
    assert jwks.calls == 2


async def test_concurrent_cold_start_shares_one_fetch() -> None:
    jwks = _Jwks()
    jwks.delay = 0.05
    verifier = _verifier(jwks)
    tokens = [_sign(_claims(jti=f"j{i}")) for i in range(10)]
    await asyncio.gather(*(verifier(t) for t in tokens))
    assert jwks.calls == 1


async def test_concurrent_unknown_kid_failures_share_one_fetch() -> None:
    jwks = _Jwks()
    jwks.delay = 0.05
    verifier = _verifier(jwks)
    tokens = [_sign(key=_KEY_B, kid="kid-missing") for _ in range(10)]
    results = await asyncio.gather(*(verifier(t) for t in tokens), return_exceptions=True)
    assert all(isinstance(r, OidcVerificationError) for r in results)
    assert jwks.calls == 1


async def test_a_cancelled_waiter_does_not_cancel_the_shared_fetch() -> None:
    jwks = _Jwks()
    jwks.delay = 0.05
    verifier = _verifier(jwks)
    first = asyncio.create_task(verifier(_sign()))
    second = asyncio.create_task(verifier(_sign(_claims(jti="j2"))))
    await asyncio.sleep(0.01)
    first.cancel()
    assert (await second).jti == "j2"
    assert jwks.calls == 1


async def test_rotated_out_keys_are_dropped_on_refresh(caplog: pytest.LogCaptureFixture) -> None:
    jwks = _Jwks()
    clock = _Clock()
    verifier = _verifier(jwks, clock=clock)
    await verifier(_sign())
    jwks.keys = [_jwk(_KEY_B, "kid-b")]
    clock.now += 61
    await verifier(_sign(key=_KEY_B, kid="kid-b"))
    await _rejects(verifier, _sign(), caplog)


async def test_jwks_entries_that_are_not_rs256_signing_keys_are_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    jwks = _Jwks(
        [
            _jwk(_KEY_A, "kid-enc", use="enc", alg="RSA-OAEP"),
            _jwk(_KEY_A, "kid-ps", alg="PS256"),
            {"kty": "oct", "kid": "kid-oct", "k": "c2VjcmV0"},
            {"kty": "RSA", "kid": "kid-bad", "n": "!!", "e": "AQAB"},
            "not-a-jwk",
            _jwk(_KEY_A, "kid-a"),
        ]
    )
    verifier = _verifier(jwks)
    assert (await verifier(_sign())).subject == _SUB
    for kid in ("kid-enc", "kid-ps", "kid-oct", "kid-bad"):
        await _rejects(verifier, _sign(kid=kid), caplog)


# ── JWKS unavailable (503) ───────────────────────────────────────────────────


async def _jwks_unavailable(
    jwks: _Jwks, caplog: pytest.LogCaptureFixture, *, timeout_s: float = 3.0
) -> None:
    await _rejects(_verifier(jwks, timeout_s=timeout_s), _sign(), caplog, JwksUnavailableError)


async def test_jwks_transport_timeout_is_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    jwks = _Jwks()

    async def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    jwks.respond = respond
    await _jwks_unavailable(jwks, caplog)


async def test_jwks_hang_is_bounded_by_the_timeout(caplog: pytest.LogCaptureFixture) -> None:
    jwks = _Jwks()
    jwks.delay = 5.0
    started = time.monotonic()
    await _jwks_unavailable(jwks, caplog, timeout_s=0.05)
    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize("status", [500, 502, 503, 404])
async def test_jwks_error_status_is_unavailable(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    jwks = _Jwks()

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="upstream error")

    jwks.respond = respond
    await _jwks_unavailable(jwks, caplog)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_jwks_redirect_is_unavailable_and_not_followed(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    jwks = _Jwks()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.example":
            return httpx.Response(200, json={"keys": [_jwk(_ROGUE, "kid-a")]})
        return httpx.Response(status, headers={"Location": "https://evil.example/certs"})

    jwks.respond = respond
    await _jwks_unavailable(jwks, caplog)
    assert jwks.calls == 1


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"keys": "x"}', b'{"keys": []}', b'{"nokeys": 1}'],
)
async def test_jwks_malformed_document_is_unavailable(
    body: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    jwks = _Jwks()

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    jwks.respond = respond
    await _jwks_unavailable(jwks, caplog)


def _failing_until_fixed(jwks: _Jwks) -> Callable[[], None]:
    state = {"failing": True}

    async def respond(request: httpx.Request) -> httpx.Response:
        if state["failing"]:
            return httpx.Response(503)
        return httpx.Response(200, json={"keys": jwks.keys})

    jwks.respond = respond
    return lambda: state.update(failing=False)


async def test_failed_fetch_backs_off_briefly_instead_of_starting_the_cooldown() -> None:
    jwks = _Jwks()
    fix = _failing_until_fixed(jwks)
    clock = _Clock()
    verifier = _verifier(jwks, clock=clock)
    with pytest.raises(JwksUnavailableError):
        await verifier(_sign())
    fix()
    clock.now += 4.9
    with pytest.raises(JwksUnavailableError):
        await verifier(_sign())
    assert jwks.calls == 1
    clock.now += 0.1
    assert (await verifier(_sign())).subject == _SUB
    assert jwks.calls == 2


async def test_failure_backoff_does_not_hide_cached_keys() -> None:
    jwks = _Jwks()
    clock = _Clock()
    verifier = _verifier(jwks, clock=clock)
    await verifier(_sign())
    _failing_until_fixed(jwks)
    clock.now += 61
    with pytest.raises(JwksUnavailableError):
        await verifier(_sign(key=_KEY_B, kid="kid-b"))
    assert (await verifier(_sign())).subject == _SUB
    assert jwks.calls == 2


async def test_waiters_arriving_during_the_failure_backoff_fail_fast() -> None:
    jwks = _Jwks()
    _failing_until_fixed(jwks)
    verifier = _verifier(jwks)
    with pytest.raises(JwksUnavailableError):
        await verifier(_sign())
    results = await asyncio.gather(
        *(verifier(_sign(_claims(jti=f"j{i}"))) for i in range(5)), return_exceptions=True
    )
    assert all(isinstance(r, JwksUnavailableError) for r in results)
    assert jwks.calls == 1


async def test_all_waiters_on_a_shared_failing_fetch_are_unavailable() -> None:
    jwks = _Jwks()
    _failing_until_fixed(jwks)
    jwks.delay = 0.05
    verifier = _verifier(jwks)
    results = await asyncio.gather(
        *(verifier(_sign(_claims(jti=f"j{i}"))) for i in range(10)), return_exceptions=True
    )
    assert all(isinstance(r, JwksUnavailableError) for r in results)
    assert jwks.calls == 1


async def test_a_cancelled_waiter_on_a_failing_fetch_leaves_no_unretrieved_exception() -> None:
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        jwks = _Jwks()
        _failing_until_fixed(jwks)
        jwks.delay = 0.05
        verifier = _verifier(jwks)
        waiter = asyncio.create_task(verifier(_sign()))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await asyncio.sleep(0.1)
        del waiter
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
    assert jwks.calls == 1
    assert reported == []


async def test_an_invalid_jwks_url_is_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    jwks = _Jwks()
    client = JwksClient("https://keycloak.corp.lan:abc/certs", http=jwks.client(), clock=_Clock())
    verifier = KeycloakOidcVerifier(
        issuer=_ISSUER, audience=_AUDIENCE, client_id=_CLIENT_ID, team_map=_TEAM_MAP, jwks=client
    )
    await _rejects(verifier, _sign(), caplog, JwksUnavailableError)
    assert jwks.calls == 0


def _jwks_body_of(size: int) -> bytes:
    body = json.dumps({"keys": [_jwk(_KEY_A, "kid-a")]}).encode()
    assert len(body) <= size
    return body + b" " * (size - len(body))


async def test_jwks_body_at_the_cap_is_accepted() -> None:
    jwks = _Jwks()
    body = _jwks_body_of(oidc_verifier.MAX_JWKS_BYTES)

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    jwks.respond = respond
    assert (await _verifier(jwks)(_sign())).subject == _SUB


async def test_jwks_body_over_the_cap_is_unavailable(caplog: pytest.LogCaptureFixture) -> None:
    jwks = _Jwks()
    body = _jwks_body_of(oidc_verifier.MAX_JWKS_BYTES + 1)

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    jwks.respond = respond
    await _jwks_unavailable(jwks, caplog)


# ── construction ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    ["http://keycloak.corp.lan/certs", "ftp://keycloak.corp.lan/certs", "keycloak/certs"],
)
def test_non_https_jwks_url_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        JwksClient(url, http=httpx.AsyncClient())


def test_http_jwks_url_needs_an_explicit_opt_in() -> None:
    JwksClient("http://keycloak:8080/certs", http=httpx.AsyncClient(), allow_insecure_http=True)


def test_a_missing_cryptography_extra_is_named_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "cryptography", None)
    with pytest.raises(RuntimeError, match="'oidc' extra"):
        JwksClient(_JWKS_URL, http=httpx.AsyncClient())
    with pytest.raises(RuntimeError, match="'oidc' extra"):
        KeycloakOidcVerifier(
            issuer=_ISSUER,
            audience=_AUDIENCE,
            client_id=_CLIENT_ID,
            team_map=_TEAM_MAP,
            jwks=JwksClient.__new__(JwksClient),
        )


def _issuance_settings(**overrides: Any) -> IssuanceSettings:
    base: dict[str, Any] = {
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "client_id": _CLIENT_ID,
        "jwks_url": _JWKS_URL,
        "team_claim": "groups",
        "team_map": _TEAM_MAP,
        "user_claim": "preferred_username",
        "operator_audience": _OPERATOR_AUDIENCE,
        "ca_bundle": None,
        "token_ttl_days": 30,
        "max_active": 2,
        "min_interval_seconds": 600,
        "max_inflight": 4,
        "rate_per_minute": 30,
    }
    base.update(overrides)
    return IssuanceSettings(**base)


class _RecordingClient:
    instances: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        _RecordingClient.instances.append(kwargs)


@pytest.mark.parametrize(
    ("ca_bundle", "verify"), [(None, True), ("/etc/ssl/corp.pem", "/etc/ssl/corp.pem")]
)
def test_from_settings_builds_a_bounded_non_redirecting_client(
    monkeypatch: pytest.MonkeyPatch, ca_bundle: str | None, verify: Any
) -> None:
    _RecordingClient.instances = []
    monkeypatch.setattr(oidc_verifier.httpx, "AsyncClient", _RecordingClient)
    KeycloakOidcVerifier.from_settings(_issuance_settings(ca_bundle=ca_bundle))
    (kwargs,) = _RecordingClient.instances
    assert kwargs["follow_redirects"] is False
    assert kwargs["verify"] == verify
    timeout = kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 3.0
    assert timeout.read == 3.0


def test_from_settings_refuses_an_http_jwks_url() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        KeycloakOidcVerifier.from_settings(
            _issuance_settings(jwks_url="http://keycloak.corp.lan/certs")
        )


@pytest.fixture
def issuance_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in settings.all_keys():
        monkeypatch.delenv(name, raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]\n"/devs/payments" = "payments"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", "http://keycloak:8080/realms/dev")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID", _CLIENT_ID)
    config.reset_cache()
    yield
    config.reset_cache()


async def test_from_settings_accepts_an_http_issuer_that_settings_allowed_outside_prod(
    issuance_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_ENV", "dev")
    resolved = settings.issuance()
    assert resolved is not None
    verifier = KeycloakOidcVerifier.from_settings(resolved)
    await verifier.aclose()


def test_an_http_issuer_in_prod_is_refused_by_settings_before_construction(
    issuance_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_ENV", "prod")
    with pytest.raises(ConfigError, match="CORP_GATEWAY_ISSUE_OIDC_ISSUER"):
        settings.issuance()


async def test_from_settings_verifies_with_the_settings_values() -> None:
    jwks = _Jwks()
    verifier = KeycloakOidcVerifier.from_settings(
        _issuance_settings(team_map=(("/devs/core", "core"),)), http=jwks.client()
    )
    claims = await verifier(_sign(_claims(groups=["/devs/core"])))
    assert claims.team_id == "core"
    await verifier.aclose()
