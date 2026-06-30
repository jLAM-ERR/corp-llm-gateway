"""Tests for DP-3: LocalDetectionPass + merged local+oracle sanitization."""

from __future__ import annotations

import json

import httpx

from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.local_pass import LocalDetectionPass
from corp_llm_gateway.sanitizer.orchestrator import _merge_local
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
