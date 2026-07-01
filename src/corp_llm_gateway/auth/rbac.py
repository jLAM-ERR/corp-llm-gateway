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

    Raises OperatorDenied for missing, invalid, expired, or unprivileged tokens.
    Raises RuntimeError if PyJWT is not installed.
    Signature + algorithm are read from CORP_GATEWAY_OIDC_KEY / CORP_GATEWAY_OIDC_ALG.
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
    alg: str = config.get("CORP_GATEWAY_OIDC_ALG", "RS256") or "RS256"

    try:
        payload: dict[str, Any] = jwt.decode(token, key, algorithms=[alg])
    except jwt.ExpiredSignatureError:
        raise OperatorDenied from None
    except jwt.InvalidTokenError:
        raise OperatorDenied from None

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
