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
from corp_llm_gateway.rules import Gazetteer, Rule, Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.orchestrator import _sample_selected, normalize_oracle_trigger
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


# ---------------------------------------------------------------------------
# F3: CORP_LLM_ORACLE_TRIGGER — broaden the conditional oracle
# ---------------------------------------------------------------------------


class _RuleLoader(RulesLoader):
    def __init__(self, rules: Rules) -> None:
        self._rules = rules

    async def load(self, team_id: str) -> Rules:
        return self._rules


# ── default (gazetteer_hit) — latency parity, no behavior change ─────────────


async def test_trigger_default_is_gazetteer_hit_local_does_not_wake_oracle() -> None:
    """No oracle_trigger passed → default gazetteer_hit: a local finding on a
    no-gazetteer-hit leaf does NOT wake the oracle (ADR-003 latency parity)."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    email = Finding("bob@example.com", "EMAIL", 6, 21, 0.95)
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        local_detectors=[_StaticFindingDetector([email])],
    )
    result = await orch.sanitize("hello bob@example.com here", team_id="t1", conversation_id="c1")
    assert call_count[0] == 0
    assert "bob@example.com" not in result.sanitized_text  # local still applied


# ── any_local_finding — backstop an incomplete local detection ───────────────


async def test_trigger_any_local_finding_local_wakes_oracle() -> None:
    """any_local_finding: a local (regex/NER) finding on a no-gaz-hit leaf wakes the oracle."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    email = Finding("bob@example.com", "EMAIL", 6, 21, 0.95)
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        local_detectors=[_StaticFindingDetector([email])],
        oracle_trigger="any_local_finding",
    )
    result = await orch.sanitize("hello bob@example.com here", team_id="t1", conversation_id="c1")
    assert call_count[0] == 1
    assert "bob@example.com" not in result.sanitized_text


async def test_trigger_any_local_finding_rule_wakes_oracle() -> None:
    """any_local_finding: a matched rule (no gaz hit, no local detector) wakes the oracle."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    rules = Rules(rules=(Rule("widget", "[WIDGET]"),))
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _RuleLoader(rules),
        gazetteer=gaz,
        oracle_trigger="any_local_finding",
    )
    await orch.sanitize("the widget is broken", team_id="t1", conversation_id="c1")
    assert call_count[0] == 1


async def test_trigger_any_local_finding_clean_skips_oracle() -> None:
    """any_local_finding: nothing fired (no gaz/local/rule) → oracle still skipped."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        oracle_trigger="any_local_finding",
    )
    await orch.sanitize("nothing sensitive at all", team_id="t1", conversation_id="c1")
    assert call_count[0] == 0


async def test_trigger_any_local_finding_bijection_holds() -> None:
    """any_local_finding: local + oracle both contribute on a no-gaz-hit leaf; bijection holds."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    email = Finding("alice@corp.lan", "EMAIL", 5, 19, 0.95)
    client, _ = _client_with_counter([("Bob Jones", "[PERSON_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        local_detectors=[_StaticFindingDetector([email])],
        oracle_trigger="any_local_finding",
    )
    text = "mail alice@corp.lan and Bob Jones now"
    result = await orch.sanitize(text, team_id="t1", conversation_id="c1")
    originals = [o for o, _ in result.pairs]
    placeholders = [p for _, p in result.pairs]
    assert len(originals) == len(set(originals)), "duplicate original"
    assert len(placeholders) == len(set(placeholders)), "placeholder collision"
    assert "alice@corp.lan" not in result.sanitized_text
    assert "Bob Jones" not in result.sanitized_text


# ── always — oracle on every leaf ────────────────────────────────────────────


async def test_trigger_always_calls_on_clean_request() -> None:
    """always: the oracle runs even on a clean no-gaz-hit leaf."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        oracle_trigger="always",
    )
    await orch.sanitize("nothing here at all", team_id="t1", conversation_id="c1")
    assert call_count[0] == 1


# ── gazetteer hit always runs the oracle regardless of trigger ───────────────


@pytest.mark.parametrize("trigger", ["gazetteer_hit", "any_local_finding", "sampled:0", "always"])
async def test_trigger_gaz_hit_always_runs_oracle(trigger: str) -> None:
    """A gazetteer hit runs the oracle under EVERY trigger (baseline preserved)."""
    gaz = Gazetteer({"AML": "REGULATED"})
    client, call_count = _client_with_counter([("AML", "[REGULATED_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        oracle_trigger=trigger,
    )
    await orch.sanitize("AML review", team_id="t1", conversation_id="c1")
    assert call_count[0] == 1


# ── sampled:<pct> — deterministic per-request sampling ───────────────────────


async def test_trigger_sampled_deterministic_same_request() -> None:
    """sampled:50: the SAME request (fresh cache each time) yields the SAME decision."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    text = "some neutral content for sampling"
    counts: list[int] = []
    for _ in range(2):
        client, call_count = _client_with_counter([])
        orch = SanitizationOrchestrator(
            client,
            InMemoryMappingStore(),
            _StaticRulesLoader(),
            gazetteer=gaz,
            oracle_trigger="sampled:50",
        )
        await orch.sanitize(text, team_id="t1", conversation_id="conv-xyz")
        counts.append(call_count[0])
    assert counts[0] == counts[1], "same request must resolve to the same sampling decision"


async def test_trigger_sampled_half_of_varied_set() -> None:
    """sampled:50: over a varied set of no-hit requests, ~half wake the oracle."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    n = 200
    selected = 0
    for i in range(n):
        client, call_count = _client_with_counter([])
        orch = SanitizationOrchestrator(
            client,
            InMemoryMappingStore(),
            _StaticRulesLoader(),
            gazetteer=gaz,
            oracle_trigger="sampled:50",
        )
        await orch.sanitize(f"neutral content {i}", team_id="t1", conversation_id=f"conv-{i}")
        selected += call_count[0]
    assert 0.3 * n <= selected <= 0.7 * n


async def test_trigger_sampled_zero_never_calls() -> None:
    """sampled:0 behaves like gazetteer_hit on a no-hit leaf: oracle never runs."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        oracle_trigger="sampled:0",
    )
    await orch.sanitize("plain neutral text", team_id="t1", conversation_id="c1")
    assert call_count[0] == 0


async def test_trigger_sampled_hundred_always_calls() -> None:
    """sampled:100 behaves like always on a no-hit leaf: oracle always runs."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        oracle_trigger="sampled:100",
    )
    await orch.sanitize("plain neutral text", team_id="t1", conversation_id="c1")
    assert call_count[0] == 1


# ── deliver-flag rescan honors the same trigger (M2 mirror) ──────────────────


async def test_trigger_always_widens_deliver_flag_rescan() -> None:
    """always: the deliver-flag rescan consults the oracle even with a gazetteer + no hit."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    name = "Ivan Petrov"  # no regex/gaz/rule hit — only the oracle recognises it
    client, call_count = _client_with_counter([(name, "[NAME_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        oracle_trigger="always",
        size_threshold_bytes=10,
        oversize_policy="deliver-flag",
        oversize_deliver_teams=frozenset({"t1"}),
    )
    big = f"memo about {name} " + "z" * 40
    with pytest.raises(OversizeContentError):
        await orch.sanitize(big, team_id="t1", conversation_id="c1")
    assert call_count[0] == 1


async def test_trigger_gazetteer_hit_deliver_flag_skips_oracle_on_nohit() -> None:
    """Default gazetteer_hit: the deliver-flag rescan skips the oracle on a no-hit leaf
    (the F3 gap that widening the trigger closes)."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    client, call_count = _client_with_counter([("Ivan Petrov", "[NAME_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        size_threshold_bytes=10,
        oversize_policy="deliver-flag",
        oversize_deliver_teams=frozenset({"t1"}),
    )
    big = "memo about Ivan Petrov " + "z" * 40
    result = await orch.sanitize(big, team_id="t1", conversation_id="c1")
    assert call_count[0] == 0
    assert result.block_reason == "oversize:delivered"


# ── normalize + sampling primitives ──────────────────────────────────────────


def test_normalize_oracle_trigger_canonical_forms() -> None:
    assert normalize_oracle_trigger(None) == "gazetteer_hit"
    assert normalize_oracle_trigger("") == "gazetteer_hit"
    assert normalize_oracle_trigger("  ALWAYS ") == "always"
    assert normalize_oracle_trigger("Any_Local_Finding") == "any_local_finding"
    assert normalize_oracle_trigger("sampled:25") == "sampled:25"
    assert normalize_oracle_trigger("SAMPLED:0") == "sampled:0"
    assert normalize_oracle_trigger("sampled:100") == "sampled:100"


@pytest.mark.parametrize(
    "bad", ["bogus", "sampled:", "sampled:abc", "sampled:-1", "sampled:101", "sample:50"]
)
def test_normalize_oracle_trigger_rejects_bad(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_oracle_trigger(bad)


def test_orchestrator_rejects_bad_trigger() -> None:
    client, _ = _client_with_counter([])
    with pytest.raises(ValueError):
        SanitizationOrchestrator(
            client,
            InMemoryMappingStore(),
            _StaticRulesLoader(),
            oracle_trigger="nope",
        )


def test_sample_selected_bounds_and_deterministic() -> None:
    assert _sample_selected("abc", 0) is False
    assert _sample_selected("abc", 100) is True
    assert _sample_selected("seed-1", 50) == _sample_selected("seed-1", 50)


def test_sample_selected_distribution_is_uniform() -> None:
    n = 1000
    hits = sum(_sample_selected(f"seed-{i}", 50) for i in range(n))
    assert 0.4 * n <= hits <= 0.6 * n


# ---------------------------------------------------------------------------
# CORP_LLM_ORACLE_ENABLED — oracle-off gate (Task 2)
# ---------------------------------------------------------------------------


async def test_oracle_disabled_gazetteer_hit_skips_oracle_local_findings_applied() -> None:
    """oracle_enabled=False: a gazetteer hit must NOT call the oracle; the
    gazetteer/local findings are still sanitized (inverted
    test_gazetteer_hit_calls_oracle)."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    client, call_count = _client_with_counter([("Project Polaris", "[PRODUCT_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        oracle_enabled=False,
    )
    result = await orch.sanitize(
        "We are working on Project Polaris this sprint.",
        team_id="t1",
        conversation_id="c1",
    )
    assert call_count[0] == 0, "oracle must NOT be called when disabled"
    assert "Project Polaris" not in result.sanitized_text
    assert any("PRODUCT" in p for _, p in result.pairs)


async def test_oracle_disabled_trigger_always_still_zero_calls() -> None:
    """oracle_enabled=False wins over CORP_LLM_ORACLE_TRIGGER=always: still 0 calls."""
    gaz = Gazetteer({"AML": "REGULATED"})
    client, call_count = _client_with_counter([("AML", "[REGULATED_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        gazetteer=gaz,
        oracle_trigger="always",
        oracle_enabled=False,
    )
    result = await orch.sanitize("AML review", team_id="t1", conversation_id="c1")
    assert call_count[0] == 0, "oracle_enabled=False must win over oracle_trigger=always"
    assert "AML" not in result.sanitized_text


def test_oracle_enabled_requires_client() -> None:
    """Construction fails fast: oracle_enabled=True (default) with no client is a
    programming error, not a config error."""
    with pytest.raises(ValueError):
        SanitizationOrchestrator(
            None,
            InMemoryMappingStore(),
            _StaticRulesLoader(),
        )


async def test_oracle_disabled_local_pass_branch_applies_replace_md_rules() -> None:
    """local_pass branch (no gazetteer), oracle disabled: replace.md rules no
    longer arrive via the oracle round-trip, so they must be applied directly —
    not silently dropped."""
    rules = Rules(rules=(Rule("Zephyr Ledger", "[CONFIDENTIAL_PROJECT]"),))
    email = Finding("bob@example.com", "EMAIL", 22, 38, 0.95)
    client, call_count = _client_with_counter([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _RuleLoader(rules),
        local_detectors=[_StaticFindingDetector([email])],
        oracle_enabled=False,
        # no gazetteer → local_pass branch
    )
    result = await orch.sanitize(
        "Migrating Zephyr Ledger, contact bob@example.com",
        team_id="t1",
        conversation_id="c1",
    )
    assert call_count[0] == 0, "oracle must NOT be called when disabled"
    assert "Zephyr Ledger" not in result.sanitized_text
    assert "[CONFIDENTIAL_PROJECT]" in result.sanitized_text
    assert "bob@example.com" not in result.sanitized_text
    originals = [o for o, _ in result.pairs]
    placeholders = [p for _, p in result.pairs]
    assert len(originals) == len(set(originals)), "duplicate original in pairs"
    assert len(placeholders) == len(set(placeholders)), "placeholder collision"
