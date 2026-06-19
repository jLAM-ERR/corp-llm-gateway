"""End-to-end sanitization orchestrator (M1-7 core).

Composes: corp-LLM client + three-tier strategies + MappingStore (cache A
+ B) + RulesLoader (cache C) + payload utils. The LiteLLM-specific
pre_call hook adapter wraps this for actual deployment; this module is
framework-free and unit-testable.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from corp_llm_gateway.corp_llm import (
    SANITIZE_TOOL_NAME,
    SANITIZE_TOOL_SCHEMA,
    CorpLlmClient,
)
from corp_llm_gateway.payload import (
    DEFAULT_THRESHOLD_BYTES,
    should_skip_sanitization,
)
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer.engine import (
    AllStrategiesFailedError,
    CorpLlmSanitizer,
)
from corp_llm_gateway.sanitizer.strategies import (
    FunctionCallStrategy,
    JsonStrategy,
    RegexStrategy,
    StrategyResult,
)
from corp_llm_gateway.storage import MappingStore

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
    ) -> None:
        self._corp_llm = corp_llm
        self._mapping_store = mapping_store
        self._rules_loader = rules_loader
        self._sanitizer = sanitizer or default_sanitizer()
        self._cache_a_ttl = cache_a_ttl_seconds
        self._cache_b_ttl = cache_b_ttl_seconds
        self._size_threshold = size_threshold_bytes

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
