import json
from typing import Any

import httpx

from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.rules import (
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
