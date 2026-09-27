from abc import ABC, abstractmethod
from datetime import timedelta

from corp_llm_gateway.tokens.models import TokenInfo


class TokenStore(ABC):
    @abstractmethod
    async def lookup(self, corp_token: str) -> TokenInfo | None: ...

    @abstractmethod
    async def revoke_user(self, user_id: str) -> int: ...

    @abstractmethod
    async def list_tokens(self, user_id: str | None = None) -> tuple[TokenInfo, ...]: ...

    async def issue_for_subject(
        self,
        info: TokenInfo,
        *,
        issuer: str,
        subject: str,
        jti: str,
        max_active: int,
        min_interval: timedelta,
    ) -> TokenInfo:
        """Atomically store ``info`` (unrevoked) as a token for the OIDC ``(issuer, subject)``.

        ``info.issued_at`` is "now", from the caller's clock (replicas are assumed
        NTP-synced). In order: a ``jti`` seen before raises
        ``IssuancePolicyError(E_ISSUE_REPLAY)``; any token for the subject issued less
        than ``min_interval`` ago, revoked or expired included, raises
        ``E_ISSUE_RATE`` (the interval is an issuance rate, not a property of the
        active set); otherwise the oldest active tokens are revoked so that, with
        ``info``, exactly ``max_active`` remain. Active means unrevoked and unexpired
        at "now"; rows without OIDC identity (CLI-issued) never count. A store that
        bounds its lock waits raises ``E_ISSUE_BUSY`` when one times out; a
        ``corp_token`` collision raises ``RuntimeError``. Returns the stored row.
        """
        raise NotImplementedError(
            f"TokenStore impl {type(self).__name__} lacks issue_for_subject; "
            "developer token issuance needs InMemoryTokenStore or PostgresTokenStore"
        )
