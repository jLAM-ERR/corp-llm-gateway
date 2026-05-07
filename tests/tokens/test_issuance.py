from datetime import UTC, datetime, timedelta

import pytest

from corp_llm_gateway.tokens import (
    AuthMiddleware,
    DEFAULT_TOKEN_TTL_DAYS,
    InMemoryTokenStore,
    OidcClaims,
    OidcVerificationError,
    TokenIssuer,
)


def _verifier(claims: OidcClaims):
    async def verify(oidc_token: str) -> OidcClaims:
        if oidc_token == "invalid":
            raise OidcVerificationError("bad signature")
        return claims
    return verify


@pytest.mark.asyncio
async def test_default_ttl_is_30_days() -> None:
    assert DEFAULT_TOKEN_TTL_DAYS == 30


@pytest.mark.asyncio
async def test_issues_token_with_default_ttl() -> None:
    store = InMemoryTokenStore()
    issuer = TokenIssuer(
        store, _verifier(OidcClaims(user_id="alice", team_id="t1"))
    )
    before = datetime.now(UTC)
    result = await issuer.issue("oidc-tok")
    assert result.corp_token.startswith("ct_")
    assert result.expires_at - before >= timedelta(days=29)
    assert result.expires_at - before <= timedelta(days=30, minutes=1)


@pytest.mark.asyncio
async def test_issued_token_lookups_in_store() -> None:
    store = InMemoryTokenStore()
    issuer = TokenIssuer(
        store, _verifier(OidcClaims(user_id="alice", team_id="t1", scopes=("read",)))
    )
    result = await issuer.issue("oidc-tok")
    info = await store.lookup(result.corp_token)
    assert info is not None
    assert info.user_id == "alice"
    assert info.team_id == "t1"
    assert info.scopes == ("read",)


@pytest.mark.asyncio
async def test_issued_token_authenticates_via_middleware() -> None:
    store = InMemoryTokenStore()
    issuer = TokenIssuer(
        store, _verifier(OidcClaims(user_id="alice", team_id="t1"))
    )
    result = await issuer.issue("oidc-tok")
    mw = AuthMiddleware(store)
    ctx = await mw.authenticate(result.corp_token)
    assert ctx.user_id == "alice"


@pytest.mark.asyncio
async def test_missing_oidc_token_rejected() -> None:
    issuer = TokenIssuer(
        InMemoryTokenStore(), _verifier(OidcClaims(user_id="x", team_id="x"))
    )
    with pytest.raises(OidcVerificationError):
        await issuer.issue("")


@pytest.mark.asyncio
async def test_invalid_oidc_token_rejected() -> None:
    issuer = TokenIssuer(
        InMemoryTokenStore(), _verifier(OidcClaims(user_id="x", team_id="x"))
    )
    with pytest.raises(OidcVerificationError):
        await issuer.issue("invalid")


@pytest.mark.asyncio
async def test_custom_ttl_respected() -> None:
    issuer = TokenIssuer(
        InMemoryTokenStore(),
        _verifier(OidcClaims(user_id="x", team_id="x")),
        ttl=timedelta(hours=1),
    )
    before = datetime.now(UTC)
    result = await issuer.issue("oidc-tok")
    assert result.expires_at - before <= timedelta(hours=1, seconds=5)


@pytest.mark.asyncio
async def test_each_issue_returns_unique_token() -> None:
    issuer = TokenIssuer(
        InMemoryTokenStore(), _verifier(OidcClaims(user_id="alice", team_id="t1"))
    )
    r1 = await issuer.issue("oidc-tok")
    r2 = await issuer.issue("oidc-tok")
    assert r1.corp_token != r2.corp_token
