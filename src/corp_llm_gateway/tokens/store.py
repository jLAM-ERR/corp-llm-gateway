from abc import ABC, abstractmethod

from corp_llm_gateway.tokens.models import TokenInfo


class TokenStore(ABC):
    @abstractmethod
    async def lookup(self, corp_token: str) -> TokenInfo | None: ...

    @abstractmethod
    async def revoke_user(self, user_id: str) -> int: ...

    @abstractmethod
    async def list_tokens(self, user_id: str | None = None) -> tuple[TokenInfo, ...]: ...
