"""KeycloakOidcVerifier + JwksClient — the issuance-side OIDC contract.

Signs real RS256 tokens and serves the JWKS through ``httpx.MockTransport``, so
the module needs `cryptography` (the 'oidc' extra) — it skips on the
graceful-degradation venv that lacks it.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
import traceback
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import httpx
import jwt
import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from corp_llm_gateway.settings import IssuanceSettings
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
    headers: dict[str, Any] = {}
    if kid is not None:
        headers["kid"] = kid
    if typ is not None:
        headers["typ"] = typ
    token = jwt.encode(claims if claims is not None else _claims(), key, "RS256", headers=headers)
    if typ is None:
        return _rewrite_header(token, lambda h: h.pop("typ", None))
    return token


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _rewrite_header(token: str, edit: Callable[[dict[str, Any]], Any]) -> str:
    head, payload, sig = token.split(".")
    header = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    edit(header)
    return ".".join([_b64(json.dumps(header).encode()), payload, sig])


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
    token = _rewrite_header(_sign(), lambda h: h.__setitem__("typ", typ))
    if typ is None:
        token = _rewrite_header(_sign(), lambda h: h.pop("typ"))
    await _rejects(_verifier(_Jwks()), token, caplog)


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


async def test_failed_fetch_does_not_start_the_cooldown() -> None:
    jwks = _Jwks()
    failing = True

    async def respond(request: httpx.Request) -> httpx.Response:
        if failing:
            return httpx.Response(503)
        return httpx.Response(200, json={"keys": jwks.keys})

    jwks.respond = respond
    verifier = _verifier(jwks)
    with pytest.raises(JwksUnavailableError):
        await verifier(_sign())
    failing = False
    assert (await verifier(_sign())).subject == _SUB
    assert jwks.calls == 2


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


async def test_from_settings_verifies_with_the_settings_values() -> None:
    jwks = _Jwks()
    verifier = KeycloakOidcVerifier.from_settings(
        _issuance_settings(team_map=(("/devs/core", "core"),)), http=jwks.client()
    )
    claims = await verifier(_sign(_claims(groups=["/devs/core"])))
    assert claims.team_id == "core"
    await verifier.aclose()
