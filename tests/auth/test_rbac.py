"""verify_operator RBAC hardening (F11).

RS256 is pinned and aud/iss are verified. HS256, forged signatures, wrong /
missing aud/iss, and an empty key are all rejected. These sign real RS256
tokens, so the module needs `cryptography` (the 'oidc' extra) — it skips on the
graceful-degradation venv that lacks it.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest

from corp_llm_gateway import config
from corp_llm_gateway.auth.rbac import OperatorDenied, verify_operator

pytest.importorskip("cryptography")

_AUDIENCE = "corp-llm-gateway"
_ISSUER = "https://keycloak.corp.lan/realms/corp"


def _keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv, pub


_PRIV_PEM, _PUB_PEM = _keypair()
_OTHER_PRIV_PEM, _ = _keypair()  # for forged-signature tokens


def _sign(
    priv_pem: str,
    *,
    alg: str = "RS256",
    roles: list[str] | None = None,
    flat_roles: list[str] | None = None,
    scope: str | None = None,
    aud: str | None = _AUDIENCE,
    iss: str | None = _ISSUER,
    exp_offset: int = 3600,
) -> str:
    payload: dict[str, Any] = {"sub": "op", "exp": int(time.time()) + exp_offset}
    if aud is not None:
        payload["aud"] = aud
    if iss is not None:
        payload["iss"] = iss
    if roles is not None:
        payload["realm_access"] = {"roles": roles}
    if flat_roles is not None:
        payload["roles"] = flat_roles
    if scope is not None:
        payload["scope"] = scope
    return jwt.encode(payload, priv_pem, algorithm=alg)


@pytest.fixture(autouse=True)
def _oidc_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    empty = tmp_path / "config.toml"
    empty.write_text("")
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(empty))
    monkeypatch.setenv("CORP_GATEWAY_OIDC_KEY", _PUB_PEM)
    monkeypatch.setenv("CORP_GATEWAY_OIDC_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("CORP_GATEWAY_OIDC_ISSUER", _ISSUER)
    config.reset_cache()
    yield
    config.reset_cache()


# ── accepted ─────────────────────────────────────────────────────────────────


def test_valid_rs256_operator_via_realm_access() -> None:
    verify_operator(_sign(_PRIV_PEM, roles=["gateway:operator"]))


def test_valid_rs256_operator_via_flat_roles() -> None:
    verify_operator(_sign(_PRIV_PEM, flat_roles=["gateway:operator"]))


def test_valid_rs256_operator_via_scope() -> None:
    verify_operator(_sign(_PRIV_PEM, scope="openid gateway:operator profile"))


# ── rejected: forgery / algorithm / key ──────────────────────────────────────


def test_hs256_token_rejected() -> None:
    secret = "shared-secret-key-that-is-long-enough-32b"
    tok = _sign(secret, alg="HS256", roles=["gateway:operator"])
    with pytest.raises(OperatorDenied):
        verify_operator(tok)


def test_forged_signature_rejected() -> None:
    tok = _sign(_OTHER_PRIV_PEM, roles=["gateway:operator"])
    with pytest.raises(OperatorDenied):
        verify_operator(tok)


def test_empty_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_GATEWAY_OIDC_KEY", "")
    with pytest.raises(OperatorDenied):
        verify_operator(_sign(_PRIV_PEM, roles=["gateway:operator"]))


# ── rejected: audience / issuer ──────────────────────────────────────────────


def test_wrong_audience_rejected() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator(_sign(_PRIV_PEM, roles=["gateway:operator"], aud="someone-else"))


def test_wrong_issuer_rejected() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator(_sign(_PRIV_PEM, roles=["gateway:operator"], iss="https://evil"))


def test_missing_audience_rejected() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator(_sign(_PRIV_PEM, roles=["gateway:operator"], aud=None))


def test_missing_issuer_rejected() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator(_sign(_PRIV_PEM, roles=["gateway:operator"], iss=None))


def test_unset_audience_config_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORP_GATEWAY_OIDC_AUDIENCE", raising=False)
    with pytest.raises(OperatorDenied):
        verify_operator(_sign(_PRIV_PEM, roles=["gateway:operator"]))


# ── rejected: expiry / role / shape ──────────────────────────────────────────


def test_expired_token_rejected() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator(_sign(_PRIV_PEM, roles=["gateway:operator"], exp_offset=-10))


def test_missing_role_rejected() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator(_sign(_PRIV_PEM, roles=["some:other-role"]))


def test_empty_token_rejected() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator("")


def test_malformed_token_rejected() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator("not.a.valid.jwt")
