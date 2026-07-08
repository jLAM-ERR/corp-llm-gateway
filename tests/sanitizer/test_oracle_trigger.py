"""Tests for DP-4: conditional oracle trigger via Gazetteer.

Verifies:
- gazetteer HIT  → oracle called once; terms redacted with [LABEL_NNN]
- gazetteer NO-HIT → oracle NOT called; local findings still applied
- bijection holds (M1-9) in both paths
- default (gazetteer=None) leaves DP-3/legacy behaviour unchanged
"""

from __future__ import annotations

import json

import httpx
import pytest

from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.payload import OversizeContentError
from corp_llm_gateway.rules import Gazetteer, Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import InMemoryMappingStore


class _StaticRulesLoader(RulesLoader):
    async def load(self, team_id: str) -> Rules:
        return Rules(rules=())


class _StaticFindingDetector(PIIDetector):
    """Returns a fixed list of findings regardless of text."""

    def __init__(self, findings: list[Finding]) -> None:
        self._findings = findings

    async def detect(self, text: str) -> list[Finding]:
        return list(self._findings)


def _client_with_counter(
    pairs: list[tuple[str, str]],
) -> tuple[CorpLlmClient, list[int]]:
    """Return a mock corp-LLM client that returns *pairs* and counts calls."""
    call_count: list[int] = [0]

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
    return CorpLlmClient("https://corp-llm.example", model="m", http=http), call_count


# ---------------------------------------------------------------------------
# Gazetteer HIT → oracle called
# ---------------------------------------------------------------------------


async def test_gazetteer_hit_calls_oracle() -> None:
    """When the gazetteer matches, the oracle must be called exactly once."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    client, call_count = _client_with_counter([("Project Polaris", "[PRODUCT_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
    )
    result = await orch.sanitize(
        "We are working on Project Polaris this sprint.",
        team_id="t1",
        conversation_id="c1",
    )
    assert call_count[0] == 1, "oracle must be called on a gazetteer hit"
    assert "Project Polaris" not in result.sanitized_text


async def test_gazetteer_hit_product_redacted_with_label() -> None:
    """Product term gets a [PRODUCT_NNN] placeholder in the output."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    client, _ = _client_with_counter([("Project Polaris", "[PRODUCT_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
    )
    result = await orch.sanitize(
        "Roadmap for Project Polaris.",
        team_id="t1",
        conversation_id="c1",
    )
    # The oracle returned PRODUCT_001; verify it ends up in the output
    placeholders = [p for _, p in result.pairs]
    assert any("PRODUCT" in p for p in placeholders)


async def test_gazetteer_hit_regulated_term() -> None:
    """Regulated term (AML) triggers oracle and appears in pairs."""
    gaz = Gazetteer({"AML": "REGULATED"})
    client, call_count = _client_with_counter([("AML", "[REGULATED_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
    )
    result = await orch.sanitize(
        "Our AML compliance is under review.",
        team_id="t1",
        conversation_id="c1",
    )
    assert call_count[0] == 1
    assert "AML" not in result.sanitized_text


# ---------------------------------------------------------------------------
# Gazetteer NO-HIT → oracle skipped
# ---------------------------------------------------------------------------


async def test_gazetteer_nohit_oracle_not_called() -> None:
    """When no gazetteer term matches, the oracle must NOT be called."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
    )
    result = await orch.sanitize(
        "Hello world, nothing sensitive here.",
        team_id="t1",
        conversation_id="c1",
    )
    assert call_count[0] == 0, "oracle must NOT be called when gazetteer has no hit"
    # No redaction
    assert result.sanitized_text == "Hello world, nothing sensitive here."
    assert result.pairs == ()


async def test_gazetteer_nohit_local_findings_still_applied() -> None:
    """Without a gazetteer hit, local detector findings are still applied."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    email_finding = Finding("bob@example.com", "EMAIL", 6, 21, 0.95)
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        local_detectors=[_StaticFindingDetector([email_finding])],
    )
    text = "hello bob@example.com is here"
    result = await orch.sanitize(text, team_id="t1", conversation_id="c1")
    assert call_count[0] == 0, "oracle must NOT be called"
    assert "bob@example.com" not in result.sanitized_text
    assert any("EMAIL" in p for _, p in result.pairs)


async def test_gazetteer_nohit_local_findings_bijection() -> None:
    """Without oracle, local findings still produce unique placeholder:original pairs."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    f1 = Finding("alice", "PERSON", 0, 5, 0.9)
    f2 = Finding("bob", "PERSON", 6, 9, 0.9)
    client, _ = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        local_detectors=[_StaticFindingDetector([f1, f2])],
    )
    result = await orch.sanitize("alice bob plain text", team_id="t1", conversation_id="c1")
    originals = [o for o, _ in result.pairs]
    placeholders = [p for _, p in result.pairs]
    assert len(originals) == len(set(originals)), "duplicate original in pairs"
    assert len(placeholders) == len(set(placeholders)), "placeholder collision"


# ---------------------------------------------------------------------------
# Bijection invariant with oracle running (gazetteer HIT)
# ---------------------------------------------------------------------------


async def test_gazetteer_hit_bijection_with_local_and_oracle() -> None:
    """With gazetteer hit: oracle + local both contribute; bijection must hold."""
    gaz = Gazetteer({"AML": "REGULATED"})
    # Oracle finds a person name; local detector finds an email
    oracle_pairs = [("AML", "[REGULATED_001]"), ("Alice Smith", "[PERSON_001]")]
    email_finding = Finding("alice@corp.lan", "EMAIL", 30, 44, 0.95)
    client, call_count = _client_with_counter(oracle_pairs)
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        local_detectors=[_StaticFindingDetector([email_finding])],
    )
    text = "AML review by Alice Smith — contact alice@corp.lan"
    result = await orch.sanitize(text, team_id="t1", conversation_id="c1")
    assert call_count[0] == 1
    originals = [o for o, _ in result.pairs]
    placeholders = [p for _, p in result.pairs]
    assert len(originals) == len(set(originals)), "duplicate original"
    assert len(placeholders) == len(set(placeholders)), "placeholder collision"
    # All three must be redacted
    assert "AML" not in result.sanitized_text
    assert "alice@corp.lan" not in result.sanitized_text


# ---------------------------------------------------------------------------
# Cache-A reuse across branches
# ---------------------------------------------------------------------------


async def test_gazetteer_hit_cache_a_oracle_called_once() -> None:
    """Second identical call hits cache-A; oracle called only once total."""
    gaz = Gazetteer({"AML": "REGULATED"})
    client, call_count = _client_with_counter([("AML", "[REGULATED_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
    )
    text = "AML procedure"
    await orch.sanitize(text, team_id="t1", conversation_id="c1")
    r2 = await orch.sanitize(text, team_id="t1", conversation_id="c2")
    assert call_count[0] == 1, "oracle must be called only once (cache-A on second)"
    assert r2.cache_a_hit is True


# ---------------------------------------------------------------------------
# Default (gazetteer=None) stays identical to DP-3 / legacy
# ---------------------------------------------------------------------------


async def test_default_no_gazetteer_oracle_always_called() -> None:
    """gazetteer=None ⇒ DP-3 path: oracle called unconditionally."""
    client, call_count = _client_with_counter([("alice", "[NAME_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        # No gazetteer, no local_detectors → legacy path
    )
    result = await orch.sanitize("hello alice", team_id="t1", conversation_id="c1")
    assert call_count[0] == 1
    assert result.sanitized_text == "hello [NAME_001]"
    assert result.pairs == (("alice", "[NAME_001]"),)


async def test_default_no_gazetteer_with_local_detectors_oracle_called() -> None:
    """gazetteer=None + local_detectors ⇒ DP-3 path: oracle always called."""
    email_finding = Finding("bob@example.com", "EMAIL", 6, 21, 0.95)
    client, call_count = _client_with_counter([("alice", "[NAME_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        local_detectors=[_StaticFindingDetector([email_finding])],
        # gazetteer defaults to None
    )
    await orch.sanitize("alice bob@example.com", team_id="t1", conversation_id="c1")
    assert call_count[0] == 1, "DP-3 path must always call the oracle"


async def test_gazetteer_nohit_cache_a_oracle_never_called() -> None:
    """Two identical no-hit requests: oracle stays at zero calls."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
    )
    text = "Just plain text."
    await orch.sanitize(text, team_id="t1", conversation_id="c1")
    r2 = await orch.sanitize(text, team_id="t1", conversation_id="c2")
    assert call_count[0] == 0, "oracle must never be called in no-hit path"
    assert r2.cache_a_hit is True


# ---------------------------------------------------------------------------
# Marking detection
# ---------------------------------------------------------------------------


async def test_confidential_mark_detected_triggers_oracle() -> None:
    """A confidentiality marking triggers the oracle via gazetteer hit."""
    gaz = Gazetteer({"Confidential": "CONFIDENTIAL_MARK"})
    client, call_count = _client_with_counter([("Confidential", "[CONFIDENTIAL_MARK_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
    )
    result = await orch.sanitize(
        "Confidential — do not distribute.",
        team_id="t1",
        conversation_id="c1",
    )
    assert call_count[0] == 1
    assert "Confidential" not in result.sanitized_text


# ---------------------------------------------------------------------------
# Round-trip desanitization
# ---------------------------------------------------------------------------


async def test_round_trip_restores_original_gazetteer_hit() -> None:
    """Applying pairs from the gazetteer-hit path is fully reversible."""
    gaz = Gazetteer({"AML": "REGULATED"})
    oracle_pairs = [("AML", "[REGULATED_001]")]
    client, _ = _client_with_counter(oracle_pairs)
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
    )
    original_text = "Reviewing AML controls."
    result = await orch.sanitize(original_text, team_id="t1", conversation_id="c1")
    restored = result.sanitized_text
    for orig, placeholder in result.pairs:
        restored = restored.replace(placeholder, orig)
    assert restored == original_text


async def test_round_trip_restores_original_gazetteer_nohit() -> None:
    """Applying pairs from the no-hit path (local only) is fully reversible."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    email = Finding("dev@corp.lan", "EMAIL", 6, 18, 0.95)
    client, _ = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        local_detectors=[_StaticFindingDetector([email])],
    )
    original_text = "hello dev@corp.lan end"
    result = await orch.sanitize(original_text, team_id="t1", conversation_id="c1")
    restored = result.sanitized_text
    for orig, placeholder in result.pairs:
        restored = restored.replace(placeholder, orig)
    assert restored == original_text


# ---------------------------------------------------------------------------
# Size-threshold short-circuit unaffected
# ---------------------------------------------------------------------------


async def test_size_threshold_fails_closed_before_gazetteer() -> None:
    """Oversize input is short-circuited (fail-closed) before gazetteer/oracle run."""
    gaz = Gazetteer({"AML": "REGULATED"})
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        size_threshold_bytes=5,
    )
    with pytest.raises(OversizeContentError):
        await orch.sanitize("AML oversize text here", team_id="t1", conversation_id="c1")
    assert call_count[0] == 0


# ---------------------------------------------------------------------------
# Gazetteer-only (no local detectors) still works
# ---------------------------------------------------------------------------


async def test_gazetteer_without_local_detectors_hit() -> None:
    """Gazetteer works with no local_detectors supplied."""
    gaz = Gazetteer({"CFT": "REGULATED"})
    client, call_count = _client_with_counter([("CFT", "[REGULATED_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        # local_detectors not supplied
    )
    result = await orch.sanitize("CFT controls", team_id="t1", conversation_id="c1")
    assert call_count[0] == 1
    assert "CFT" not in result.sanitized_text


async def test_gazetteer_without_local_detectors_nohit() -> None:
    """Gazetteer with no local_detectors and no hit: clean pass."""
    gaz = Gazetteer({"CFT": "REGULATED"})
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
    )
    result = await orch.sanitize("Nothing here.", team_id="t1", conversation_id="c1")
    assert call_count[0] == 0
    assert result.pairs == ()
    assert result.sanitized_text == "Nothing here."
