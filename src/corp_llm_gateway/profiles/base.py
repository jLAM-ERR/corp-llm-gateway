"""Profile bundle types + the ProfileLoader contract.

A profile is a declarative data bundle (profile.toml + replace.md + gazetteer
term files + allowlist) layered over the core. ``PolicyKnobs.merge`` composes
layers with most-restrictive-wins for security knobs so composition only ever
ADDS redaction (monotone-tightening — preserves the M1-14 no-originals-leak
invariant). ``ProfileBundle`` mirrors exactly the argument set
``SanitizationOrchestrator`` already accepts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from corp_llm_gateway.payload import DEFAULT_THRESHOLD_BYTES
from corp_llm_gateway.team_config.models import FailPolicyOverrides

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from corp_llm_gateway.detectors.base import PIIDetector
    from corp_llm_gateway.rules.gazetteer import Gazetteer
    from corp_llm_gateway.rules.models import Rules
    from corp_llm_gateway.sanitizer.allowlist import Allowlist
    from corp_llm_gateway.team_config.models import FailPolicy


class ProfileNotFoundError(Exception):
    pass


class ProfileParseError(Exception):
    pass


# Oracle trigger modes ranked by detection coverage (least → most). Merge keeps
# the highest-coverage mode across layers so a layer can only widen the oracle,
# never narrow it (monotone-tightening). "sampled:<pct>" collapses to "sampled".
#
# This is the ONE ordering: `PolicyKnobs.merge` composes layers with it and
# `sanitizer.profile_orchestrator` picks the request-time trigger with it. A
# second ranking that drifts from this one would silently narrow the oracle.
_ORACLE_COVERAGE: dict[str, int] = {
    "gazetteer_hit": 0,
    "sampled": 1,
    "any_local_finding": 2,
    "always": 3,
}

_SAMPLED_PREFIX = "sampled:"


@dataclass(frozen=True)
class PolicyKnobs:
    """Declarative policy layer. Security knobs tighten under merge; retention
    is a non-security knob resolved last-writer (most-specific layer wins)."""

    size_threshold_bytes: int = DEFAULT_THRESHOLD_BYTES
    block_payloads: bool = False
    dlp_guard: bool = False
    oracle_mode: str = "gazetteer_hit"
    # None == this layer imposes no provider restriction (intersection identity).
    allowed_providers: frozenset[str] | None = None
    canary_patterns: tuple[str, ...] = ()
    fail_policy: FailPolicyOverrides = field(default_factory=FailPolicyOverrides)
    retention_hot_days: int | None = None
    retention_cold_years: int | None = None

    @classmethod
    def merge(cls, layers: Sequence[PolicyKnobs]) -> PolicyKnobs:
        """Compose ordered ``[core, ..., most-specific]`` layers.

        Security: size=min, block/dlp=OR, providers=intersection,
        fail_policy=most-closed, oracle=most-coverage, canaries=union.
        Non-security: retention=last-writer. Empty input → defaults.
        """
        if not layers:
            return cls()
        return cls(
            size_threshold_bytes=min(layer.size_threshold_bytes for layer in layers),
            block_payloads=any(layer.block_payloads for layer in layers),
            dlp_guard=any(layer.dlp_guard for layer in layers),
            oracle_mode=broadest_oracle_mode(*(layer.oracle_mode for layer in layers)),
            allowed_providers=_intersect_providers(layers),
            canary_patterns=_union_canaries(layers),
            fail_policy=_merge_fail_policy(layers),
            retention_hot_days=_last_writer(layer.retention_hot_days for layer in layers),
            retention_cold_years=_last_writer(layer.retention_cold_years for layer in layers),
        )


@dataclass(frozen=True)
class ProfileBundle:
    """Resolved, merged profile — the exact constructor inputs
    ``SanitizationOrchestrator`` accepts (so D4 needs no orchestrator change)."""

    detectors: tuple[PIIDetector, ...]
    gazetteer: Gazetteer | None
    rules: Rules
    allowlist: Allowlist
    policy: PolicyKnobs
    profile_ids: tuple[str, ...]
    # DECLARED name of each entry in `detectors`, same order. Consumers key
    # per-detector policy off it (which detectors may run on CODE segments — see
    # `registry.CODE_SAFE_DETECTORS`); an unnamed detector is treated as unsafe.
    detector_names: tuple[str, ...] = ()


class ProfileLoader(ABC):
    """Load one profile layer by id (sibling of ``rules.loader.RulesLoader``)."""

    @abstractmethod
    async def load(self, profile_id: str) -> ProfileBundle: ...


class StubProfileLoader(ProfileLoader):
    """Placeholder for a signed/remote profile backend (ADR-001 rule 3)."""

    async def load(self, profile_id: str) -> ProfileBundle:
        raise NotImplementedError(
            "StubProfileLoader: signed/remote profile bundles await the D6 offline-PKI "
            "decision; use FileProfileLoader for local bundles"
        )


def _sampled_pct(mode: str) -> int | None:
    """The ``<pct>`` of a ``sampled:<pct>`` mode; None for any other mode.

    Tolerant on purpose — canonicalization is not validation. An unparseable
    percentage keeps its "sampled" rank here and is rejected later by
    ``sanitizer.orchestrator.normalize_oracle_trigger``.
    """
    head, _, tail = mode.partition(":")
    if head.strip().lower() != "sampled":
        return None
    try:
        return int(tail.strip())
    except ValueError:
        return None


def _canonical_oracle_mode(mode: str) -> str:
    """Collapse the two degenerate sample percentages onto their exact equals.

    ``_sample_selected`` returns True for every leaf at pct >= 100 and False for
    every leaf at pct <= 0, so those are ``always`` / ``gazetteer_hit`` by
    definition. Without this, ``sampled:100`` would rank BELOW
    ``any_local_finding`` and picking the latter would narrow coverage.
    """
    pct = _sampled_pct(mode)
    if pct is None:
        return mode
    if pct >= 100:
        return "always"
    if pct <= 0:
        return "gazetteer_hit"
    return mode


def oracle_rank(mode: str) -> int:
    """Coverage rank of one oracle trigger mode; unknown → -1."""
    head = _canonical_oracle_mode(mode).split(":", 1)[0].strip().lower()
    return _ORACLE_COVERAGE.get(head, -1)


def broadest_oracle_mode(*modes: str) -> str:
    """The widest-coverage mode among *modes* (empty → the default).

    Ties between two ``sampled`` modes go to the higher percentage: same rank,
    strictly more leaves. Degenerate percentages are returned as the fixed mode
    they are identical to.
    """
    if not modes:
        return "gazetteer_hit"
    canonical = [_canonical_oracle_mode(mode) for mode in modes]
    return max(canonical, key=lambda mode: (oracle_rank(mode), _sampled_pct(mode) or 0))


def _intersect_providers(layers: Sequence[PolicyKnobs]) -> frozenset[str] | None:
    result: frozenset[str] | None = None
    for layer in layers:
        if layer.allowed_providers is None:
            continue
        result = layer.allowed_providers if result is None else (result & layer.allowed_providers)
    return result


def _union_canaries(layers: Sequence[PolicyKnobs]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for layer in layers:
        for pattern in layer.canary_patterns:
            if pattern not in seen:
                seen.add(pattern)
                out.append(pattern)
    return tuple(out)


def _closed(current: FailPolicy, candidate: FailPolicy) -> FailPolicy:
    return "fail-closed" if "fail-closed" in (current, candidate) else "continue"


def _merge_fail_policy(layers: Sequence[PolicyKnobs]) -> FailPolicyOverrides:
    pre: FailPolicy = "continue"
    sink: FailPolicy = "continue"
    buffer: FailPolicy = "continue"
    for layer in layers:
        pre = _closed(pre, layer.fail_policy.pre_pass_down)
        sink = _closed(sink, layer.fail_policy.audit_sink_down)
        buffer = _closed(buffer, layer.fail_policy.audit_buffer_full)
    return FailPolicyOverrides(
        pre_pass_down=pre,
        audit_sink_down=sink,
        audit_buffer_full=buffer,
    )


def _last_writer(values: Iterable[int | None]) -> int | None:
    result: int | None = None
    for value in values:
        if value is not None:
            result = value
    return result
