import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta

from corp_llm_gateway.tokens.errors import IssuancePolicyError
from corp_llm_gateway.tokens.models import TokenInfo
from corp_llm_gateway.tokens.store import TokenStore


class InMemoryTokenStore(TokenStore):
    def __init__(self) -> None:
        self._tokens: dict[str, TokenInfo] = {}
        self._subjects: dict[str, tuple[str, str]] = {}
        self._jtis: set[str] = set()
        self._subject_locks: dict[tuple[str, str], asyncio.Lock] = {}

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

    async def list_tokens(self, user_id: str | None = None) -> tuple[TokenInfo, ...]:
        return tuple(
            info for info in self._tokens.values() if user_id is None or info.user_id == user_id
        )

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
        key = (issuer, subject)
        lock = self._subject_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if jti in self._jtis:
                raise IssuancePolicyError(IssuancePolicyError.REPLAY)
            now = info.issued_at
            held_by_subject = [
                held
                for token, owner in self._subjects.items()
                if owner == key and (held := self._tokens.get(token)) is not None
            ]
            # The interval is an issuance rate: revoked and expired rows count.
            latest = max((held.issued_at for held in held_by_subject), default=None)
            if latest is not None and now - latest < min_interval:
                raise IssuancePolicyError(IssuancePolicyError.RATE)
            if info.corp_token in self._tokens:
                raise RuntimeError("corp_tokens unique violation on issuance")
            active = sorted(
                (
                    held
                    for held in held_by_subject
                    if held.revoked_at is None and held.expires_at > now
                ),
                key=lambda held: (held.issued_at, held.corp_token),
            )
            for held in active[: max(0, len(active) - max_active + 1)]:
                self._tokens[held.corp_token] = dataclasses.replace(held, revoked_at=now)
            stored = dataclasses.replace(info, revoked_at=None)
            self._tokens[stored.corp_token] = stored
            self._subjects[stored.corp_token] = key
            self._jtis.add(jti)
            return stored
