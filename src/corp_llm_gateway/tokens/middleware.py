import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from corp_llm_gateway.tokens.errors import (
    ExpiredTokenError,
    InvalidTokenError,
    MissingTokenError,
    RevokedTokenError,
)
from corp_llm_gateway.tokens.in_memory import InMemoryTokenStore
from corp_llm_gateway.tokens.models import TokenInfo
from corp_llm_gateway.tokens.store import TokenStore

_log = logging.getLogger(__name__)

_DEV_TEAM_ID = "local-dev"
_DEV_TOKEN_TTL_DAYS = 365


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


def _seed_dev_team_token(store: InMemoryTokenStore, token: str) -> None:
    """Seed CORP_LLM_DEV_TEAM_TOKEN into an in-memory store for team ``local-dev``.

    DEV-ONLY: the supported equivalent of the demo shim's seeding
    (``_demo_guardrail.py``), gated by the double guard in
    :func:`make_auth_middleware` (no Postgres DSN, non-prod ``CORP_ENV``).
    """
    now = datetime.now(UTC)
    store.upsert(
        TokenInfo(
            corp_token=token,
            user_id="local-dev",
            team_id=_DEV_TEAM_ID,
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=_DEV_TOKEN_TTL_DAYS),
        )
    )
    _log.info("seeded CORP_LLM_DEV_TEAM_TOKEN for team=%s (dev-only)", _DEV_TEAM_ID)


def make_auth_middleware(*, revocation_cache_seconds: float = 60.0) -> AuthMiddleware:
    """Build AuthMiddleware from config.

    Uses PostgresTokenStore when CORP_LLM_PG_DSN is configured; raises
    RuntimeError if the DSN is set but asyncpg is absent (misconfiguration).
    Falls back to InMemoryTokenStore when no DSN is configured (dev/demo) —
    if CORP_LLM_DEV_TEAM_TOKEN is also set, it seeds a working X-Corp-Auth for
    team "local-dev" unless CORP_ENV marks a production deployment (double
    guard: never seeds against Postgres, never seeds in prod).
    """
    from corp_llm_gateway.config import get as _get_cfg
    from corp_llm_gateway.config import is_prod

    dsn = _get_cfg("CORP_LLM_PG_DSN")
    dev_token = _get_cfg("CORP_LLM_DEV_TEAM_TOKEN", "") or ""
    store: TokenStore
    if dsn:
        from corp_llm_gateway.tokens.postgres_store import PostgresTokenStore

        store = PostgresTokenStore(dsn)  # raises RuntimeError if asyncpg absent
        if dev_token:
            _log.warning(
                "CORP_LLM_DEV_TEAM_TOKEN is set but ignored: CORP_LLM_PG_DSN is "
                "configured — the dev-only token seam only applies to the "
                "in-memory token store"
            )
    else:
        mem_store = InMemoryTokenStore()
        if dev_token:
            if is_prod():
                _log.warning(
                    "CORP_LLM_DEV_TEAM_TOKEN is set but ignored: CORP_ENV marks a "
                    "production deployment — the dev-only token seam is refused in prod"
                )
            else:
                _seed_dev_team_token(mem_store, dev_token)
        store = mem_store
    return AuthMiddleware(store, revocation_cache_seconds=revocation_cache_seconds)
