"""RBAC enforcement for the gateway-admin CLI.

Verifies that the caller holds the ``gateway:operator`` Keycloak claim
before executing admin mutations.
"""

from __future__ import annotations

from typing import Any

from corp_llm_gateway import config


class OperatorDenied(Exception):  # noqa: N818 — intentional name; part of public CLI contract
    """Raised when the caller lacks the gateway:operator claim."""


def verify_operator(token: str) -> None:
    """Decode *token* and assert it carries gateway:operator.

    Raises OperatorDenied for missing, invalid, expired, wrong-aud/iss, or
    unprivileged tokens, and for an unverifiable config (empty signing key or
    unset audience/issuer). Raises RuntimeError if PyJWT / cryptography is absent.

    Verification is pinned to RS256 (F11): a symmetric HS256 token — forgeable
    with an empty or leaked key — is rejected. The public key is read from
    CORP_GATEWAY_OIDC_KEY; the expected claims from CORP_GATEWAY_OIDC_AUDIENCE /
    CORP_GATEWAY_OIDC_ISSUER, both of which MUST be present and match.
    CORP_GATEWAY_OIDC_ALG is no longer honored — RS256 is enforced.
    """
    try:
        import jwt
    except ImportError as exc:
        raise RuntimeError(
            "pyjwt>=2.8 is required for RBAC enforcement; install the 'oidc' extra"
        ) from exc

    if not token:
        raise OperatorDenied

    key: str = config.get("CORP_GATEWAY_OIDC_KEY", "") or ""
    audience: str = config.get("CORP_GATEWAY_OIDC_AUDIENCE", "") or ""
    issuer: str = config.get("CORP_GATEWAY_OIDC_ISSUER", "") or ""
    # Fail closed: an empty key or unset aud/iss can't verify an operator. An
    # empty key + HS256 was the forgeable path (F11).
    if not key or not audience or not issuer:
        raise OperatorDenied

    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "aud", "iss"]},
        )
    except jwt.PyJWTError:
        raise OperatorDenied from None
    except NotImplementedError as exc:
        # RS256 verification needs `cryptography` (the 'oidc' extra). Refuse
        # rather than silently fall back to a weaker algorithm.
        raise RuntimeError(
            "RS256 RBAC verification requires 'cryptography'; install the 'oidc' extra"
        ) from exc

    if not _has_operator_role(payload):
        raise OperatorDenied


def _has_operator_role(payload: dict[str, Any]) -> bool:
    # Keycloak: realm_access.roles
    realm_access = payload.get("realm_access")
    if isinstance(realm_access, dict):
        roles = realm_access.get("roles", [])
        if isinstance(roles, list) and "gateway:operator" in roles:
            return True

    # flat roles list
    flat = payload.get("roles")
    if isinstance(flat, list) and "gateway:operator" in flat:
        return True

    # space-separated scope string
    scope = payload.get("scope")
    return isinstance(scope, str) and "gateway:operator" in scope.split()


def get_admin_token(cli_token: str | None = None) -> str:
    """Return the operator token: CLI arg wins, then CORP_GATEWAY_ADMIN_TOKEN env/file."""
    if cli_token:
        return cli_token
    return config.get("CORP_GATEWAY_ADMIN_TOKEN", "") or ""
