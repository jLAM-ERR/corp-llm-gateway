from datetime import UTC, datetime, timedelta

import pytest

from corp_llm_gateway.tokens import InMemoryTokenStore, TokenInfo


def _info(corp_token: str, user_id: str = "alice", team_id: str = "t1") -> TokenInfo:
    now = datetime.now(UTC)
    return TokenInfo(
        corp_token=corp_token,
        user_id=user_id,
        team_id=team_id,
        scopes=("read",),
        issued_at=now,
        expires_at=now + timedelta(days=30),
    )


@pytest.mark.asyncio
async def test_upsert_and_lookup() -> None:
    store = InMemoryTokenStore()
    info = _info("tok-1")
    store.upsert(info)
    assert await store.lookup("tok-1") == info
