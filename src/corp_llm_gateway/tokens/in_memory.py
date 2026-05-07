from datetime import UTC, datetime

from corp_llm_gateway.tokens.models import TokenInfo
from corp_llm_gateway.tokens.store import TokenStore


class InMemoryTokenStore(TokenStore):
    def __init__(self) -> None:
        self._tokens: dict[str, TokenInfo] = {}

    def upsert(self, info: TokenInfo) -> None:
        self._tokens[info.corp_token] = info

    async def lookup(self, corp_token: str) -> TokenInfo | None:
        return self._tokens.get(corp_token)

    async def revoke_user(self, user_id: str) -> int:
        count = 0
        now = datetime.now(UTC)
        for token, info in list(self._tokens.items()):
            if info.user_id == user_id and info.revoked_at is None:
                self._tokens[token] = TokenInfo(
                    corp_token=info.corp_token,
                    user_id=info.user_id,
                    team_id=info.team_id,
                    scopes=info.scopes,
                    issued_at=info.issued_at,
                    expires_at=info.expires_at,
                    revoked_at=now,
                )
                count += 1
        return count
