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
async def test_lookup_unknown_returns_none() -> None:
    store = InMemoryTokenStore()
    assert await store.lookup("missing") is None


@pytest.mark.asyncio
async def test_upsert_and_lookup() -> None:
    store = InMemoryTokenStore()
    info = _info("tok-1")
    store.upsert(info)
    assert await store.lookup("tok-1") == info


@pytest.mark.asyncio
async def test_revoke_user_marks_all_their_tokens() -> None:
    store = InMemoryTokenStore()
    store.upsert(_info("tok-1", user_id="alice"))
    store.upsert(_info("tok-2", user_id="alice"))
    store.upsert(_info("tok-3", user_id="bob"))

    n = await store.revoke_user("alice")
    assert n == 2

    a1 = await store.lookup("tok-1")
    a2 = await store.lookup("tok-2")
    b3 = await store.lookup("tok-3")
    assert a1 is not None and a1.revoked_at is not None
    assert a2 is not None and a2.revoked_at is not None
    assert b3 is not None and b3.revoked_at is None


@pytest.mark.asyncio
async def test_revoke_user_idempotent() -> None:
    store = InMemoryTokenStore()
    store.upsert(_info("tok-1", user_id="alice"))
    n1 = await store.revoke_user("alice")
    n2 = await store.revoke_user("alice")
    assert n1 == 1
    assert n2 == 0
