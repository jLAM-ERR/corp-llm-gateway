import json
from typing import Any

import httpx

from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.rules import (
    Gazetteer,
    Rule,
    Rules,
    RulesLoader,
)
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import InMemoryMappingStore


class _StaticRulesLoader(RulesLoader):
    def __init__(self, rules: Rules) -> None:
        self._rules = rules

    async def load(self, team_id: str) -> Rules:
        return self._rules


def _client_returning_pairs(pairs: list[tuple[str, str]]) -> tuple[CorpLlmClient, list[dict]]:
    """Return a client that always tool-calls back the given pairs, plus the
    captured request bodies for assertions."""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
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
    return client, captured


# Round-trip ----------------------------------------------------------------


async def test_orchestrator_substitutes_placeholders() -> None:
    client, _ = _client_returning_pairs([("alice", "[NAME_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
    )
    result = await orch.sanitize("hello alice", team_id="t1", conversation_id="c1")
    assert result.sanitized_text == "hello [NAME_001]"
    assert result.pairs == (("alice", "[NAME_001]"),)
    assert result.skipped is False


# Cache A — content hash dedup ----------------------------------------------


async def test_orchestrator_cache_a_hit_skips_corp_llm() -> None:
    client, captured = _client_returning_pairs([("alice", "[N1]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
    )
    r1 = await orch.sanitize("hello alice", team_id="t1", conversation_id="c1")
    r2 = await orch.sanitize("hello alice", team_id="t1", conversation_id="c1")
    assert r1.cache_a_hit is False
    assert r2.cache_a_hit is True
    assert r2.sanitized_text == r1.sanitized_text
    assert len(captured) == 1, "cache A should prevent the second corp-LLM call"


async def test_cache_a_keyed_by_team() -> None:
    client, captured = _client_returning_pairs([("alice", "[N1]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
    )
    await orch.sanitize("hello alice", team_id="t1", conversation_id="c1")
    await orch.sanitize("hello alice", team_id="t2", conversation_id="c1")
    assert len(captured) == 2, "different teams must not share cache A"


async def test_cache_a_invalidated_when_rules_change() -> None:
    client, captured = _client_returning_pairs([("alice", "[N1]")])
    store = InMemoryMappingStore()
    orch_v1 = SanitizationOrchestrator(
        client, store, _StaticRulesLoader(Rules(rules=(Rule("alice", "[N1]"),)))
    )
    orch_v2 = SanitizationOrchestrator(
        client,
        store,
        _StaticRulesLoader(Rules(rules=(Rule("alice", "[N9]"),))),
    )
    await orch_v1.sanitize("hello alice", team_id="t1", conversation_id="c1")
    await orch_v2.sanitize("hello alice", team_id="t1", conversation_id="c1")
    assert len(captured) == 2, "rule change must invalidate cache A"


# Cache B — per-conversation mapping ----------------------------------------


async def test_cache_b_records_conversation_mappings() -> None:
    client, _ = _client_returning_pairs([("alice", "[N1]")])
    store = InMemoryMappingStore()
    orch = SanitizationOrchestrator(client, store, _StaticRulesLoader(Rules(rules=())))
    await orch.sanitize("hello alice", team_id="t1", conversation_id="c1")
    assert await store.get_placeholder("c1", "alice") == "[N1]"
    assert await store.get_original("c1", "[N1]") == "alice"


# Idempotency ---------------------------------------------------------------


async def test_idempotency_same_input_same_output() -> None:
    client, _ = _client_returning_pairs([("alice", "[N1]")])
    orch = SanitizationOrchestrator(
        client, InMemoryMappingStore(), _StaticRulesLoader(Rules(rules=()))
    )
    r1 = await orch.sanitize("hello alice", team_id="t1", conversation_id="c1")
    r2 = await orch.sanitize("hello alice", team_id="t1", conversation_id="c2")
    assert r1.sanitized_text == r2.sanitized_text
    assert r1.pairs == r2.pairs


# Size threshold ------------------------------------------------------------


async def test_oversize_input_skips_sanitization() -> None:
    client, captured = _client_returning_pairs([("alice", "[N1]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
        size_threshold_bytes=10,
    )
    big = "x" * 50
    result = await orch.sanitize(big, team_id="t1", conversation_id="c1")
    assert result.skipped is True
    assert result.sanitized_text == big
    assert len(captured) == 0, "oversize input must not call corp-LLM"


# Length-descending substitution invariant ---------------------------------


async def test_long_pattern_replaces_before_short_one() -> None:
    """Without descending-length sort, `alice` would shadow `alice cooper`."""
    client, _ = _client_returning_pairs([("alice", "[NAME]"), ("alice cooper", "[NAME_LONG]")])
    orch = SanitizationOrchestrator(
        client, InMemoryMappingStore(), _StaticRulesLoader(Rules(rules=()))
    )
    result = await orch.sanitize("alice cooper sang", team_id="t1", conversation_id="c1")
    assert "[NAME_LONG]" in result.sanitized_text


# Rule prompt injection -----------------------------------------------------


async def test_team_rules_appear_in_system_prompt() -> None:
    client, captured = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=(Rule("Project Polaris", "[CONFIDENTIAL]"),))),
    )
    await orch.sanitize("hi", team_id="t1", conversation_id="c1")
    body: dict[str, Any] = captured[0]
    system = body["messages"][0]
    assert system["role"] == "system"
    assert "Project Polaris" in system["content"]
    assert "[CONFIDENTIAL]" in system["content"]


# Tool-call shape -----------------------------------------------------------


async def test_request_includes_tools_and_forced_tool_choice() -> None:
    client, captured = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client, InMemoryMappingStore(), _StaticRulesLoader(Rules(rules=()))
    )
    await orch.sanitize("hi", team_id="t1", conversation_id="c1")
    body = captured[0]
    assert body["tools"][0]["function"]["name"] == SANITIZE_TOOL_NAME
    assert body["tool_choice"]["function"]["name"] == SANITIZE_TOOL_NAME


# ---------------------------------------------------------------------------
# Replace.md local path — rules applied before oracle (gazetteer branch)
# ---------------------------------------------------------------------------


class _StaticFindingDetector(PIIDetector):
    def __init__(self, findings: list[Finding]) -> None:
        self._findings = findings

    async def detect(self, text: str) -> list[Finding]:
        return list(self._findings)


async def test_rules_applied_in_gazetteer_nohit_oracle_not_called() -> None:
    """Gazetteer no-hit + rule present → rule applies; oracle NOT called."""
    gaz = Gazetteer({})  # empty — never hits
    rules = Rules(rules=(Rule("Zephyr Ledger", "[CONFIDENTIAL_PROJECT]"),))
    client, captured = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        gazetteer=gaz,
    )
    result = await orch.sanitize(
        "Migrating Zephyr Ledger to new stack", team_id="t1", conversation_id="c1"
    )
    assert len(captured) == 0, "oracle must NOT be called when gazetteer has no hit"
    assert ("Zephyr Ledger", "[CONFIDENTIAL_PROJECT]") in result.pairs
    assert "[CONFIDENTIAL_PROJECT]" in result.sanitized_text
    assert "Zephyr Ledger" not in result.sanitized_text


async def test_rule_wins_over_oracle_on_origin_collision_in_gazetteer_hit() -> None:
    """When oracle also names a rule's origin, the rule's replacement wins."""
    gaz = Gazetteer({"trigger": "PRODUCT"})
    rules = Rules(rules=(Rule("Zephyr Ledger", "[CONFIDENTIAL_PROJECT]"),))
    # Oracle returns a pair for Zephyr Ledger with a different placeholder.
    oracle_pairs = [("trigger", "[PRODUCT_001]"), ("Zephyr Ledger", "[PERSON_001]")]
    client, captured = _client_returning_pairs(oracle_pairs)
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        gazetteer=gaz,
    )
    result = await orch.sanitize(
        "trigger event for Zephyr Ledger migration", team_id="t1", conversation_id="c1"
    )
    assert len(captured) == 1, "oracle must be called on a gazetteer hit"
    originals = [o for o, _ in result.pairs]
    # Rule wins: Zephyr Ledger → [CONFIDENTIAL_PROJECT], not [PERSON_001]
    zl_placeholders = [p for o, p in result.pairs if o == "Zephyr Ledger"]
    assert zl_placeholders == ["[CONFIDENTIAL_PROJECT]"]
    # Oracle's trigger pair still present
    assert "trigger" in originals
    # Bijection
    assert len(originals) == len(set(originals)), "duplicate original in pairs"
    placeholders = [p for _, p in result.pairs]
    assert len(placeholders) == len(set(placeholders)), "placeholder collision"


async def test_rule_wins_over_local_finding_in_gazetteer_nohit() -> None:
    """Rule origin also flagged by local detector → only the rule pair appears."""
    gaz = Gazetteer({})
    rules = Rules(rules=(Rule("Zephyr Ledger", "[CONFIDENTIAL_PROJECT]"),))
    # Local detector would also flag the same text as a PERSON finding.
    local_finding = Finding("Zephyr Ledger", "PERSON", 0, 12, 0.9)
    client, captured = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        gazetteer=gaz,
        local_detectors=[_StaticFindingDetector([local_finding])],
    )
    result = await orch.sanitize(
        "Zephyr Ledger quarterly report", team_id="t1", conversation_id="c1"
    )
    assert len(captured) == 0
    # Exactly one pair for the origin; the rule's placeholder wins.
    zl_pairs = [(o, p) for o, p in result.pairs if o == "Zephyr Ledger"]
    assert zl_pairs == [("Zephyr Ledger", "[CONFIDENTIAL_PROJECT]")]
    # No stray PERSON placeholder for the same origin.
    assert not any("PERSON" in p for _, p in result.pairs)


async def test_rules_bijection_holds_in_gazetteer_nohit() -> None:
    """Multiple rules in no-hit branch: unique originals + unique placeholders."""
    gaz = Gazetteer({})
    rules = Rules(
        rules=(
            Rule("Zephyr Ledger", "[CONFIDENTIAL_PROJECT]"),
            Rule("db-legacy-7", "[INTERNAL_HOST]"),
        )
    )
    client, captured = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        gazetteer=gaz,
    )
    result = await orch.sanitize(
        "Zephyr Ledger connects to db-legacy-7 daily", team_id="t1", conversation_id="c1"
    )
    assert len(captured) == 0
    originals = [o for o, _ in result.pairs]
    placeholders = [p for _, p in result.pairs]
    assert len(originals) == len(set(originals)), "duplicate original in pairs"
    assert len(placeholders) == len(set(placeholders)), "placeholder collision"
    assert "[CONFIDENTIAL_PROJECT]" in result.sanitized_text
    assert "[INTERNAL_HOST]" in result.sanitized_text
