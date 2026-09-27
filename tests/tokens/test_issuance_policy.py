"""IssuancePolicy: per-(iss, sub) cap, interval and jti replay, driven by IssuanceSettings.

The store-level atomicity (both backends, parallel races) is pinned in
test_token_store_contract.py; this file covers the policy layer on top.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from corp_llm_gateway.settings import IssuanceSettings
from corp_llm_gateway.tokens import (
    AuthMiddleware,
    InMemoryTokenStore,
    OidcClaims,
    OidcVerificationError,
    RevokedTokenError,
    TokenIssuer,
)
from corp_llm_gateway.tokens.errors import IssuancePolicyError
from corp_llm_gateway.tokens.issuance_policy import IssuancePolicy
from corp_llm_gateway.tokens.models import TokenInfo
from corp_llm_gateway.tokens.store import TokenStore

_ISS = "https://kc.corp.test/realms/dev"
_SUB = "f3b1c2d4-subject-alice"
_T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _settings(*, max_active: int = 2, min_interval_seconds: int = 600) -> IssuanceSettings:
    return IssuanceSettings(
        issuer=_ISS,
        audience="corp-gateway-issue",
        client_id="corp-gateway-cli",
        jwks_url=f"{_ISS}/protocol/openid-connect/certs",
        team_claim="groups",
        team_map=(("/eng", "t1"),),
        user_claim="preferred_username",
        operator_audience="corp-gateway-admin",
        ca_bundle=None,
        token_ttl_days=30,
        max_active=max_active,
        min_interval_seconds=min_interval_seconds,
        max_inflight=4,
        rate_per_minute=30,
    )


def _claims(jti: str, *, subject: str = _SUB, issuer: str = _ISS) -> OidcClaims:
    return OidcClaims(
        user_id="alice",
        team_id="t1",
        scopes=("read",),
        issuer=issuer,
        subject=subject,
        jti=jti,
    )


class _Clock:
    def __init__(self, start: datetime = _T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _policy(
    store: TokenStore | None = None, clock: _Clock | None = None, **settings: int
) -> tuple[IssuancePolicy, InMemoryTokenStore | TokenStore, _Clock]:
    store = store if store is not None else InMemoryTokenStore()
    clock = clock or _Clock()
    return IssuancePolicy(store, _settings(**settings), clock=clock), store, clock


async def _revoked(store: TokenStore, corp_token: str) -> bool:
    info = await store.lookup(corp_token)
    assert info is not None
    return info.revoked_at is not None


@pytest.mark.asyncio
async def test_issue_stores_a_token_with_the_configured_ttl() -> None:
    policy, store, _ = _policy()

    result = await policy.issue(_claims("jti-1"))

    assert result.corp_token.startswith("ct_")
    assert result.expires_at == _T0 + timedelta(days=30)
    info = await store.lookup(result.corp_token)
    assert info is not None
    assert (info.user_id, info.team_id, info.scopes) == ("alice", "t1", ("read",))
    assert info.issued_at == _T0
    assert info.revoked_at is None


@pytest.mark.asyncio
async def test_issued_token_authenticates_via_middleware() -> None:
    policy, store, clock = _policy()
    result = await policy.issue(_claims("jti-1"))

    ctx = await AuthMiddleware(store).authenticate(result.corp_token, now=clock.now)

    assert ctx.user_id == "alice"
    assert ctx.team_id == "t1"


@pytest.mark.asyncio
async def test_third_active_token_revokes_the_oldest() -> None:
    policy, store, clock = _policy()
    first = await policy.issue(_claims("jti-1"))
    clock.advance(timedelta(minutes=11))
    second = await policy.issue(_claims("jti-2"))
    clock.advance(timedelta(minutes=11))
    third = await policy.issue(_claims("jti-3"))

    assert await _revoked(store, first.corp_token)
    assert not await _revoked(store, second.corp_token)
    assert not await _revoked(store, third.corp_token)


@pytest.mark.asyncio
async def test_rotated_out_token_no_longer_authenticates() -> None:
    policy, store, clock = _policy(max_active=1)
    first = await policy.issue(_claims("jti-1"))
    clock.advance(timedelta(minutes=11))
    await policy.issue(_claims("jti-2"))

    with pytest.raises(RevokedTokenError):
        await AuthMiddleware(store).authenticate(first.corp_token, now=clock.now)


@pytest.mark.asyncio
async def test_two_issuances_within_the_interval_are_rate_limited() -> None:
    policy, _, clock = _policy()
    await policy.issue(_claims("jti-1"))
    clock.advance(timedelta(seconds=599))

    with pytest.raises(IssuancePolicyError) as exc_info:
        await policy.issue(_claims("jti-2"))

    assert exc_info.value.code == "E_ISSUE_RATE"
    clock.advance(timedelta(seconds=1))
    await policy.issue(_claims("jti-3"))


@pytest.mark.asyncio
async def test_interval_comes_from_settings() -> None:
    policy, _, clock = _policy(min_interval_seconds=60)
    await policy.issue(_claims("jti-1"))
    clock.advance(timedelta(seconds=60))

    await policy.issue(_claims("jti-2"))


@pytest.mark.asyncio
async def test_same_jti_twice_is_a_replay() -> None:
    policy, _, clock = _policy()
    await policy.issue(_claims("jti-same"))
    clock.advance(timedelta(hours=1))

    with pytest.raises(IssuancePolicyError) as exc_info:
        await policy.issue(_claims("jti-same"))

    assert exc_info.value.code == "E_ISSUE_REPLAY"


@pytest.mark.asyncio
async def test_policy_errors_carry_the_code_only() -> None:
    policy, _, _ = _policy()
    await policy.issue(_claims("jti-secret-value"))

    with pytest.raises(IssuancePolicyError) as exc_info:
        await policy.issue(_claims("jti-secret-value"))

    exc = exc_info.value
    assert exc.args == ("E_ISSUE_REPLAY",)
    rendered = f"{exc!s} {exc!r}"
    for value in (_ISS, _SUB, "jti-secret-value", "alice"):
        assert value not in rendered


@pytest.mark.asyncio
async def test_expired_tokens_do_not_count_toward_the_cap() -> None:
    policy, store, clock = _policy(max_active=1)
    first = await policy.issue(_claims("jti-1"))
    clock.advance(timedelta(days=31))

    await policy.issue(_claims("jti-2"))

    assert not await _revoked(store, first.corp_token)


@pytest.mark.asyncio
async def test_cli_issued_tokens_are_ignored_by_the_cap() -> None:
    policy, store, clock = _policy(max_active=1)
    assert isinstance(store, InMemoryTokenStore)
    store.upsert(
        TokenInfo(
            corp_token="ct-cli",
            user_id="alice",
            team_id="t1",
            scopes=(),
            issued_at=_T0 - timedelta(days=1),
            expires_at=_T0 + timedelta(days=29),
        )
    )

    await policy.issue(_claims("jti-1"))
    clock.advance(timedelta(minutes=11))
    await policy.issue(_claims("jti-2"))

    assert not await _revoked(store, "ct-cli")


@pytest.mark.asyncio
async def test_other_subjects_are_not_affected() -> None:
    policy, store, _ = _policy(max_active=1)
    alice = await policy.issue(_claims("jti-a", subject="sub-alice"))

    bob = await policy.issue(_claims("jti-b", subject="sub-bob"))

    assert not await _revoked(store, alice.corp_token)
    assert not await _revoked(store, bob.corp_token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        _claims(""),
        _claims("jti-1", subject=""),
        _claims("jti-1", issuer=""),
    ],
    ids=["no-jti", "no-subject", "no-issuer"],
)
async def test_claims_without_identity_are_refused(claims: OidcClaims) -> None:
    policy, store, _ = _policy()

    with pytest.raises(OidcVerificationError) as exc_info:
        await policy.issue(claims)

    assert exc_info.value.args == ("E_OIDC_CLAIMS",)
    assert await store.list_tokens() == ()


@pytest.mark.asyncio
async def test_store_without_subject_issuance_is_refused_loudly() -> None:
    class _LegacyStore(TokenStore):
        async def lookup(self, corp_token: str) -> TokenInfo | None:
            return None

        async def revoke_user(self, user_id: str) -> int:
            return 0

        async def list_tokens(self, user_id: str | None = None) -> tuple[TokenInfo, ...]:
            return ()

    policy, _, _ = _policy(store=_LegacyStore())

    with pytest.raises(NotImplementedError, match="issue_for_subject"):
        await policy.issue(_claims("jti-1"))


# TokenIssuer delegation -------------------------------------------------------


def _verifier(claims: OidcClaims):
    async def verify(oidc_token: str) -> OidcClaims:
        if oidc_token == "invalid":
            raise OidcVerificationError("E_OIDC_SIGNATURE")
        return claims

    return verify


@pytest.mark.asyncio
async def test_token_issuer_delegates_to_the_policy() -> None:
    policy, store, _ = _policy()
    issuer = TokenIssuer(store, _verifier(_claims("jti-1")), policy=policy)

    result = await issuer.issue("oidc-bearer")

    assert result.expires_at == _T0 + timedelta(days=30)
    assert await store.lookup(result.corp_token) is not None
    with pytest.raises(IssuancePolicyError) as exc_info:
        await issuer.issue("oidc-bearer")
    assert exc_info.value.code == "E_ISSUE_REPLAY"


@pytest.mark.asyncio
async def test_token_issuer_verifies_before_the_policy_runs() -> None:
    policy, store, _ = _policy()
    issuer = TokenIssuer(store, _verifier(_claims("jti-1")), policy=policy)

    with pytest.raises(OidcVerificationError):
        await issuer.issue("invalid")
    with pytest.raises(OidcVerificationError):
        await issuer.issue("")

    assert await store.list_tokens() == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"ttl": timedelta(hours=1)},
        {"ttl": timedelta(days=30)},
        {"token_factory": lambda: "ct_fixed"},
    ],
    ids=["ttl", "default-valued-ttl", "token-factory"],
)
def test_token_issuer_refuses_settings_the_policy_would_ignore(overrides: dict) -> None:
    policy, store, _ = _policy()

    with pytest.raises(ValueError, match="policy"):
        TokenIssuer(store, _verifier(_claims("jti-1")), policy=policy, **overrides)
