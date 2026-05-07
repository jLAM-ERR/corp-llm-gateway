"""Token issuance — `/internal/issue-token` endpoint logic (M2-3).

The HTTP layer is provided by LiteLLM/FastAPI in M1-7; this module
contains the framework-free issuance logic so it's unit-testable in
isolation.

Flow per the plan:
  1. Developer's install.sh runs Keycloak device-flow → gets OIDC token.
  2. install.sh POSTs OIDC token to /internal/issue-token.
  3. This module validates the OIDC token and issues a 30-day corp token.

OIDC validation is delegated to a `OidcVerifier` callable that the host
wires up — this keeps the issuance code free of any specific OIDC SDK
choice.
"""
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from corp_llm_gateway.tokens.models import TokenInfo
from corp_llm_gateway.tokens.store import TokenStore

DEFAULT_TOKEN_TTL_DAYS = 30


@dataclass(frozen=True)
class OidcClaims:
    user_id: str
    team_id: str
    scopes: tuple[str, ...] = field(default_factory=tuple)


OidcVerifier = Callable[[str], Awaitable[OidcClaims]]


class OidcVerificationError(Exception):
    pass


@dataclass(frozen=True)
class IssueResult:
    corp_token: str
    expires_at: datetime


class TokenIssuer:
    def __init__(
        self,
        store: TokenStore,
        verifier: OidcVerifier,
        *,
        ttl: timedelta = timedelta(days=DEFAULT_TOKEN_TTL_DAYS),
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._verifier = verifier
        self._ttl = ttl
        self._token_factory = token_factory or _default_token_factory

    async def issue(self, oidc_token: str) -> IssueResult:
        if not oidc_token:
            raise OidcVerificationError("missing OIDC token")
        claims = await self._verifier(oidc_token)
        now = datetime.now(UTC)
        corp_token = self._token_factory()
        info = TokenInfo(
            corp_token=corp_token,
            user_id=claims.user_id,
            team_id=claims.team_id,
            scopes=claims.scopes,
            issued_at=now,
            expires_at=now + self._ttl,
        )
        await _store_upsert(self._store, info)
        return IssueResult(corp_token=corp_token, expires_at=info.expires_at)


def _default_token_factory() -> str:
    return f"ct_{secrets.token_urlsafe(32)}"


async def _store_upsert(store: TokenStore, info: TokenInfo) -> None:
    upsert = getattr(store, "upsert", None)
    if upsert is None:
        raise NotImplementedError(
            f"TokenStore impl {type(store).__name__} lacks upsert; "
            "issuance requires a store with insert capability"
        )
    upsert(info)
