"""Profile-aware orchestrator wrapper (D4) — activate profiles at request time.

Layers the profile architecture over the core ``SanitizationOrchestrator``
WITHOUT touching its class body. For a ``team_id`` it resolves the merged
``ProfileBundle`` (D1/D2 ``resolve_team`` + ``PolicyKnobs.merge`` — never
re-implemented here), memoizes ONE inner orchestrator per resolved layer-key,
and delegates ``sanitize()`` to it while folding the D3 ``bundle_fingerprint``
into Cache A so a result never bleeds across profiles. Empty ``profile_ids`` →
the core orchestrator + no fingerprint (byte-identical to today).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from corp_llm_gateway.payload import OVERSIZE_FAIL_CLOSED
from corp_llm_gateway.profiles import (
    PolicyKnobs,
    ProfileCycleError,
    ProfileDepthError,
    ProfileIntegrityError,
    ProfileNotFoundError,
    ProfileParseError,
    bundle_fingerprint,
)
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer.orchestrator import SanitizationOrchestrator
from corp_llm_gateway.team_config import TeamConfig, TeamNotFoundError

if TYPE_CHECKING:
    from collections.abc import Callable

    from corp_llm_gateway.corp_llm import CorpLlmClient
    from corp_llm_gateway.profiles import ProfileBundle, ProfileResolver
    from corp_llm_gateway.sanitizer.engine import CorpLlmSanitizer
    from corp_llm_gateway.sanitizer.orchestrator import SanitizeResult
    from corp_llm_gateway.storage import MappingStore
    from corp_llm_gateway.team_config import TeamConfigStore

    InnerBuilder = Callable[[ProfileBundle], SanitizationOrchestrator]

# Any profile-resolution failure is fail-closed (invariant 6): a misconfigured
# profile must never fall through to un-profiled egress. The hook catches these.
PROFILE_ERRORS: tuple[type[Exception], ...] = (
    ProfileCycleError,
    ProfileDepthError,
    ProfileIntegrityError,
    ProfileNotFoundError,
    ProfileParseError,
)

# Mirror SanitizationOrchestrator's own TTL defaults so an inner orchestrator
# built here dedups on the same horizon as a plain one.
_DEFAULT_CACHE_A_TTL_SECONDS = 36000
_DEFAULT_CACHE_B_TTL_SECONDS = 3600


@dataclass(frozen=True)
class ResolvedProfile:
    """A team's resolved profile: the inner orchestrator to sanitize with, its
    D3 cache-key fingerprint, the merged ``PolicyKnobs`` the hook enforces, and
    the ordered layer-key for the audit trail."""

    policy: PolicyKnobs
    fingerprint: str | None
    orchestrator: SanitizationOrchestrator
    profile_ids: tuple[str, ...]

    async def sanitize(self, text: str, *, team_id: str, conversation_id: str) -> SanitizeResult:
        return await self.orchestrator.sanitize(
            text,
            team_id=team_id,
            conversation_id=conversation_id,
            profile_fingerprint=self.fingerprint,
        )


def passthrough_resolved(orchestrator: SanitizationOrchestrator) -> ResolvedProfile:
    """The no-profile resolution: default policy, no fingerprint — today's behavior."""
    return ResolvedProfile(
        policy=PolicyKnobs(),
        fingerprint=None,
        orchestrator=orchestrator,
        profile_ids=(),
    )


class _LayeredRulesLoader(RulesLoader):
    """Team ``replace.md`` rules + the resolved bundle's rules, concatenated.

    Concatenation only ADDS redaction rules (monotone-tightening): profiles layer
    OVER the team's own rules, never remove them. The team's rules load per
    request by ``team_id``, so ONE inner orchestrator (memoized by layer-key)
    still serves every team that resolves to these layers.
    """

    def __init__(self, base: RulesLoader, profile_rules: Rules) -> None:
        self._base = base
        self._profile_rules = profile_rules

    async def load(self, team_id: str) -> Rules:
        base = await self._base.load(team_id)
        return Rules(rules=base.rules + self._profile_rules.rules)


def build_inner_orchestrator(
    bundle: ProfileBundle,
    *,
    corp_llm: CorpLlmClient | None,
    mapping_store: MappingStore,
    base_rules_loader: RulesLoader,
    sanitizer: CorpLlmSanitizer | None = None,
    cache_a_ttl_seconds: int = _DEFAULT_CACHE_A_TTL_SECONDS,
    cache_b_ttl_seconds: int = _DEFAULT_CACHE_B_TTL_SECONDS,
    oversize_policy: str = OVERSIZE_FAIL_CLOSED,
    oversize_deliver_teams: frozenset[str] = frozenset(),
    oracle_enabled: bool = True,
) -> SanitizationOrchestrator:
    """Construct the inner orchestrator for one resolved bundle.

    Maps the bundle's detectors/gazetteer/rules/allowlist/policy onto the
    unchanged ``SanitizationOrchestrator`` constructor and shares the corp-LLM
    client + mapping store (Cache A) with every other profile — the per-profile
    fingerprint keeps their shared Cache-A entries apart (D3). ``oracle_enabled``
    mirrors the core orchestrator's switch (CORP_LLM_ORACLE_ENABLED) — a
    disabled oracle means every profile's inner orchestrator is also client-less.
    """
    return SanitizationOrchestrator(
        corp_llm,
        mapping_store,
        _LayeredRulesLoader(base_rules_loader, bundle.rules),
        sanitizer=sanitizer,
        cache_a_ttl_seconds=cache_a_ttl_seconds,
        cache_b_ttl_seconds=cache_b_ttl_seconds,
        size_threshold_bytes=bundle.policy.size_threshold_bytes,
        oversize_policy=oversize_policy,
        oversize_deliver_teams=oversize_deliver_teams,
        local_detectors=list(bundle.detectors) or None,
        gazetteer=bundle.gazetteer,
        allowlist=bundle.allowlist,
        oracle_enabled=oracle_enabled,
    )


class ProfileAwareOrchestrator:
    """Request-time profile activation over a core ``SanitizationOrchestrator``.

    ``resolve(team_id)`` fetches the ``TeamConfig``, resolves its merged bundle,
    and (for a non-empty profile set) memoizes ONE inner orchestrator per
    resolved layer-key. ``sanitize()`` is a drop-in for the core orchestrator's
    that routes through the resolved inner one with the D3 fingerprint. Unknown
    teams and empty ``profile_ids`` fall through to the core orchestrator.
    """

    def __init__(
        self,
        core: SanitizationOrchestrator,
        *,
        team_store: TeamConfigStore,
        resolver: ProfileResolver,
        build_inner: InnerBuilder,
    ) -> None:
        self._core = core
        self._team_store = team_store
        self._resolver = resolver
        self._build_inner = build_inner
        self._inner_by_key: dict[tuple[str, ...], SanitizationOrchestrator] = {}
        self._fingerprint_by_key: dict[tuple[str, ...], str] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, team_id: str) -> ResolvedProfile:
        config = await self._team_config(team_id)
        bundle = await self._resolver.resolve_team(config)
        if not bundle.profile_ids:
            return passthrough_resolved(self._core)
        key = bundle.profile_ids
        inner = self._inner_by_key.get(key)
        if inner is None:
            inner = await self._build_and_cache(key, bundle)
        return ResolvedProfile(
            policy=bundle.policy,
            fingerprint=self._fingerprint_by_key[key],
            orchestrator=inner,
            profile_ids=bundle.profile_ids,
        )

    async def sanitize(self, text: str, *, team_id: str, conversation_id: str) -> SanitizeResult:
        resolved = await self.resolve(team_id)
        return await resolved.sanitize(text, team_id=team_id, conversation_id=conversation_id)

    async def _build_and_cache(
        self, key: tuple[str, ...], bundle: ProfileBundle
    ) -> SanitizationOrchestrator:
        async with self._lock:
            inner = self._inner_by_key.get(key)
            if inner is None:
                inner = self._build_inner(bundle)
                # Set the fingerprint BEFORE publishing the orchestrator so a
                # reader that sees the inner in the dict always finds its fp.
                self._fingerprint_by_key[key] = bundle_fingerprint(bundle)
                self._inner_by_key[key] = inner
            return inner

    async def _team_config(self, team_id: str) -> TeamConfig:
        try:
            return await self._team_store.get(team_id)
        except TeamNotFoundError:
            # Unknown team → no profiles → the core orchestrator (today's behavior).
            return TeamConfig(team_id=team_id, name=team_id)
