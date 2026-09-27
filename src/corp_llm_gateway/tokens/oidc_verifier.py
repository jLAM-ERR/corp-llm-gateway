"""Keycloak access-token verifier for ``POST /internal/issue-token``.

Satisfies the ``OidcVerifier`` contract in ``tokens/issuance.py``. The JWKS is
fetched by our own async client (not ``PyJWKClient``, which blocks the loop):
HTTPS only, no redirects, a hard total timeout, one shared in-flight refresh,
and a cooldown so unknown ``kid`` values cannot drive refetches.

Failures raise with an error code only; the token, ``sub``, groups and other
claim values never reach exception args, tracebacks or logs (M1-14).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from corp_llm_gateway.tokens.issuance import (
    JwksUnavailableError,
    OidcClaims,
    OidcTeamMappingError,
    OidcVerificationError,
)

if TYPE_CHECKING:
    from corp_llm_gateway.settings import IssuanceSettings

_log = logging.getLogger(__name__)

JWKS_TIMEOUT_S = 3.0
UNKNOWN_KID_COOLDOWN_S = 60.0
LEEWAY_S = 60

_ALGORITHM = "RS256"
_REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub", "jti", "azp")
_ALLOWED_TYP = frozenset({"jwt", "at+jwt"})


def _require_https(url: str, allow_insecure_http: bool) -> None:
    parts = urlsplit(url)
    allowed = {"https", "http"} if allow_insecure_http else {"https"}
    if parts.scheme.lower() not in allowed or not parts.netloc:
        raise ValueError("JWKS URL must be an absolute HTTPS URL")


class JwksClient:
    """Async JWKS cache keyed by ``kid`` with single-flight refresh."""

    def __init__(
        self,
        url: str,
        *,
        http: httpx.AsyncClient | None = None,
        verify: bool | str = True,
        timeout_s: float = JWKS_TIMEOUT_S,
        cooldown_s: float = UNKNOWN_KID_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
        allow_insecure_http: bool = False,
    ) -> None:
        _require_https(url, allow_insecure_http)
        self._url = url
        self._timeout_s = timeout_s
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s), verify=verify, follow_redirects=False
        )
        self._keys: dict[str, Any] = {}
        self._refreshed_at: float | None = None
        self._inflight: asyncio.Task[None] | None = None

    async def key_for(self, kid: str) -> Any:
        key = self._keys.get(kid)
        if key is not None:
            return key
        if self._inflight is None and self._in_cooldown():
            raise OidcVerificationError("E_OIDC_UNKNOWN_KID")
        await self._refresh()
        key = self._keys.get(kid)
        if key is None:
            raise OidcVerificationError("E_OIDC_UNKNOWN_KID")
        return key

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _in_cooldown(self) -> bool:
        return (
            self._refreshed_at is not None and self._clock() - self._refreshed_at < self._cooldown_s
        )

    async def _refresh(self) -> None:
        task = self._inflight
        if task is None:
            task = asyncio.get_running_loop().create_task(self._fetch())
            task.add_done_callback(self._clear_inflight)
            self._inflight = task
        # shield: one caller's cancellation must not cancel the fetch others await.
        await asyncio.shield(task)

    def _clear_inflight(self, task: asyncio.Task[None]) -> None:
        if self._inflight is task:
            self._inflight = None
        if not task.cancelled():
            task.exception()

    async def _fetch(self) -> None:
        try:
            async with asyncio.timeout(self._timeout_s):
                resp = await self._http.get(self._url, follow_redirects=False)
        except (httpx.HTTPError, TimeoutError) as exc:
            _log.warning("issuance JWKS fetch failed: %s", type(exc).__name__)
            raise JwksUnavailableError("E_JWKS_UNAVAILABLE") from None
        if resp.status_code != 200:
            _log.warning("issuance JWKS fetch failed: HTTP %d", resp.status_code)
            raise JwksUnavailableError("E_JWKS_UNAVAILABLE")
        keys = _parse_jwks(resp)
        if not keys:
            _log.warning("issuance JWKS has no usable RS256 signing key")
            raise JwksUnavailableError("E_JWKS_UNAVAILABLE")
        self._keys = keys
        self._refreshed_at = self._clock()


def _parse_jwks(resp: httpx.Response) -> dict[str, Any]:
    import jwt

    try:
        document = resp.json()
    except ValueError:
        return {}
    entries = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return {}
    keys: dict[str, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kty") != "RSA":
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            continue
        if entry.get("use", "sig") != "sig" or entry.get("alg", _ALGORITHM) != _ALGORITHM:
            continue
        try:
            keys[kid] = jwt.PyJWK(entry, algorithm=_ALGORITHM).key
        except (jwt.PyJWTError, ValueError, TypeError):
            continue
    return keys


class KeycloakOidcVerifier:
    """Verify a Keycloak access JWT and map it to issuance claims."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        client_id: str,
        team_map: Sequence[tuple[str, str]],
        jwks: JwksClient,
        team_claim: str = "groups",
        user_claim: str = "preferred_username",
        operator_audience: str = "",
        leeway_s: int = LEEWAY_S,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._client_id = client_id
        self._team_map = tuple(team_map)
        self._jwks = jwks
        self._team_claim = team_claim
        self._user_claim = user_claim
        self._operator_audience = operator_audience
        self._leeway_s = leeway_s

    @classmethod
    def from_settings(
        cls,
        settings: IssuanceSettings,
        *,
        http: httpx.AsyncClient | None = None,
        allow_insecure_http: bool = False,
    ) -> KeycloakOidcVerifier:
        jwks = JwksClient(
            settings.jwks_url,
            http=http,
            verify=settings.ca_bundle or True,
            allow_insecure_http=allow_insecure_http,
        )
        return cls(
            issuer=settings.issuer,
            audience=settings.audience,
            client_id=settings.client_id,
            team_map=settings.team_map,
            jwks=jwks,
            team_claim=settings.team_claim,
            user_claim=settings.user_claim,
            operator_audience=settings.operator_audience,
        )

    async def aclose(self) -> None:
        await self._jwks.aclose()

    async def __call__(self, token: str) -> OidcClaims:
        try:
            return await self._verify(token)
        except (OidcVerificationError, OidcTeamMappingError) as exc:
            _log.info("issuance token rejected: %s", exc.args[0] if exc.args else "")
            raise

    async def _verify(self, token: str) -> OidcClaims:
        import jwt

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise OidcVerificationError("E_OIDC_MALFORMED") from None
        if header.get("alg") != _ALGORITHM:
            raise OidcVerificationError("E_OIDC_ALG")
        typ = header.get("typ")
        if not isinstance(typ, str) or typ.lower() not in _ALLOWED_TYP:
            raise OidcVerificationError("E_OIDC_TYP")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise OidcVerificationError("E_OIDC_KID")
        key = await self._jwks.key_for(kid)
        claims = _decode(
            token,
            key,
            audience=self._audience,
            issuer=self._issuer,
            leeway=self._leeway_s,
        )
        if claims.get("azp") != self._client_id:
            raise OidcVerificationError("E_OIDC_AZP")
        aud = claims["aud"]
        audiences = [aud] if isinstance(aud, str) else aud
        if self._operator_audience and self._operator_audience in audiences:
            raise OidcVerificationError("E_OIDC_OPERATOR_AUDIENCE")
        subject, jti = claims["sub"], claims["jti"]
        if not (isinstance(subject, str) and subject and isinstance(jti, str) and jti):
            raise OidcVerificationError("E_OIDC_CLAIM_TYPE")
        user = claims.get(self._user_claim)
        return OidcClaims(
            user_id=user if isinstance(user, str) and user else subject,
            team_id=self._team_for(claims.get(self._team_claim)),
            issuer=claims["iss"],
            subject=subject,
            jti=jti,
        )

    def _team_for(self, groups: object) -> str:
        if groups is None:
            raise OidcTeamMappingError("E_ISSUE_NO_TEAM")
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            raise OidcVerificationError("E_OIDC_CLAIM_TYPE")
        member_of = set(groups)
        for group, team_id in self._team_map:
            if group in member_of:
                return team_id
        raise OidcTeamMappingError("E_ISSUE_NO_TEAM")


def _decode(token: str, key: Any, *, audience: str, issuer: str, leeway: int) -> dict[str, Any]:
    import jwt

    try:
        return jwt.decode(
            token,
            key,
            algorithms=[_ALGORITHM],
            audience=audience,
            issuer=issuer,
            options={"require": list(_REQUIRED_CLAIMS)},
            leeway=leeway,
        )
    except jwt.ExpiredSignatureError:
        code = "E_OIDC_EXPIRED"
    except jwt.ImmatureSignatureError:
        code = "E_OIDC_NOT_YET_VALID"
    except jwt.MissingRequiredClaimError:
        code = "E_OIDC_MISSING_CLAIM"
    except jwt.InvalidAudienceError:
        code = "E_OIDC_AUDIENCE"
    except jwt.InvalidIssuerError:
        code = "E_OIDC_ISSUER"
    except jwt.InvalidSignatureError:
        code = "E_OIDC_SIGNATURE"
    except jwt.PyJWTError:
        code = "E_OIDC_INVALID"
    raise OidcVerificationError(code)
