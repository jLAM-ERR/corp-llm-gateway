"""End-to-end sanitization orchestrator (M1-7 core).

Composes: corp-LLM client + three-tier strategies + MappingStore (cache A
+ B) + RulesLoader (cache C) + payload utils. The LiteLLM-specific
pre_call hook adapter wraps this for actual deployment; this module is
framework-free and unit-testable.
"""

from __future__ import annotations

import bisect
import hashlib
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

from corp_llm_gateway.corp_llm import (
    SANITIZE_TOOL_NAME,
    SANITIZE_TOOL_SCHEMA,
    CorpLlmClient,
)
from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector, _deduplicate
from corp_llm_gateway.payload import (
    DEFAULT_THRESHOLD_BYTES,
    OVERSIZE_CHUNK,
    OVERSIZE_DELIVER_FLAG,
    OVERSIZE_FAIL_CLOSED,
    OversizeContentError,
    normalize_oversize_policy,
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
from corp_llm_gateway.sanitizer.placeholder import AppliedSpan, apply_spans
from corp_llm_gateway.sanitizer.placeholder_allocator import RequestPlaceholderAllocator
from corp_llm_gateway.sanitizer.strategies import (
    FunctionCallStrategy,
    JsonStrategy,
    RegexStrategy,
    StrategyResult,
)
from corp_llm_gateway.storage import MappingStore, PlaceholderMapping

_PLACEHOLDER_LABEL_RE = re.compile(r"^\[([A-Z_]+)_(\d+)\]$")
# A Cache-A hit applies the stored mapping WITHOUT running detectors, so an entry
# written under narrower coverage replays as unredacted for the whole ~10h TTL.
# TWO mechanisms keep the key honest, and neither subsumes the other:
#
#   1. this constant — bump on any BUILD-time change to what the request path
#      redacts or how it substitutes. It is the only thing that can see a change
#      made INSIDE a component: adding BANK_CARD was a rule-table edit inside
#      RegexChecksumDetector — same class, same config, same construction args,
#      so the policy fingerprint below is blind to it.
#   2. `_policy_fingerprint` (computed per orchestrator in __init__) — hashes the
#      effective policy: detector classes, the code-safe subset, gazetteer terms,
#      allowlist entries, oracle trigger. It is the only thing that can see a
#      RUNTIME/CONFIG-time change: flipping CORP_NER_ENABLED adds a whole
#      detector on one pod while its neighbours on the same image, same version
#      and same Redis keep the old set.
#
# Bumping the constant retires every prior entry at upgrade with no cache flush.
# Log of boundaries:
#   span-aware-v1  substitution moved from pair-global replace to span-aware
#                  planning; old entries could have dropped a mapping.
#   coverage-v2    detector coverage widened three ways: the Luhn-validated
#                  BANK_CARD label is new (older entries never saw a card),
#                  local NER's participation in CODE segments changed, and corp
#                  NER can add a whole detector when enabled.
_CACHE_A_ALGORITHM_VERSION = b"coverage-v2"

# Bump when the fingerprint's own INPUT SET changes (a newly folded component),
# so entries keyed by the older, less complete fingerprint are retired too.
#   policy-v2  the gazetteer's LEMMATIZER identity joined the input set: equal
#              term signatures still match different text when one process has
#              the `ner` extra and another does not.
#   policy-v3  detectors may now report a `policy_signature()` beyond their class
#              (see PolicySignatureDetector); dual_ner reports its effective ENGINE
#              capability, so a model-less pod and a model-backed one stop sharing
#              a key. Same hole as policy-v2, on the primary NER path.
_POLICY_FINGERPRINT_VERSION = b"policy-v3"

# Chunk overlap sizing (F1 chunk policy). Regex/checksum patterns are LINEAR and
# now run over the FULL text (see `_sanitize_chunked`), so the overlap no longer
# has to contain them — critically, several regex patterns are UNBOUNDED (JWT,
# Bearer {20,}, sk-{32,}, DB URL {3,}) and could never be bounded by a fixed
# overlap anyway (H1). The overlap only has to keep a BOUNDED entity from the
# CHUNKED pass (NER spans, gazetteer terms) inside one window; 8192+128 is a
# conservative upper bound for those and is retained as a safe margin.
_MAX_ENTITY_CHARS = 8192 + 128
_DEFAULT_CHUNK_OVERLAP_CHARS = _MAX_ENTITY_CHARS
_DEFAULT_CHUNK_WINDOW_CHARS = 4 * _MAX_ENTITY_CHARS

# CORP_LLM_ORACLE_TRIGGER values (F3). Govern when the CONDITIONAL oracle runs on
# a NO-gazetteer-hit leaf. A gazetteer hit ALWAYS runs the oracle regardless — the
# trigger only widens the no-hit case. gazetteer_hit (default) keeps ADR-003
# latency parity; the others backstop an incomplete local detection.
ORACLE_TRIGGER_GAZETTEER_HIT = "gazetteer_hit"
ORACLE_TRIGGER_ANY_LOCAL_FINDING = "any_local_finding"
ORACLE_TRIGGER_ALWAYS = "always"
_ORACLE_TRIGGER_SAMPLED_PREFIX = "sampled:"
_ORACLE_TRIGGER_FIXED = frozenset(
    {ORACLE_TRIGGER_GAZETTEER_HIT, ORACLE_TRIGGER_ANY_LOCAL_FINDING, ORACLE_TRIGGER_ALWAYS}
)

logger = logging.getLogger(__name__)


def _parse_sample_pct(trigger: str) -> int:
    """Extract the ``<pct>`` (0..100) from a canonical ``sampled:<pct>`` trigger."""
    raw = trigger[len(_ORACLE_TRIGGER_SAMPLED_PREFIX) :]
    try:
        pct = int(raw)
    except ValueError:
        raise ValueError(
            f"invalid oracle trigger {trigger!r}; 'sampled:<pct>' needs an integer, got {raw!r}"
        ) from None
    if not 0 <= pct <= 100:
        raise ValueError(f"invalid oracle trigger {trigger!r}; pct must be 0..100, got {pct}")
    return pct


def normalize_oracle_trigger(value: str | None) -> str:
    """Validate CORP_LLM_ORACLE_TRIGGER; unset/empty → gazetteer_hit. Unknown → ValueError.

    Canonical forms: ``gazetteer_hit`` | ``any_local_finding`` | ``always`` |
    ``sampled:<pct>`` where ``<pct>`` is an integer 0..100.
    """
    trigger = (value or ORACLE_TRIGGER_GAZETTEER_HIT).strip().lower()
    if trigger in _ORACLE_TRIGGER_FIXED:
        return trigger
    if trigger.startswith(_ORACLE_TRIGGER_SAMPLED_PREFIX):
        return f"{_ORACLE_TRIGGER_SAMPLED_PREFIX}{_parse_sample_pct(trigger)}"
    raise ValueError(
        f"invalid oracle trigger {value!r}; expected one of "
        f"{sorted(_ORACLE_TRIGGER_FIXED)} or 'sampled:<pct>' (pct 0..100)"
    )


def _sample_selected(seed: str, pct: int) -> bool:
    """Deterministic per-request sample: True for ~``pct``% of distinct seeds.

    Seeds from a stable SHA-256 of the seed string (conversation_id + content),
    NEVER a PRNG or clock — a given request always resolves the same way (F3), so
    the sampled fraction is reproducible and auditable.
    """
    if pct <= 0:
        return False
    if pct >= 100:
        return True
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100 < pct


@dataclass(frozen=True)
class _MatchedRule:
    start: int
    end: int
    original: str
    replacement: str


class _NonOverlappingSpans:
    """Track accepted non-overlapping half-open ``[start, end)`` spans and
    answer "does this candidate overlap an already-accepted span" without a
    linear scan over every prior accepted span.

    ``_rule_matches``/``_plan_replacements`` used to append accepted spans to
    a plain list and check a NEW candidate with ``any(... for ... in
    occupied)`` — O(accepted) per candidate, O(candidates^2) overall. Measured
    on a single 100 KiB text leaf with one 4-char rule term (~20k matches):
    ~5s of synchronous CPU, blocking the asyncio event loop against a ~6ms
    p50 / 4s p99 budget (no GPU escape hatch — CLAUDE.md).

    Spans are kept sorted by start, so bisect only has to inspect the two
    immediate neighbors of the insertion point — any span farther away is,
    by the non-overlapping invariant, guaranteed not to touch the candidate.
    That turns the check into O(log k); the array insert to keep the list
    sorted is a single C-level memmove (`list.insert`), not a Python-level
    loop, so it stays fast in practice at the sizes this engine handles
    (bounded by the payload size threshold / chunk window).
    """

    __slots__ = ("_ends", "_starts")

    def __init__(self) -> None:
        self._starts: list[int] = []
        self._ends: list[int] = []

    def overlaps(self, start: int, end: int) -> bool:
        # A zero-length candidate (start == end) sitting exactly at an
        # accepted span's own start diverges from the old linear scan's
        # `start < used_end and end > used_start` semantics (this returns
        # True there; the old check returned False) — every other position,
        # including every other zero-length position, agrees. Unreachable in
        # practice: every candidate this engine produces comes from a
        # non-empty regex match, so start < end always holds. See
        # test_non_overlapping_spans_zero_length_candidate_diverges_at_left_edge.
        i = bisect.bisect_right(self._starts, start)
        if i > 0 and self._ends[i - 1] > start:
            return True
        return i < len(self._starts) and self._starts[i] < end

    def add(self, start: int, end: int) -> None:
        i = bisect.bisect_right(self._starts, start)
        self._starts.insert(i, start)
        self._ends.insert(i, end)


# Rule dictionaries are per-team and small (tens of patterns); recompiling one
# on every _rule_matches call measured ~44 ms/call at 200 rules x 10.8 KB
# against a ~6 ms p50 CPU budget (no GPU escape hatch — see CLAUDE.md). 4096
# covers, e.g., ~20 teams at 200 unique patterns each (or far more teams with
# smaller dictionaries) while staying bounded so the cache can't grow without
# limit across an unbounded number of teams over the process lifetime.
@lru_cache(maxsize=4096)
def _rule_pattern(source: str) -> re.Pattern[str]:
    """Case-insensitive substring match (decision 2, defect #7).

    release/1.0.x used a plain `r.pattern in text` (case-sensitive) substring
    test; an identifier-prefix anchor was added on top of it, which silently
    REDUCED redaction (a rule `Ledger` stopped matching `MyLedger`). Keep only
    the case-insensitivity widening — drop the anchor so a rule matches inside
    a larger word on either side, same as release, plus the widening.
    """
    return re.compile(re.escape(source), re.IGNORECASE | re.UNICODE)


def _rule_matches(rules: Rules, text: str) -> tuple[_MatchedRule, ...]:
    """Return non-overlapping literal-rule matches, longest configured rule first."""
    selected: list[_MatchedRule] = []
    occupied = _NonOverlappingSpans()
    ordered = sorted(enumerate(rules.rules), key=lambda item: (-len(item[1].pattern), item[0]))
    for _, rule in ordered:
        for match in _rule_pattern(rule.pattern).finditer(text):
            start, end = match.span()
            if occupied.overlaps(start, end):
                continue
            original = text[start:end]
            selected.append(
                _MatchedRule(
                    start=start,
                    end=end,
                    original=original,
                    replacement=rule.replacement,
                )
            )
            occupied.add(start, end)
    return tuple(sorted(selected, key=lambda item: item.start))


@dataclass(frozen=True)
class _ReplacementPlan:
    sanitized_text: str
    pairs: tuple[tuple[str, str], ...]
    spans: tuple[AppliedSpan, ...]
    # Originals (of `pairs`) matched by a replace.md rule rather than a
    # detector/oracle finding — threaded to SanitizeResult so the request-level
    # RequestPlaceholderAllocator can exempt them from the bijection re-mint:
    # case-insensitive rule matches sharing one configured replacement are a
    # deliberate many-to-one mapping, not a collision.
    rule_originals: frozenset[str] = frozenset()


def _plan_replacements(
    text: str,
    pairs: tuple[tuple[str, str], ...],
    rule_matches: tuple[_MatchedRule, ...],
    *,
    active_rule_origins: frozenset[str] | None = None,
) -> _ReplacementPlan:
    """Select concrete replacement spans: longest span wins (M1-5/decision 1).

    Rule matches and finding/oracle pair occurrences compete in ONE candidate
    pool ordered by span length descending, so a rule can no longer steal a
    span out from under a longer, genuinely-overlapping finding (defect #5).
    Tiebreak on equal length: rule over finding, then lower start, then pair
    index — deterministic so the Cache-A hit path (which re-runs this with the
    already-filtered `cached.pairs`) reproduces the miss path bit-for-bit. An
    equal-length tie at the IDENTICAL span can't actually reach this pool:
    the same span of the same text is the same substring, which the
    `seen_originals` dedup below already resolved (it keeps the first pair —
    rule pairs precede oracle/finding pairs in the caller's `pairs` argument).
    """
    unique_pairs: list[tuple[str, str]] = []
    seen_originals: set[str] = set()
    for original, replacement in pairs:
        if not original or original in seen_originals:
            continue
        seen_originals.add(original)
        unique_pairs.append((original, replacement))

    pair_by_original = dict(unique_pairs)
    pair_index_by_original = {original: index for index, (original, _) in enumerate(unique_pairs)}
    if active_rule_origins is None:
        available_pairs = set(unique_pairs)
        active_rule_origins = frozenset(
            match.original
            for match in rule_matches
            if (match.original, match.replacement) in available_pairs
        )

    # (sort_key, span) candidates from ONE pool. sort_key =
    # (-length, is_finding, start, pair_index) — rule (is_finding=0) beats
    # finding (is_finding=1) at equal length, then lower start, then pair_index.
    candidates: list[tuple[tuple[int, int, int, int], AppliedSpan]] = []
    for match in rule_matches:
        if match.original not in active_rule_origins or match.original not in pair_by_original:
            continue
        pair_index = pair_index_by_original[match.original]
        key = (-(match.end - match.start), 0, match.start, pair_index)
        candidates.append((key, AppliedSpan(match.start, match.end, match.original)))

    for pair_index, (original, _) in enumerate(unique_pairs):
        if original in active_rule_origins:
            continue
        for occurrence in re.finditer(re.escape(original), text):
            start, end = occurrence.start(), occurrence.end()
            key = (-(end - start), 1, start, pair_index)
            candidates.append((key, AppliedSpan(start, end, original)))

    selected: list[AppliedSpan] = []
    occupied = _NonOverlappingSpans()
    for _, span in sorted(candidates, key=lambda item: item[0]):
        if occupied.overlaps(span.start, span.end):
            continue
        selected.append(span)
        occupied.add(span.start, span.end)

    spans = tuple(sorted(selected, key=lambda span: (span.start, span.end)))
    used_originals = {span.original for span in spans}
    used_pairs = tuple(pair for pair in unique_pairs if pair[0] in used_originals)
    return _ReplacementPlan(
        sanitized_text=apply_spans(text, spans, used_pairs),
        pairs=used_pairs,
        spans=spans,
        rule_originals=frozenset(active_rule_origins) & used_originals,
    )


@dataclass(frozen=True)
class SanitizeResult:
    sanitized_text: str
    pairs: tuple[tuple[str, str], ...]
    cache_a_hit: bool
    skipped: bool
    # Set on the opt-in oversize deliver-flag egress so the audit trail can
    # distinguish a delivered oversize original from a normal zero-redaction
    # request (M1). None on every other path.
    block_reason: str | None = None
    # Exact forward-substitution ranges, retained so request-level placeholder
    # remapping can reproduce the same overlap decisions without global replace.
    applied_spans: tuple[AppliedSpan, ...] = ()
    # Originals (of `pairs`) matched by a replace.md rule — see
    # RequestPlaceholderAllocator.remap's `exempt_from_bijection`.
    rule_originals: frozenset[str] = frozenset()


OVERSIZE_DELIVERED_REASON = "oversize:delivered"


def default_sanitizer() -> CorpLlmSanitizer:
    return CorpLlmSanitizer(strategies=[FunctionCallStrategy(), JsonStrategy(), RegexStrategy()])


class SanitizationOrchestrator:
    def __init__(
        self,
        corp_llm: CorpLlmClient | None,
        mapping_store: MappingStore,
        rules_loader: RulesLoader,
        *,
        sanitizer: CorpLlmSanitizer | None = None,
        cache_a_ttl_seconds: int = 36000,
        cache_b_ttl_seconds: int = 3600,
        size_threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
        oversize_policy: str = OVERSIZE_FAIL_CLOSED,
        oversize_deliver_teams: frozenset[str] = frozenset(),
        chunk_window_chars: int = _DEFAULT_CHUNK_WINDOW_CHARS,
        chunk_overlap_chars: int = _DEFAULT_CHUNK_OVERLAP_CHARS,
        local_detectors: list[PIIDetector] | None = None,
        code_safe_detectors: list[PIIDetector] | None = None,
        gazetteer: Gazetteer | None = None,
        allowlist: Allowlist | None = None,
        oracle_trigger: str = ORACLE_TRIGGER_GAZETTEER_HIT,
        oracle_enabled: bool = True,
    ) -> None:
        if oracle_enabled and corp_llm is None:
            raise ValueError(
                "oracle_enabled=True requires a corp_llm client; pass "
                "oracle_enabled=False for a client-less (local-first only) orchestrator"
            )
        self._corp_llm = corp_llm
        self._oracle_enabled = oracle_enabled
        self._mapping_store = mapping_store
        self._rules_loader = rules_loader
        self._sanitizer = sanitizer or default_sanitizer()
        self._cache_a_ttl = cache_a_ttl_seconds
        self._cache_b_ttl = cache_b_ttl_seconds
        self._size_threshold = size_threshold_bytes
        self._oversize_policy = normalize_oversize_policy(oversize_policy)
        self._oversize_deliver_teams = oversize_deliver_teams
        self._chunk_window = chunk_window_chars
        self._chunk_overlap = chunk_overlap_chars
        # code_safe_detectors is the subset allowed on raw CODE segments (regex /
        # checksum). Left None it defaults to every detector, which is what every
        # pre-B4 caller got; the composition root now passes the restricted list so
        # NER-class detectors — corp NER above all, which would ship source code to
        # an external service — never see a CODE segment.
        self._local = (
            LocalDetectionPass(local_detectors, code_safe_detectors=code_safe_detectors)
            if local_detectors
            else None
        )
        # NER-only pass for chunk mode: regex/checksum is pulled out and run over
        # the FULL text (H1), so the chunked pass carries only the size-bounded
        # detectors. None when no non-regex detector is configured.
        chunk_detectors = (
            [d for d in local_detectors if not isinstance(d, RegexChecksumDetector)]
            if local_detectors
            else []
        )
        chunk_code_safe = (
            [d for d in code_safe_detectors if not isinstance(d, RegexChecksumDetector)]
            if code_safe_detectors is not None
            else None
        )
        self._chunk_local = (
            LocalDetectionPass(chunk_detectors, code_safe_detectors=chunk_code_safe)
            if chunk_detectors
            else None
        )
        self._gazetteer = gazetteer
        self._allowlist = allowlist
        # F3: how widely the conditional oracle fires on a no-gazetteer-hit leaf.
        # Default gazetteer_hit preserves latency parity (oracle skipped on no hit).
        self._oracle_trigger = normalize_oracle_trigger(oracle_trigger)
        self._oracle_sample_pct = (
            _parse_sample_pct(self._oracle_trigger)
            if self._oracle_trigger.startswith(_ORACLE_TRIGGER_SAMPLED_PREFIX)
            else None
        )
        # Deterministic regex+checksum detector used both for the deliver-flag
        # rescan and for the full-text (seam-independent) pass in chunk mode.
        self._regex_checksum = RegexChecksumDetector()
        # Computed ONCE: it is per-request-invariant, and the local path has a
        # ~6ms p50 budget. None ⇒ fail closed: Cache A is off for this instance.
        self._policy_fingerprint = self._compute_policy_fingerprint()

    def _compute_policy_fingerprint(self) -> str | None:
        """Hash the effective redaction policy; None when it cannot be computed.

        Returning None disables Cache A rather than falling back to a constant —
        a constant would silently restore cross-policy replay (M4 fail-closed).
        """
        try:
            h = hashlib.sha256()
            _feed(h, [_POLICY_FINGERPRINT_VERSION])
            detectors = self._local.detectors if self._local is not None else ()
            code_detectors = self._local.code_detectors if self._local is not None else ()
            _feed(h, [b"detectors"])
            _feed(h, sorted(_detector_identity(d).encode("utf-8") for d in detectors))
            _feed(h, [b"code_safe"])
            _feed(h, sorted(_detector_identity(d).encode("utf-8") for d in code_detectors))
            _feed(h, [b"gazetteer"])
            if self._gazetteer is not None:
                _feed(h, [t.encode("utf-8") for t in self._gazetteer.term_signature()])
                # Matching lemmatizes; the CONFIGURED terms alone do not say what
                # this process can match. Taken here, at construction, because
                # the probe latches the lazy backends: the value cannot change
                # once the fingerprint has been written.
                _feed(h, [t.encode("utf-8") for t in self._gazetteer.lemmatizer_signature()])
            _feed(h, [b"allowlist"])
            if self._allowlist is not None:
                _feed(h, sorted(e.encode("utf-8") for e in self._allowlist.entries))
            _feed(h, [b"oracle_trigger", self._oracle_trigger.encode("utf-8")])
            return h.hexdigest()
        except Exception as exc:
            # Type name only — never the policy contents or any user text (M1-14).
            logger.warning(
                "cache_a_disabled reason=policy_fingerprint_failed exc=%s",
                type(exc).__name__,
            )
            return None

    async def sanitize(
        self,
        text: str,
        *,
        team_id: str,
        conversation_id: str,
        profile_fingerprint: str | None = None,
    ) -> SanitizeResult:
        # profile_fingerprint distinguishes the resolved profile bundle in the
        # SHARED Cache-A key (D3): two requests with identical team/rules/text but
        # different profiles must NOT reuse each other's sanitization, or a
        # RU-152FZ redaction can bleed to a US request. None keeps the no-profile
        # behavior within the current cache algorithm version.
        content_bytes = len(text.encode("utf-8"))
        logger.info(
            "sanitize_start team_id=%s conversation_id=%s content_bytes=%d",
            team_id,
            conversation_id,
            content_bytes,
        )

        if should_skip_sanitization(content_bytes, threshold_bytes=self._size_threshold):
            return await self._handle_oversize(
                text,
                team_id=team_id,
                conversation_id=conversation_id,
                content_bytes=content_bytes,
            )

        rules = await self._rules_loader.load(team_id)
        logger.info(
            "sanitize_rules_loaded team_id=%s conversation_id=%s rule_count=%d",
            team_id,
            conversation_id,
            len(rules.rules),
        )
        rule_matches = _rule_matches(rules, text)

        # No policy fingerprint ⇒ Cache A is off for this orchestrator: both the
        # read and the write are skipped, so nothing can be served or seeded under
        # a key that ignores the effective policy.
        content_hash = (
            _content_hash(
                team_id,
                rules,
                text,
                profile_fingerprint,
                self._oracle_enabled,
                policy_fingerprint=self._policy_fingerprint,
            )
            if self._policy_fingerprint is not None
            else None
        )

        cached = await self._mapping_store.get_dedup(content_hash) if content_hash else None
        if cached is not None:
            plan = _plan_replacements(text, cached.pairs, rule_matches)
            logger.info(
                "sanitize_cache_a_hit team_id=%s conversation_id=%s content_hash=%s pairs=%d",
                team_id,
                conversation_id,
                content_hash[:12],
                len(plan.pairs),
            )
            await self._record_conversation_mappings(conversation_id, plan.pairs)
            logger.info(
                "sanitize_cache_b_recorded team_id=%s conversation_id=%s "
                "pairs=%d ttl=%d source=cache_a",
                team_id,
                conversation_id,
                len(plan.pairs),
                self._cache_b_ttl,
            )
            return SanitizeResult(
                plan.sanitized_text,
                plan.pairs,
                cache_a_hit=True,
                skipped=False,
                applied_spans=plan.spans,
                rule_originals=plan.rule_originals,
            )

        logger.info(
            "sanitize_cache_a_miss team_id=%s conversation_id=%s content_hash=%s",
            team_id,
            conversation_id,
            content_hash[:12] if content_hash else "disabled",
        )

        detected = await self._detect(
            text,
            rules,
            team_id=team_id,
            conversation_id=conversation_id,
            rule_matches=rule_matches,
        )
        plan = _plan_replacements(text, detected.pairs, rule_matches)
        mapping = PlaceholderMapping(pairs=plan.pairs)

        if content_hash:
            await self._mapping_store.set_dedup(
                content_hash, mapping, ttl_seconds=self._cache_a_ttl
            )
            logger.info(
                "sanitize_cache_a_stored team_id=%s conversation_id=%s "
                "content_hash=%s ttl=%d pairs=%d",
                team_id,
                conversation_id,
                content_hash[:12],
                self._cache_a_ttl,
                len(plan.pairs),
            )

        await self._record_conversation_mappings(conversation_id, plan.pairs)
        logger.info(
            "sanitize_cache_b_recorded team_id=%s conversation_id=%s "
            "pairs=%d ttl=%d source=corp_llm",
            team_id,
            conversation_id,
            len(plan.pairs),
            self._cache_b_ttl,
        )

        return SanitizeResult(
            plan.sanitized_text,
            plan.pairs,
            cache_a_hit=False,
            skipped=False,
            applied_spans=plan.spans,
            rule_originals=plan.rule_originals,
        )

    async def _detect(
        self,
        text: str,
        rules: Rules,
        *,
        team_id: str,
        conversation_id: str,
        chunked: bool = False,
        rule_matches: tuple[_MatchedRule, ...] | None = None,
    ) -> StrategyResult:
        """Run the local-first cascade over one text leaf; return merged pairs.

        The size check, Cache A/B, and placeholder substitution live in the
        callers (`sanitize` / `_sanitize_chunked`) — this is the detection core.
        When *chunked*, the NER-only local pass is used (regex/checksum runs
        full-text in `_sanitize_chunked`, so it is not repeated per chunk — H1).

        *rule_matches*: `sanitize()` already computes this over the SAME full
        `text` before calling here, so it is threaded through to avoid a second
        ~O(rules x len(text)) pass. `_sanitize_chunked` calls this with
        `chunked=True` on a per-chunk substring (different text than the rule
        matches it computed for the full document), so that call site must
        NOT pass its own — leaving the default (None) recomputes for the chunk.
        """
        local_pass = self._chunk_local if chunked else self._local
        if rule_matches is None:
            rule_matches = _rule_matches(rules, text)
        rules_pairs = tuple(
            dict.fromkeys((match.original, match.replacement) for match in rule_matches)
        )
        if self._gazetteer is not None:
            # DP-4 + F3: run gazetteer + local first. The oracle is CONDITIONAL — a
            # gazetteer hit always runs it; CORP_LLM_ORACLE_TRIGGER may widen the
            # no-hit case (any_local_finding | sampled:<pct> | always).
            gaz_findings = await self._gazetteer.detect(text)
            local_findings = await local_pass.findings(text) if local_pass is not None else []
            # Findings are no longer dropped for overlapping a rule span (defect #5,
            # mechanism a) — `_plan_replacements` decides the winner by span length.
            combined = _deduplicate(local_findings + gaz_findings)
            # Rules are top-priority: computed once, applied in both sub-branches.
            oracle_runs = self._oracle_should_run(
                gaz_findings=gaz_findings,
                local_findings=local_findings,
                rules_pairs=rules_pairs,
                conversation_id=conversation_id,
                text=text,
            )
            if oracle_runs:
                logger.info(
                    "sanitize_branch=gazetteer oracle=yes trigger=%s gaz_hit=%s "
                    "team_id=%s conversation_id=%s gaz_hits=%d local_findings=%d",
                    self._oracle_trigger,
                    bool(gaz_findings),
                    team_id,
                    conversation_id,
                    len(gaz_findings),
                    len(local_findings),
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
                merged_pairs = _merge_local(rules_pairs + oracle_result.pairs, combined)
                _emit_gazetteer_proposal(oracle_result.pairs, combined, team_id, conversation_id)
            else:
                # Oracle skipped → rules still apply; local findings merged.
                logger.info(
                    "sanitize_branch=gazetteer oracle=skipped trigger=%s "
                    "team_id=%s conversation_id=%s local_findings=%d",
                    self._oracle_trigger,
                    team_id,
                    conversation_id,
                    len(local_findings),
                )
                merged_pairs = _merge_local(rules_pairs, combined)
            result = StrategyResult(pairs=merged_pairs)

        elif local_pass is not None:
            if self._oracle_enabled:
                # DP-3 path: oracle always on. Rules are applied directly (decision 3)
                # rather than only via the oracle round-trip, so deterministic rule
                # application never depends on the oracle honoring the system prompt.
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
                oracle_result = await self._call_corp_llm(text, rules)
                logger.info(
                    "sanitize_corp_llm_call_done team_id=%s conversation_id=%s pairs=%d",
                    team_id,
                    conversation_id,
                    len(oracle_result.pairs),
                )
                local_findings = await local_pass.findings(text)
                base_pairs = rules_pairs + oracle_result.pairs
                merged_pairs = _merge_local(base_pairs, local_findings)
            else:
                # Oracle disabled: rules no longer arrive via the oracle round-trip —
                # apply replace.md rules directly (rules_pairs, already computed
                # above), same precedence as the gazetteer branch's oracle-skipped
                # case (rules are the base; local findings merge in after, per
                # _merge_local's dedup-by-origin rules).
                local_findings = await local_pass.findings(text)
                logger.info(
                    "sanitize_branch=local_pass oracle=disabled team_id=%s conversation_id=%s "
                    "rule_matches=%d",
                    team_id,
                    conversation_id,
                    len(rules_pairs),
                )
                base_pairs = rules_pairs
                merged_pairs = _merge_local(base_pairs, local_findings)
            logger.info(
                "sanitize_local_pass team_id=%s conversation_id=%s "
                "oracle_pairs=%d local_findings=%d merged_pairs=%d",
                team_id,
                conversation_id,
                len(base_pairs),
                len(local_findings),
                len(merged_pairs),
            )
            result = StrategyResult(pairs=merged_pairs)

        else:
            # Legacy path: oracle only. Unreachable via config once Task-1 validation
            # is in place (oracle off requires local-first on) — defense in depth.
            if not self._oracle_enabled:
                raise RuntimeError(
                    "oracle disabled (CORP_LLM_ORACLE_ENABLED=0) but this orchestrator "
                    "has no local detection configured — nothing left to sanitize with; "
                    "pass local_detectors and/or a gazetteer, or enable the oracle"
                )
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
            oracle_result = await self._call_corp_llm(text, rules)
            logger.info(
                "sanitize_corp_llm_call_done team_id=%s conversation_id=%s pairs=%d",
                team_id,
                conversation_id,
                len(oracle_result.pairs),
            )
            result = StrategyResult(pairs=rules_pairs + oracle_result.pairs)

        if self._allowlist is not None:
            result = StrategyResult(pairs=self._allowlist.filter_pairs(result.pairs))
        return result

    def _oracle_should_run(
        self,
        *,
        gaz_findings: list[Finding],
        local_findings: list[Finding],
        rules_pairs: tuple[tuple[str, str], ...],
        conversation_id: str,
        text: str,
    ) -> bool:
        """Decide whether the conditional oracle runs for THIS leaf (F3 trigger).

        A gazetteer hit ALWAYS runs the oracle (ADR-003 baseline). The
        CORP_LLM_ORACLE_TRIGGER knob only widens the no-hit case: ``gazetteer_hit``
        keeps today's skip (latency parity); ``any_local_finding`` backstops a
        rules/regex/NER hit; ``sampled:<pct>`` runs a deterministic fraction;
        ``always`` runs every leaf.
        """
        if not self._oracle_enabled:
            return False
        if gaz_findings:
            return True
        if self._oracle_sample_pct is not None:
            return _sample_selected(f"{conversation_id}\x1f{text}", self._oracle_sample_pct)
        if self._oracle_trigger == ORACLE_TRIGGER_ALWAYS:
            return True
        if self._oracle_trigger == ORACLE_TRIGGER_ANY_LOCAL_FINDING:
            return bool(local_findings or rules_pairs)
        return False  # gazetteer_hit

    async def _handle_oversize(
        self,
        text: str,
        *,
        team_id: str,
        conversation_id: str,
        content_bytes: int,
    ) -> SanitizeResult:
        """Dispatch an oversize leaf on CORP_LLM_OVERSIZE_POLICY (default fail-closed).

        F1: the old skip-and-deliver path forwarded the ORIGINAL leaf UNSANITIZED.
        Every branch here either sanitizes the content or refuses egress.
        """
        policy = self._oversize_policy
        logger.warning(
            "sanitize_oversize team_id=%s conversation_id=%s size=%d threshold=%d policy=%s",
            team_id,
            conversation_id,
            content_bytes,
            self._size_threshold,
            policy,
        )
        if policy == OVERSIZE_CHUNK:
            rules = await self._rules_loader.load(team_id)
            return await self._sanitize_chunked(
                text, rules, team_id=team_id, conversation_id=conversation_id
            )
        if policy == OVERSIZE_DELIVER_FLAG and team_id in self._oversize_deliver_teams:
            rules = await self._rules_loader.load(team_id)
            return await self._deliver_oversize(
                text,
                rules,
                team_id=team_id,
                conversation_id=conversation_id,
                content_bytes=content_bytes,
            )
        # fail-closed (default), or deliver-flag requested without a team opt-in.
        logger.warning(
            "sanitize_oversize_blocked team_id=%s conversation_id=%s size=%d policy=%s",
            team_id,
            conversation_id,
            content_bytes,
            policy,
        )
        raise OversizeContentError(
            content_bytes=content_bytes, threshold_bytes=self._size_threshold
        )

    async def _sanitize_chunked(
        self,
        text: str,
        rules: Rules,
        *,
        team_id: str,
        conversation_id: str,
    ) -> SanitizeResult:
        """Full-text regex/checksum + sliding-window NER, one shared allocator.

        H1: regex/checksum matching is LINEAR and size-independent, and several
        of its patterns are UNBOUNDED (JWT, Bearer {20,}, sk-{32,}, DB URL {3,}),
        so a fixed chunk overlap can never guarantee they stay inside one window.
        Run them over the FULL text instead — seam position is then irrelevant for
        them, and no unbounded-pattern secret can survive a seam. Only the
        size-bounded NER (+ gazetteer + conditional oracle) pass is chunked; the
        overlap keeps a BOUNDED entity straddling a seam inside one window. All
        findings route through ONE RequestPlaceholderAllocator so the reassembled
        result keeps the request-wide bijection (M1-9).
        """
        allocator = RequestPlaceholderAllocator()
        pairs: list[tuple[str, str]] = []
        seen_originals: set[str] = set()

        # Rules establish both replacement priority and placeholder ownership
        # before findings from the full-text and chunked detector passes.
        global_rule_matches = _rule_matches(rules, text)
        global_rule_pairs = tuple(
            dict.fromkeys((match.original, match.replacement) for match in global_rule_matches)
        )
        if self._allowlist is not None:
            global_rule_pairs = self._allowlist.filter_pairs(global_rule_pairs)
        # Case-insensitive rule matching can produce several differently-cased
        # originals sharing one CONFIGURED replacement — exempt every
        # rule-derived original from this allocator's bijection re-mint
        # (computed once, before any _absorb call, since it only depends on
        # the static `rules`, not on absorb order).
        rule_exempt_originals = frozenset(original for original, _ in global_rule_pairs)

        def _absorb(
            candidate: tuple[tuple[str, str], ...],
        ) -> tuple[tuple[str, str], ...]:
            accepted: list[tuple[str, str]] = []
            for original, placeholder in allocator.remap(
                candidate, exempt_from_bijection=rule_exempt_originals
            ):
                if original in seen_originals:
                    continue
                seen_originals.add(original)
                pair = (original, placeholder)
                pairs.append(pair)
                accepted.append(pair)
            return tuple(accepted)

        canonical_rule_pairs = _absorb(global_rule_pairs)
        active_rule_origins = frozenset(original for original, _ in canonical_rule_pairs)

        # Full-text linear pass: unbounded secrets are matched whole regardless
        # of where a chunk seam falls.
        full_regex_findings = await self._regex_checksum.detect(text)
        _absorb(_merge_local((), full_regex_findings))

        # Chunked size-bounded pass (NER + gazetteer + conditional oracle).
        chunk_count = 0
        for chunk in _iter_overlapping_chunks(text, self._chunk_window, self._chunk_overlap):
            chunk_count += 1
            chunk_result = await self._detect(
                chunk, rules, team_id=team_id, conversation_id=conversation_id, chunked=True
            )
            _absorb(chunk_result.pairs)
        canonical = tuple(pairs)
        plan = _plan_replacements(
            text,
            canonical,
            global_rule_matches,
            active_rule_origins=active_rule_origins,
        )
        logger.info(
            "sanitize_oversize_chunked team_id=%s conversation_id=%s chunks=%d "
            "regex_findings=%d pairs=%d",
            team_id,
            conversation_id,
            chunk_count,
            len(full_regex_findings),
            len(plan.pairs),
        )
        await self._record_conversation_mappings(conversation_id, plan.pairs)
        return SanitizeResult(
            plan.sanitized_text,
            plan.pairs,
            cache_a_hit=False,
            skipped=False,
            applied_spans=plan.spans,
            rule_originals=plan.rule_originals,
        )

    async def _deliver_oversize(
        self,
        text: str,
        rules: Rules,
        *,
        team_id: str,
        conversation_id: str,
        content_bytes: int,
    ) -> SanitizeResult:
        """Opt-in deliver-flag: forward the original ONLY if a full rescan is clean.

        Runs the SAME detection the normal path would (regex+checksum + configured
        detectors + gazetteer + rules + the conditional oracle-on-gazetteer-hit) —
        not just the DLP guard's handful of secret regexes — so an oracle-only
        finding also blocks the egress (M2). Any finding fails closed; a clean scan
        delivers the original, flagged distinctly for the audit trail (M1).
        """
        findings = await self._scan_findings(text, rules, conversation_id=conversation_id)
        if findings:
            logger.warning(
                "sanitize_oversize_deliver_blocked team_id=%s conversation_id=%s "
                "size=%d finding_count=%d",
                team_id,
                conversation_id,
                content_bytes,
                len(findings),
            )
            raise OversizeContentError(
                content_bytes=content_bytes, threshold_bytes=self._size_threshold
            )
        logger.warning(
            "sanitize_oversize_delivered team_id=%s conversation_id=%s size=%d block_reason=%s",
            team_id,
            conversation_id,
            content_bytes,
            OVERSIZE_DELIVERED_REASON,
        )
        return SanitizeResult(
            text, (), cache_a_hit=False, skipped=True, block_reason=OVERSIZE_DELIVERED_REASON
        )

    async def _scan_findings(
        self, text: str, rules: Rules, *, conversation_id: str
    ) -> list[Finding]:
        # Always-on deterministic floor: regex+checksum runs regardless of which
        # local detectors are configured (defence-in-depth for the opt-in path).
        findings: list[Finding] = list(await self._regex_checksum.detect(text))
        gaz_findings: list[Finding] = []
        if self._gazetteer is not None:
            gaz_findings = list(await self._gazetteer.detect(text))
            findings.extend(gaz_findings)
        local_findings: list[Finding] = []
        if self._local is not None:
            local_findings = await self._local.findings(text)
            findings.extend(local_findings)
        rule_matches = _rule_matches(rules, text)
        rules_pairs = tuple(
            dict.fromkeys((match.original, match.replacement) for match in rule_matches)
        )
        for match in rule_matches:
            findings.append(
                Finding(
                    text=match.original,
                    label="RULE",
                    start=match.start,
                    end=match.end,
                    score=1.0,
                )
            )
        # M2: mirror the normal path's conditional oracle so an oracle-only finding
        # (no regex/local/gazetteer/rule hit) also blocks a deliver-flag egress.
        # Identical F3 trigger to `_detect`; no gazetteer configured → legacy
        # always-on oracle.
        oracle_runs = (
            self._oracle_should_run(
                gaz_findings=gaz_findings,
                local_findings=local_findings,
                rules_pairs=rules_pairs,
                conversation_id=conversation_id,
                text=text,
            )
            if self._gazetteer is not None
            else self._oracle_enabled
        )
        if oracle_runs:
            oracle_result = await self._call_corp_llm(text, rules)
            findings.extend(
                Finding(text=o, label="ORACLE", start=0, end=len(o), score=1.0)
                for o, _ in oracle_result.pairs
            )
        return findings

    async def _call_corp_llm(self, text: str, rules: Rules) -> StrategyResult:
        assert self._corp_llm is not None  # construction invariant: oracle_enabled needs a client
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


def _detector_identity(detector: object) -> str:
    """Stable cross-process identity for a detector instance.

    Import path + qualname, never ``hash()``/``id()``/default ``repr()``: those
    are process-local (PYTHONHASHSEED, memory address), so pods sharing one Redis
    would derive different keys and fragment — or worse, silently agree by
    accident on one pod pair and not another.

    The class alone can be a lie about coverage: two DualNerDetector instances
    hash identically whether or not their NER models loaded. A detector that
    implements ``PolicySignatureDetector`` therefore contributes its own strings
    on top of the class name; the probe lives on the detector, so the policy that
    only it knows never has to be reconstructed from another module's globals.
    Sorted here, so construction order inside a detector cannot fork the key.
    """
    cls = type(detector)
    base = f"{cls.__module__}.{cls.__qualname__}"
    probe = getattr(detector, "policy_signature", None)
    if probe is None:
        return base
    # A raising or non-string signature propagates: the caller turns that into
    # "no fingerprint" ⇒ Cache A off, never a key that ignores the capability.
    parts = probe()
    if not all(isinstance(p, str) for p in parts):
        raise TypeError(f"policy_signature of {base} must return strings")
    return "\x1e".join((base, *sorted(parts)))


def _feed(h: hashlib._Hash, parts: list[bytes]) -> None:
    """Absorb length-prefixed parts so no concatenation of items can collide."""
    for part in parts:
        h.update(len(part).to_bytes(8, "big"))
        h.update(part)
    h.update(b"\x1c")


def _content_hash(
    team_id: str,
    rules: Rules,
    text: str,
    profile_fingerprint: str | None = None,
    oracle_enabled: bool | None = None,
    *,
    policy_fingerprint: str | None = None,
) -> str:
    h = hashlib.sha256()
    # Retires every Cache-A entry written by a build with different substitution
    # semantics or narrower detector coverage — see the constant for what counts
    # as a boundary and why replaying such an entry is a leak.
    h.update(_CACHE_A_ALGORITHM_VERSION)
    h.update(b"\x1c")
    h.update(team_id.encode("utf-8"))
    h.update(b"\x1f")
    for rule in rules.rules:
        h.update(rule.pattern.encode("utf-8"))
        h.update(b"\x1e")
        h.update(rule.replacement.encode("utf-8"))
        h.update(b"\x1f")
    h.update(b"\x1d")
    h.update(text.encode("utf-8"))
    # Fold the profile fingerprint only when present: None adds no profile-specific
    # discriminator within the current cache algorithm version.
    if profile_fingerprint is not None:
        h.update(b"\x1c")
        h.update(profile_fingerprint.encode("utf-8"))
    # Fold the oracle mode so a cache entry written with the oracle OFF can't be
    # replayed after the oracle is turned back ON (and vice versa) — an oracle
    # re-enable must not silently skip oracle-only findings until the TTL expires.
    if oracle_enabled is not None:
        h.update(b"\x1b")
        h.update(b"oracle:1" if oracle_enabled else b"oracle:0")
    # Effective-policy discriminator: covers coverage changes made at RUNTIME
    # (e.g. CORP_NER_ENABLED adding a detector) that the build-time algorithm
    # version constant cannot see. None only on direct legacy calls — the request
    # path never keys without it (it disables Cache A instead).
    if policy_fingerprint is not None:
        h.update(b"\x1a")
        h.update(policy_fingerprint.encode("utf-8"))
    return h.hexdigest()


def _iter_overlapping_chunks(text: str, window: int, overlap: int) -> Iterator[str]:
    """Yield fixed-size windows advancing by (window - overlap).

    Any substring no longer than *overlap* is fully contained in at least one
    window, so a secret straddling a chunk seam still matches on rescan.
    """
    step = window - overlap
    if step <= 0:
        raise ValueError(f"chunk window {window} must exceed overlap {overlap}")
    n = len(text)
    start = 0
    while True:
        yield text[start : start + window]
        if start + window >= n:
            return
        start += step


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
