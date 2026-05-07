from corp_llm_gateway.tokens.models import TokenInfo
from corp_llm_gateway.tokens.store import TokenStore


class PostgresTokenStore(TokenStore):
    """Stub. Implement against M0-5 Postgres once the connection URL lands."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    async def lookup(self, corp_token: str) -> TokenInfo | None:
        raise NotImplementedError(
            "PostgresTokenStore stub — implement after M0-5 Postgres is provisioned"
        )

    async def revoke_user(self, user_id: str) -> int:
        raise NotImplementedError(
            "PostgresTokenStore stub — implement after M0-5 Postgres is provisioned"
        )
