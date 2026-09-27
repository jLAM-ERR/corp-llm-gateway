"""Per-subject minting policy for developer tokens (``POST /internal/issue-token``).

At most ``max_active`` live corp tokens per verified ``(iss, sub)`` (issuing
beyond it revokes the oldest), at most one issuance per ``min_interval_seconds``,
and each Keycloak ``jti`` mints once. The store enforces all three atomically
(``TokenStore.issue_for_subject``); this layer maps settings and claims onto it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from corp_llm_gateway.tokens.issuance import (
    IssueResult,
    OidcClaims,
    OidcVerificationError,
    default_token_factory,
)
from corp_llm_gateway.tokens.models import TokenInfo
from corp_llm_gateway.tokens.store import TokenStore

if TYPE_CHECKING:
    from corp_llm_gateway.settings import IssuanceSettings


def _utc_now() -> datetime:
    return datetime.now(UTC)


class IssuancePolicy:
    def __init__(
        self,
        store: TokenStore,
        settings: IssuanceSettings,
        *,
        clock: Callable[[], datetime] = _utc_now,
        token_factory: Callable[[], str] = default_token_factory,
    ) -> None:
        self._store = store
        self._ttl = timedelta(days=settings.token_ttl_days)
        self._max_active = settings.max_active
        self._min_interval = timedelta(seconds=settings.min_interval_seconds)
        self._clock = clock
        self._token_factory = token_factory

    async def issue(self, claims: OidcClaims) -> IssueResult:
        """Mint a corp token for ``claims``; raises ``IssuancePolicyError`` on rate/replay."""
        if not (claims.issuer and claims.subject and claims.jti):
            raise OidcVerificationError("E_OIDC_CLAIMS")
        now = self._clock()
        info = TokenInfo(
            corp_token=self._token_factory(),
            user_id=claims.user_id,
            team_id=claims.team_id,
            scopes=claims.scopes,
            issued_at=now,
            expires_at=now + self._ttl,
        )
        stored = await self._store.issue_for_subject(
            info,
            issuer=claims.issuer,
            subject=claims.subject,
            jti=claims.jti,
            max_active=self._max_active,
            min_interval=self._min_interval,
        )
        return IssueResult(corp_token=stored.corp_token, expires_at=stored.expires_at)
