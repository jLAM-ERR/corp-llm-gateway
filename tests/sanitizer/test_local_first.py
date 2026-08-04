"""Tests for DP-3: LocalDetectionPass + merged local+oracle sanitization."""

from __future__ import annotations

import json

import httpx
import pytest

from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.detectors.dual_ner import NerUnavailableError
from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.local_pass import DetectorContractError, LocalDetectionPass
from corp_llm_gateway.sanitizer.orchestrator import _merge_local
from corp_llm_gateway.sanitizer.segmenter import SegmentKind, split_segments
from corp_llm_gateway.storage import InMemoryMappingStore


class _StaticRulesLoader(RulesLoader):
    async def load(self, team_id: str) -> Rules:
        return Rules(rules=())


def _client_returning_pairs(
    pairs: list[tuple[str, str]],
) -> tuple[CorpLlmClient, list[int]]:
    call_count = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": SANITIZE_TOOL_NAME,
                                        "arguments": json.dumps(
                                            {
                                                "pairs": [
                                                    {"original": o, "replacement": r}
                                                    for o, r in pairs
                                                ]
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = CorpLlmClient("https://corp-llm.example", model="m", http=http)
    return client, call_count


class _StaticFindingDetector(PIIDetector):
    """Returns a fixed list of findings regardless of text."""

    def __init__(self, findings: list[Finding]) -> None:
        self._findings = findings

    async def detect(self, text: str) -> list[Finding]:
        return self._findings


# ---- LocalDetectionPass unit tests ----------------------------------------


async def test_local_pass_merges_concurrent_detectors() -> None:
    f1 = Finding("alice", "PERSON", 0, 5, 0.9)
    f2 = Finding("example.corp.lan", "HOSTNAME", 6, 22, 0.7)
    lp = LocalDetectionPass([_StaticFindingDetector([f1]), _StaticFindingDetector([f2])])
    findings = await lp.findings("alice example.corp.lan")
    texts = {f.text for f in findings}
    assert "alice" in texts
    assert "example.corp.lan" in texts


async def test_local_pass_empty_detectors() -> None:
    assert await LocalDetectionPass([]).findings("any text") == []


async def test_local_pass_deduplicates_overlapping() -> None:
    f1 = Finding("alice", "PERSON", 0, 5, 0.9)
    f2 = Finding("alic", "PERSON", 0, 4, 0.5)  # overlaps f1, lower score
    lp = LocalDetectionPass([_StaticFindingDetector([f1, f2])])
    findings = await lp.findings("alice")
    assert len(findings) == 1
    assert findings[0].text == "alice"


# ---- batch seam (B3) -------------------------------------------------------

_BATCH_TEXT = "alpha beta\n```py\nx = 1  # note alice\n```\ncontact bob here"


class _BatchDetector(PIIDetector):
    """Batch-capable fake: findings keyed by the exact text handed to it."""

    def __init__(self, per_text: dict[str, list[Finding]] | None = None) -> None:
        self._per_text = per_text or {}
        self.batch_calls: list[list[str]] = []
        self.detect_calls: list[str] = []

    async def detect(self, text: str) -> list[Finding]:
        self.detect_calls.append(text)
        return list(self._per_text.get(text, []))

    async def detect_batch(self, texts: list[str]) -> list[list[Finding]]:
        self.batch_calls.append(list(texts))
        return [list(self._per_text.get(t, [])) for t in texts]


def _segment_of(text: str, needle: str) -> tuple[int, int]:
    """Return (segment start, offset of *needle* inside that segment)."""
    idx = text.index(needle)
    seg = next(s for s in split_segments(text) if s.start <= idx < s.end)
    return seg.start, idx - seg.start


async def test_local_pass_batch_offsets_corrected_to_absolute() -> None:
    """Batch findings carry segment-relative offsets and must be re-based."""
    seg_start, rel = _segment_of(_BATCH_TEXT, "bob")
    assert seg_start > 0, "fixture must use a segment that does not start at 0"
    seg_text = _BATCH_TEXT[seg_start : seg_start + len("\ncontact bob here")]
    det = _BatchDetector({seg_text: [Finding("bob", "PERSON", rel, rel + 3, 0.9)]})

    findings = await LocalDetectionPass([det]).findings(_BATCH_TEXT)

    assert len(det.batch_calls) == 1, "one batched call for all eligible segments"
    assert det.detect_calls == [], "batch-capable detector must not be called per segment"
    f = next(f for f in findings if f.label == "PERSON")
    assert f.start == _BATCH_TEXT.index("bob")
    assert _BATCH_TEXT[f.start : f.end] == f.text


async def test_local_pass_batch_sends_every_segment_by_default() -> None:
    det = _BatchDetector()
    await LocalDetectionPass([det]).findings(_BATCH_TEXT)
    assert det.batch_calls == [[s.text for s in split_segments(_BATCH_TEXT)]]


async def test_local_pass_batch_skips_code_when_not_code_safe() -> None:
    """PROSE + COMMENT only, expressed through code_safe_detectors (corp NER)."""
    det = _BatchDetector()
    regex = RegexChecksumDetector()
    lp = LocalDetectionPass([det, regex], code_safe_detectors=[regex])

    await lp.findings(_BATCH_TEXT)

    assert len(det.batch_calls) == 1
    sent = det.batch_calls[0]
    segments = split_segments(_BATCH_TEXT)
    assert sent == [s.text for s in segments if s.kind is not SegmentKind.CODE]
    assert "# note alice" in sent, "COMMENT segments stay in scope"
    assert "x = 1  " not in sent, "CODE segments must not reach the batch detector"


async def test_local_pass_batch_and_sequential_detectors_merge() -> None:
    inn = "7707083893"
    text = f"{_BATCH_TEXT} ИНН {inn}"
    seg = next(s for s in split_segments(text) if s.start <= text.index("bob") < s.end)
    rel = text.index("bob") - seg.start
    batch = _BatchDetector({seg.text: [Finding("bob", "PERSON", rel, rel + 3, 0.9)]})
    lp = LocalDetectionPass([batch, RegexChecksumDetector()])

    findings = await lp.findings(text)

    texts = {f.text for f in findings}
    assert len(batch.batch_calls) == 1 and batch.detect_calls == []
    assert "bob" in texts, "batched detector contributes"
    assert inn in texts, "per-segment detector still runs"
    for f in findings:
        assert text[f.start : f.end] == f.text


async def test_local_pass_batch_result_dedupes_against_sequential() -> None:
    """Overlapping batch + per-segment findings collapse in the same pass."""
    text = "alice"
    batch = _BatchDetector({text: [Finding("alic", "PERSON", 0, 4, 0.5)]})
    sequential = _StaticFindingDetector([Finding("alice", "PERSON", 0, 5, 0.9)])
    lp = LocalDetectionPass([batch, sequential])

    findings = await lp.findings(text)

    assert len(findings) == 1
    assert findings[0].text == "alice"


async def test_local_pass_batch_not_called_for_empty_text() -> None:
    det = _BatchDetector()
    assert await LocalDetectionPass([det]).findings("") == []
    assert det.batch_calls == []


class _MiscountingBatchDetector(_BatchDetector):
    async def detect_batch(self, texts: list[str]) -> list[list[Finding]]:
        self.batch_calls.append(list(texts))
        return [[] for _ in texts][:-1]


class _OutOfRangeBatchDetector(_BatchDetector):
    async def detect_batch(self, texts: list[str]) -> list[list[Finding]]:
        self.batch_calls.append(list(texts))
        return [[Finding("x", "PERSON", 0, len(t) + 1, 1.0)] for t in texts]


async def test_local_pass_batch_length_mismatch_fails_closed() -> None:
    with pytest.raises(ValueError, match="detect_batch"):
        await LocalDetectionPass([_MiscountingBatchDetector()]).findings(_BATCH_TEXT)


async def test_local_pass_batch_out_of_range_offset_fails_closed() -> None:
    with pytest.raises(ValueError, match="offset"):
        await LocalDetectionPass([_OutOfRangeBatchDetector()]).findings("alice bob")


async def test_local_pass_batch_error_message_has_no_user_text() -> None:
    """M1-14: a protocol violation must not put user content in the exception."""
    secret = "s3cret-passphrase"
    with pytest.raises(ValueError) as exc:
        await LocalDetectionPass([_OutOfRangeBatchDetector()]).findings(secret)
    assert secret not in str(exc.value)


class _TextMismatchBatchDetector(_BatchDetector):
    """In-range offsets, but Finding.text names a different original."""

    async def detect_batch(self, texts: list[str]) -> list[list[Finding]]:
        self.batch_calls.append(list(texts))
        return [[Finding("bob", "PERSON", 0, 5, 1.0)] for _ in texts]


async def test_local_pass_batch_text_mismatch_fails_closed() -> None:
    """In-range offsets whose slice is not Finding.text corrupt the M1-9 bijection."""
    det = _TextMismatchBatchDetector()
    with pytest.raises(DetectorContractError, match="does not match"):
        await LocalDetectionPass([det]).findings("alice smith")


async def test_local_pass_batch_text_mismatch_is_ner_classified() -> None:
    """The hook's fail-closed handler catches NerUnavailableError, not bare ValueError."""
    assert issubclass(DetectorContractError, NerUnavailableError)
    with pytest.raises(NerUnavailableError):
        await LocalDetectionPass([_TextMismatchBatchDetector()]).findings("alice smith")


async def test_local_pass_batch_text_mismatch_error_has_no_user_text() -> None:
    """M1-14: neither the segment slice nor the finding's own text may appear."""
    secret = "s3cret-passphrase"
    with pytest.raises(DetectorContractError) as exc:
        await LocalDetectionPass([_TextMismatchBatchDetector()]).findings(secret)
    msg = str(exc.value)
    assert secret not in msg
    assert secret[:5] not in msg, "the segment slice is user content too"
    assert "bob" not in msg, "the finding's own text is detector-reported user content"


# ---- _merge_local unit tests -----------------------------------------------


def test_merge_local_adds_novel_finding() -> None:
    oracle_pairs = (("alice", "[PERSON_001]"),)
    f = Finding("bob@example.com", "EMAIL", 6, 21, 0.95)
    merged = _merge_local(oracle_pairs, [f])
    originals = [o for o, _ in merged]
    placeholders = [p for _, p in merged]
    assert "alice" in originals
    assert "bob@example.com" in originals
    assert len(placeholders) == len(set(placeholders)), "placeholder collision"


def test_merge_local_skips_duplicate_original() -> None:
    oracle_pairs = (("alice", "[PERSON_001]"),)
    f = Finding("alice", "PERSON", 0, 5, 0.9)  # already in oracle
    merged = _merge_local(oracle_pairs, [f])
    assert merged == oracle_pairs


def test_merge_local_no_placeholder_collision_same_label() -> None:
    oracle_pairs = (("alice", "[PERSON_001]"),)
    f = Finding("bob", "PERSON", 6, 9, 0.9)
    merged = _merge_local(oracle_pairs, [f])
    placeholders = [p for _, p in merged]
    assert len(placeholders) == len(set(placeholders))
    # Must not reuse PERSON_001
    bob_placeholder = next(p for o, p in merged if o == "bob")
    assert bob_placeholder != "[PERSON_001]"


def test_merge_local_bijection_invariant() -> None:
    """No placeholder maps to two originals; no original appears twice."""
    oracle_pairs = (("secret", "[API_KEY_001]"), ("10.0.0.1", "[IP_ADDRESS_001]"))
    findings = [
        Finding("newval", "API_KEY", 0, 6, 1.0),
        Finding("10.0.0.1", "IP_ADDRESS", 7, 14, 1.0),  # duplicate original
    ]
    merged = _merge_local(oracle_pairs, findings)
    originals = [o for o, _ in merged]
    placeholders = [p for _, p in merged]
    assert len(originals) == len(set(originals)), "duplicate original"
    assert len(placeholders) == len(set(placeholders)), "duplicate placeholder"


def test_merge_local_oracle_placeholder_blocked_for_reuse() -> None:
    """A collision case: local label counter would produce an already-used placeholder."""
    # Oracle used API_KEY_001 for a differently-named original
    oracle_pairs = (("old-secret", "[API_KEY_001]"),)
    # Local wants to add "new-secret" as API_KEY — must not reuse API_KEY_001
    f = Finding("new-secret", "API_KEY", 0, 10, 1.0)
    merged = _merge_local(oracle_pairs, [f])
    new_placeholder = next(p for o, p in merged if o == "new-secret")
    assert new_placeholder != "[API_KEY_001]"
    assert new_placeholder == "[API_KEY_002]"


# ---- Orchestrator integration tests ----------------------------------------


async def test_orchestrator_merges_local_with_oracle() -> None:
    """Oracle misses email; local detector catches it — result redacts both."""
    oracle_pairs = [("alice", "[PERSON_001]")]
    client, _ = _client_returning_pairs(oracle_pairs)
    email_finding = Finding("bob@example.com", "EMAIL", 6, 21, 0.95)
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        local_detectors=[_StaticFindingDetector([email_finding])],
    )
    result = await orch.sanitize("alice bob@example.com", team_id="t1", conversation_id="c1")
    assert "alice" not in result.sanitized_text
    assert "bob@example.com" not in result.sanitized_text
    assert "[PERSON_001]" in result.sanitized_text
    assert "bob@example.com" in {o for o, _ in result.pairs}


async def test_orchestrator_no_local_detectors_unchanged() -> None:
    """Default (no local_detectors) ⇒ output identical to oracle-only."""
    oracle_pairs = [("alice", "[NAME_001]")]
    client, _ = _client_returning_pairs(oracle_pairs)
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
    )
    result = await orch.sanitize("hello alice", team_id="t1", conversation_id="c1")
    assert result.sanitized_text == "hello [NAME_001]"
    assert result.pairs == (("alice", "[NAME_001]"),)


async def test_cache_a_stores_merged_pairs() -> None:
    """Second call hits cache-A with merged set; oracle is called only once."""
    oracle_pairs = [("alice", "[PERSON_001]")]
    client, call_count = _client_returning_pairs(oracle_pairs)
    email_finding = Finding("bob@example.com", "EMAIL", 6, 21, 0.95)
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        local_detectors=[_StaticFindingDetector([email_finding])],
    )
    await orch.sanitize("alice bob@example.com", team_id="t1", conversation_id="c1")
    r2 = await orch.sanitize("alice bob@example.com", team_id="t1", conversation_id="c2")
    assert call_count[0] == 1, "oracle must be called only once (cache-A hit on second)"
    assert r2.cache_a_hit is True
    originals2 = {o for o, _ in r2.pairs}
    assert "alice" in originals2
    assert "bob@example.com" in originals2


async def test_round_trip_restores_original() -> None:
    """Applying then reversing the merged pairs restores the original text."""
    text = "alice contacted bob@example.com"
    oracle_pairs = [("alice", "[PERSON_001]")]
    client, _ = _client_returning_pairs(oracle_pairs)
    email_finding = Finding("bob@example.com", "EMAIL", 7, 22, 0.95)
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        local_detectors=[_StaticFindingDetector([email_finding])],
    )
    result = await orch.sanitize(text, team_id="t1", conversation_id="c1")
    restored = result.sanitized_text
    for original, placeholder in result.pairs:
        restored = restored.replace(placeholder, original)
    assert restored == text


async def test_regex_detector_catches_inn_oracle_misses() -> None:
    """RegexChecksumDetector finds a valid ИНН that the oracle doesn't return."""
    inn = "7707083893"  # Sberbank ИНН-10; passes checksum
    client, _ = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        local_detectors=[RegexChecksumDetector()],
    )
    result = await orch.sanitize(f"ИНН организации: {inn}.", team_id="t1", conversation_id="c1")
    assert inn not in result.sanitized_text, "ИНН must be redacted by local pass"


async def test_oracle_still_called_with_local_pass_enabled() -> None:
    """Oracle is unconditionally on (DP-3 invariant) even with local detectors."""
    oracle_pairs = [("alice", "[PERSON_001]")]
    client, call_count = _client_returning_pairs(oracle_pairs)
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        local_detectors=[_StaticFindingDetector([])],
    )
    await orch.sanitize("alice", team_id="t1", conversation_id="c1")
    assert call_count[0] == 1, "oracle must be called even when local pass is active"
