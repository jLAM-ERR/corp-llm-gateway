import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from corp_llm_gateway.tokens.errors import (
    ExpiredTokenError,
    InvalidTokenError,
    MissingTokenError,
    RevokedTokenError,
)
from corp_llm_gateway.tokens.models import TokenInfo
from corp_llm_gateway.tokens.store import TokenStore


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    team_id: str
    scopes: tuple[str, ...]


class AuthMiddleware:
    """Validate X-Corp-Auth tokens with a 60s revocation cache.

    Cache hit: TokenInfo as it was at lookup time. A token revoked while
    cached remains valid up to revocation_cache_seconds (60s default).
    This is the documented offboarding lag from the plan's risks table.

    Plan refs: M2-2 (auth middleware), M2-3 (30d TTL), M2-6 (BYOK
    Authorization is forwarded untouched and never logged), M2-7 (token
    not in audit).
    """

    HEADER_NAME = "X-Corp-Auth"
    _HEADER_LOWER = HEADER_NAME.lower()

    def __init__(
        self,
        store: TokenStore,
        *,
        revocation_cache_seconds: float = 60.0,
    ) -> None:
        self._store = store
        self._cache_ttl = float(revocation_cache_seconds)
        self._cache: dict[str, tuple[TokenInfo, float]] = {}
        self._lock = asyncio.Lock()

    async def authenticate(
        self,
        corp_token: str | None,
        *,
        now: datetime | None = None,
    ) -> AuthContext:
        if not corp_token:
            raise MissingTokenError(f"missing {self.HEADER_NAME}")
        info = await self._lookup(corp_token)
        if info is None:
            raise InvalidTokenError("unknown token")
        return self._validate(info, now or datetime.now(UTC))

    async def authenticate_headers(
        self,
        headers: Mapping[str, str],
        *,
        now: datetime | None = None,
    ) -> AuthContext:
        token = self._extract_corp_token(headers)
        return await self.authenticate(token, now=now)

    def strip_corp_token(self, headers: Mapping[str, str]) -> dict[str, str]:
        return {k: v for k, v in headers.items() if k.lower() != self._HEADER_LOWER}

    def _extract_corp_token(self, headers: Mapping[str, str]) -> str | None:
        for k, v in headers.items():
            if k.lower() == self._HEADER_LOWER:
                return v
        return None

    async def _lookup(self, corp_token: str) -> TokenInfo | None:
        now_mono = time.monotonic()
        cached = self._cache.get(corp_token)
        if cached is not None and (now_mono - cached[1]) < self._cache_ttl:
            return cached[0]

        async with self._lock:
            now_mono = time.monotonic()
            cached = self._cache.get(corp_token)
            if cached is not None and (now_mono - cached[1]) < self._cache_ttl:
                return cached[0]
            info = await self._store.lookup(corp_token)
            if info is not None:
                self._cache[corp_token] = (info, now_mono)
            return info

    @staticmethod
    def _validate(info: TokenInfo, now: datetime) -> AuthContext:
        if info.revoked_at is not None and info.revoked_at <= now:
            raise RevokedTokenError("token has been revoked")
        if info.expires_at <= now:
            raise ExpiredTokenError("token has expired")
        return AuthContext(user_id=info.user_id, team_id=info.team_id, scopes=info.scopes)
