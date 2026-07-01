"""End-to-end sanitization orchestrator (M1-7 core).

Composes: corp-LLM client + three-tier strategies + MappingStore (cache A
+ B) + RulesLoader (cache C) + payload utils. The LiteLLM-specific
pre_call hook adapter wraps this for actual deployment; this module is
framework-free and unit-testable.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from corp_llm_gateway.corp_llm import (
    SANITIZE_TOOL_NAME,
    SANITIZE_TOOL_SCHEMA,
    CorpLlmClient,
)
from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.detectors.regex_checksum import _deduplicate
from corp_llm_gateway.payload import (
    DEFAULT_THRESHOLD_BYTES,
    should_skip_sanitization,
)
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.rules.gazetteer import Gazetteer
from corp_llm_gateway.sanitizer.allowlist import Allowlist
from corp_llm_gateway.sanitizer.engine import (
    AllStrategiesFailedError,
    CorpLlmSanitizer,
)
from corp_llm_gateway.sanitizer.local_pass import LocalDetectionPass
from corp_llm_gateway.sanitizer.strategies import (
    FunctionCallStrategy,
    JsonStrategy,
    RegexStrategy,
    StrategyResult,
)
from corp_llm_gateway.storage import MappingStore

_PLACEHOLDER_LABEL_RE = re.compile(r"^\[([A-Z_]+)_(\d+)\]$")

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SanitizeResult:
    sanitized_text: str
    pairs: tuple[tuple[str, str], ...]
    cache_a_hit: bool
    skipped: bool


def default_sanitizer() -> CorpLlmSanitizer:
    return CorpLlmSanitizer(strategies=[FunctionCallStrategy(), JsonStrategy(), RegexStrategy()])


class SanitizationOrchestrator:
    def __init__(
        self,
        corp_llm: CorpLlmClient,
        mapping_store: MappingStore,
        rules_loader: RulesLoader,
        *,
        sanitizer: CorpLlmSanitizer | None = None,
        cache_a_ttl_seconds: int = 36000,
        cache_b_ttl_seconds: int = 3600,
        size_threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
        local_detectors: list[PIIDetector] | None = None,
        gazetteer: Gazetteer | None = None,
        allowlist: Allowlist | None = None,
    ) -> None:
        self._corp_llm = corp_llm
        self._mapping_store = mapping_store
        self._rules_loader = rules_loader
        self._sanitizer = sanitizer or default_sanitizer()
        self._cache_a_ttl = cache_a_ttl_seconds
        self._cache_b_ttl = cache_b_ttl_seconds
        self._size_threshold = size_threshold_bytes
        self._local = LocalDetectionPass(local_detectors) if local_detectors else None
        self._gazetteer = gazetteer
        self._allowlist = allowlist

    async def sanitize(
        self,
        text: str,
        *,
        team_id: str,
        conversation_id: str,
    ) -> SanitizeResult:
        content_bytes = len(text.encode("utf-8"))
        logger.info(
            "sanitize_start team_id=%s conversation_id=%s content_bytes=%d",
            team_id,
            conversation_id,
            content_bytes,
        )

        if should_skip_sanitization(content_bytes, threshold_bytes=self._size_threshold):
            logger.info(
                "sanitize_skipped_size team_id=%s conversation_id=%s size=%d threshold=%d",
                team_id,
                conversation_id,
                content_bytes,
                self._size_threshold,
            )
            return SanitizeResult(text, (), cache_a_hit=False, skipped=True)

        rules = await self._rules_loader.load(team_id)
        logger.info(
            "sanitize_rules_loaded team_id=%s conversation_id=%s rule_count=%d",
            team_id,
            conversation_id,
            len(rules.rules),
        )

        content_hash = _content_hash(team_id, rules, text)

        cached = await self._mapping_store.get_dedup(content_hash)
        if cached is not None:
            logger.info(
                "sanitize_cache_a_hit team_id=%s conversation_id=%s content_hash=%s pairs=%d",
                team_id,
                conversation_id,
                content_hash[:12],
                len(cached.pairs),
            )
            await self._record_conversation_mappings(conversation_id, cached.pairs)
            logger.info(
                "sanitize_cache_b_recorded team_id=%s conversation_id=%s "
                "pairs=%d ttl=%d source=cache_a",
                team_id,
                conversation_id,
                len(cached.pairs),
                self._cache_b_ttl,
            )
            return SanitizeResult(
                _apply_pairs(text, cached.pairs),
                cached.pairs,
                cache_a_hit=True,
                skipped=False,
            )

        logger.info(
            "sanitize_cache_a_miss team_id=%s conversation_id=%s content_hash=%s",
            team_id,
            conversation_id,
            content_hash[:12],
        )

        if self._gazetteer is not None:
            # DP-4: run gazetteer + local first; oracle is conditional on a gazetteer hit.
            gaz_findings = await self._gazetteer.detect(text)
            local_findings = await self._local.findings(text) if self._local is not None else []
            combined = _deduplicate(local_findings + gaz_findings)
            if gaz_findings:
                logger.info(
                    "sanitize_branch=gazetteer_hit oracle=yes "
                    "team_id=%s conversation_id=%s gaz_hits=%d",
                    team_id,
                    conversation_id,
                    len(gaz_findings),
                )
                logger.info(
                    "sanitize_corp_llm_call_start team_id=%s conversation_id=%s",
                    team_id,
                    conversation_id,
                )
                oracle_result = await self._call_corp_llm(text, rules)
                logger.info(
                    "sanitize_corp_llm_call_done team_id=%s conversation_id=%s pairs=%d",
                    team_id,
                    conversation_id,
                    len(oracle_result.pairs),
                )
                merged_pairs = _merge_local(oracle_result.pairs, combined)
                _emit_gazetteer_proposal(oracle_result.pairs, combined, team_id, conversation_id)
            else:
                # No gazetteer hit → local pass is authoritative; oracle skipped.
                logger.info(
                    "sanitize_branch=gazetteer_nohit oracle=skipped "
                    "team_id=%s conversation_id=%s local_findings=%d",
                    team_id,
                    conversation_id,
                    len(local_findings),
                )
                merged_pairs = _merge_local((), combined)
            result = StrategyResult(pairs=merged_pairs)

        elif self._local is not None:
            # DP-3 path: oracle always on, local merged additively.
            logger.info(
                "sanitize_branch=local_pass oracle=yes team_id=%s conversation_id=%s",
                team_id,
                conversation_id,
            )
            logger.info(
                "sanitize_corp_llm_call_start team_id=%s conversation_id=%s",
                team_id,
                conversation_id,
            )
            result = await self._call_corp_llm(text, rules)
            logger.info(
                "sanitize_corp_llm_call_done team_id=%s conversation_id=%s pairs=%d",
                team_id,
                conversation_id,
                len(result.pairs),
            )
            local_findings = await self._local.findings(text)
            merged_pairs = _merge_local(result.pairs, local_findings)
            logger.info(
                "sanitize_local_pass team_id=%s conversation_id=%s "
                "oracle_pairs=%d local_findings=%d merged_pairs=%d",
                team_id,
                conversation_id,
                len(result.pairs),
                len(local_findings),
                len(merged_pairs),
            )
            result = StrategyResult(pairs=merged_pairs)

        else:
            # Legacy path: oracle only.
            logger.info(
                "sanitize_branch=oracle_only team_id=%s conversation_id=%s",
                team_id,
                conversation_id,
            )
            logger.info(
                "sanitize_corp_llm_call_start team_id=%s conversation_id=%s",
                team_id,
                conversation_id,
            )
            result = await self._call_corp_llm(text, rules)
            logger.info(
                "sanitize_corp_llm_call_done team_id=%s conversation_id=%s pairs=%d",
                team_id,
                conversation_id,
                len(result.pairs),
            )

        if self._allowlist is not None:
            result = StrategyResult(pairs=self._allowlist.filter_pairs(result.pairs))

        await self._mapping_store.set_dedup(content_hash, result, ttl_seconds=self._cache_a_ttl)
        logger.info(
            "sanitize_cache_a_stored team_id=%s conversation_id=%s content_hash=%s ttl=%d pairs=%d",
            team_id,
            conversation_id,
            content_hash[:12],
            self._cache_a_ttl,
            len(result.pairs),
        )

        await self._record_conversation_mappings(conversation_id, result.pairs)
        logger.info(
            "sanitize_cache_b_recorded team_id=%s conversation_id=%s "
            "pairs=%d ttl=%d source=corp_llm",
            team_id,
            conversation_id,
            len(result.pairs),
            self._cache_b_ttl,
        )

        return SanitizeResult(
            _apply_pairs(text, result.pairs),
            result.pairs,
            cache_a_hit=False,
            skipped=False,
        )

    async def _call_corp_llm(self, text: str, rules: Rules) -> StrategyResult:
        system_prompt = _build_system_prompt(rules)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]
        response = await self._corp_llm.chat_completion(
            messages=messages,
            tools=[SANITIZE_TOOL_SCHEMA],
            tool_choice={
                "type": "function",
                "function": {"name": SANITIZE_TOOL_NAME},
            },
        )
        try:
            return await self._sanitizer.extract(response)
        except AllStrategiesFailedError:
            raise

    async def _record_conversation_mappings(
        self,
        conversation_id: str,
        pairs: tuple[tuple[str, str], ...],
    ) -> None:
        for original, placeholder in pairs:
            await self._mapping_store.put(
                conversation_id,
                original,
                placeholder,
                sliding_ttl_seconds=self._cache_b_ttl,
            )


def _build_system_prompt(rules: Rules) -> str:
    lines = [
        "You are a corp-confidential PII / sensitive-term redactor.",
        "Identify all PII or corp-confidential terms in the user's input and",
        f"call the {SANITIZE_TOOL_NAME!r} tool with their (original, replacement)",
        "pairs. Use placeholders like [LABEL_NNN]. Apply these team-specific",
        "rules with priority — they OVERRIDE your judgment when they match:",
        "",
    ]
    for rule in rules.rules:
        lines.append(f"- `{rule.pattern}` → `{rule.replacement}`")
    if not rules.rules:
        lines.append("(no team-specific rules; use general PII judgment only)")
    return "\n".join(lines)


def _content_hash(team_id: str, rules: Rules, text: str) -> str:
    h = hashlib.sha256()
    h.update(team_id.encode("utf-8"))
    h.update(b"\x1f")
    for rule in rules.rules:
        h.update(rule.pattern.encode("utf-8"))
        h.update(b"\x1e")
        h.update(rule.replacement.encode("utf-8"))
        h.update(b"\x1f")
    h.update(b"\x1d")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


def _apply_pairs(text: str, pairs: tuple[tuple[str, str], ...]) -> str:
    sorted_pairs = sorted(pairs, key=lambda p: -len(p[0]))
    for original, placeholder in sorted_pairs:
        text = text.replace(original, placeholder)
    return text


def _emit_gazetteer_proposal(
    oracle_pairs: tuple[tuple[str, str], ...],
    combined_findings: list[Finding],
    team_id: str,
    conversation_id: str,
) -> None:
    """Log counts+labels for oracle-found items not covered by gazetteer/local.

    Logs only counts and label shapes — NO raw values (M1-14 compliant).
    # TODO(DP-4): wire ShadowDetector human-approval loop
    """
    covered_originals = {f.text for f in combined_findings}
    uncovered_placeholders = [p for o, p in oracle_pairs if o not in covered_originals]
    if not uncovered_placeholders:
        return
    label_counts: dict[str, int] = {}
    for placeholder in uncovered_placeholders:
        m = _PLACEHOLDER_LABEL_RE.match(placeholder)
        label = m.group(1) if m else "UNKNOWN"
        label_counts[label] = label_counts.get(label, 0) + 1
    logger.info(
        "gazetteer_proposal team_id=%s conversation_id=%s uncovered_count=%d label_counts=%s",
        team_id,
        conversation_id,
        len(uncovered_placeholders),
        label_counts,
    )


def _merge_local(
    oracle_pairs: tuple[tuple[str, str], ...],
    local_findings: list[Finding],
) -> tuple[tuple[str, str], ...]:
    """Merge local detector findings into oracle pairs, preserving bijection.

    Every original maps to exactly one placeholder; no placeholder is reused
    for a different original. Oracle pairs are unchanged (base).
    """
    seen_originals: set[str] = {o for o, _ in oracle_pairs}
    used_placeholders: set[str] = {p for _, p in oracle_pairs}

    # Seed per-label counter from oracle so local picks non-colliding numbers.
    label_max: dict[str, int] = {}
    for _, p in oracle_pairs:
        m = _PLACEHOLDER_LABEL_RE.match(p)
        if m:
            label, n = m.group(1), int(m.group(2))
            if n > label_max.get(label, 0):
                label_max[label] = n

    new_pairs: list[tuple[str, str]] = []
    # Deterministic order: highest score first, then longest span.
    for finding in sorted(local_findings, key=lambda f: (-f.score, -(f.end - f.start))):
        if finding.text in seen_originals:
            continue
        label = finding.label
        n = label_max.get(label, 0) + 1
        placeholder = f"[{label}_{n:03d}]"
        while placeholder in used_placeholders:
            n += 1
            placeholder = f"[{label}_{n:03d}]"
        label_max[label] = n
        seen_originals.add(finding.text)
        used_placeholders.add(placeholder)
        new_pairs.append((finding.text, placeholder))

    return oracle_pairs + tuple(new_pairs)
