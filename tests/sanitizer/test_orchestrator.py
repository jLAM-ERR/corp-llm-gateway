import json
from typing import Any

import httpx
import pytest

from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.detectors import RegexChecksumDetector
from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.payload import (
    OVERSIZE_CHUNK,
    OVERSIZE_DELIVER_FLAG,
    OversizeContentError,
)
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


# Size threshold / oversize policy (F1) -------------------------------------


async def test_oversize_input_fails_closed_by_default() -> None:
    """F1 repro: an oversize leaf must NOT egress verbatim — default fails closed."""
    client, captured = _client_returning_pairs([("alice", "[N1]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
        size_threshold_bytes=10,
    )
    big = "alice " + "x" * 50
    with pytest.raises(OversizeContentError) as ei:
        await orch.sanitize(big, team_id="t1", conversation_id="c1")
    assert ei.value.threshold_bytes == 10
    assert ei.value.content_bytes == len(big.encode("utf-8"))
    assert len(captured) == 0, "oversize input must not call corp-LLM"
    # The error carries sizes only — never the raw content (M1-14).
    assert "alice" not in str(ei.value)


async def test_oversize_chunk_policy_redacts_secret() -> None:
    """chunk policy: an oversize leaf is chunked + sanitized; the secret is redacted."""
    secret = "sk-" + "a" * 40
    client, _ = _client_returning_pairs([])  # oracle returns nothing; local finds it
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
        size_threshold_bytes=32,
        oversize_policy=OVERSIZE_CHUNK,
        local_detectors=[RegexChecksumDetector()],
    )
    big = "context " + secret + " more " + "y" * 80
    result = await orch.sanitize(big, team_id="t1", conversation_id="c1")
    assert result.skipped is False
    assert secret not in result.sanitized_text, "secret leaked through the chunk path"
    assert any(o == secret for o, _ in result.pairs)
    placeholders = [p for _, p in result.pairs]
    assert len(placeholders) == len(set(placeholders)), "bijection: placeholders must be distinct"


async def test_oversize_chunk_secret_on_seam_still_redacted() -> None:
    """A secret straddling a chunk seam stays fully inside an overlapping window."""
    client, _ = _client_returning_pairs([])
    window, overlap = 40, 25  # step 15; overlap >= the 8-char email
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
        size_threshold_bytes=20,
        oversize_policy=OVERSIZE_CHUNK,
        chunk_window_chars=window,
        chunk_overlap_chars=overlap,
        local_detectors=[RegexChecksumDetector()],
    )
    email = "me@ex.io"  # 8 chars
    # Email occupies [35, 43): cut by window0 [0, 40) but whole inside window1 [15, 55).
    text = "a" * 34 + " " + email + " " + "b" * 60
    result = await orch.sanitize(text, team_id="t1", conversation_id="c1")
    assert email not in result.sanitized_text, "seam-straddling secret leaked"
    assert any(o == email for o, _ in result.pairs)


async def test_default_chunk_overlap_covers_longest_entity() -> None:
    """Safety property: the default overlap must exceed the longest matchable entity."""
    from corp_llm_gateway.sanitizer.orchestrator import (
        _DEFAULT_CHUNK_OVERLAP_CHARS,
        _DEFAULT_CHUNK_WINDOW_CHARS,
    )

    assert _DEFAULT_CHUNK_OVERLAP_CHARS >= 8192  # PEM private-key body cap
    assert _DEFAULT_CHUNK_WINDOW_CHARS > _DEFAULT_CHUNK_OVERLAP_CHARS


async def test_oversize_deliver_flag_requires_team_optin() -> None:
    """deliver-flag without a team opt-in falls back to fail-closed."""
    client, _ = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
        size_threshold_bytes=10,
        oversize_policy=OVERSIZE_DELIVER_FLAG,
        oversize_deliver_teams=frozenset({"other-team"}),
    )
    big = "clean text " + "z" * 50
    with pytest.raises(OversizeContentError):
        await orch.sanitize(big, team_id="t1", conversation_id="c1")


async def test_oversize_deliver_flag_optin_clean_delivers() -> None:
    """deliver-flag + opt-in + a clean full rescan → delivers the original, flagged."""
    client, _ = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
        size_threshold_bytes=10,
        oversize_policy=OVERSIZE_DELIVER_FLAG,
        oversize_deliver_teams=frozenset({"t1"}),
    )
    big = "the quick brown fox jumps over the lazy dog and then again"
    result = await orch.sanitize(big, team_id="t1", conversation_id="c1")
    assert result.skipped is True
    assert result.sanitized_text == big
    assert result.pairs == ()


async def test_oversize_deliver_flag_optin_dirty_blocks() -> None:
    """deliver-flag + opt-in but the content has PII → full rescan trips → fail-closed.

    Uses an EMAIL: caught by the full regex+checksum rescan but NOT one of the DLP
    guard's five secret regexes — proving the rescan is the full cascade.
    """
    client, _ = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
        size_threshold_bytes=10,
        oversize_policy=OVERSIZE_DELIVER_FLAG,
        oversize_deliver_teams=frozenset({"t1"}),
    )
    big = "please contact leak@corp.example about this " + "q" * 40
    with pytest.raises(OversizeContentError):
        await orch.sanitize(big, team_id="t1", conversation_id="c1")


async def test_oversize_chunk_unbounded_secret_on_seam_fully_redacted() -> None:
    """H1: an UNBOUNDED-pattern secret straddling a chunk seam is fully redacted.

    JWT (`eyJ…`) and `sk-{32,}` have no length cap, so no fixed overlap can
    contain them. With a tiny window/overlap that CANNOT hold either secret, the
    full-text regex pass still matches them whole — nothing raw egresses.
    """
    client, _ = _client_returning_pairs([])  # oracle finds nothing; regex is full-text
    window, overlap = 64, 16  # neither secret fits in a 64-char window
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
        size_threshold_bytes=32,
        oversize_policy=OVERSIZE_CHUNK,
        chunk_window_chars=window,
        chunk_overlap_chars=overlap,
        local_detectors=[RegexChecksumDetector()],
    )
    jwt = "eyJ" + "A" * 300 + "." + "B" * 300 + "." + "C" * 300  # ~900 chars, unbounded
    key = "sk-" + "d" * 60  # sk-{32,}, unbounded
    text = "x" * 50 + " " + jwt + " mid " + key + " " + "y" * 50
    result = await orch.sanitize(text, team_id="t1", conversation_id="c1")
    assert jwt not in result.sanitized_text, "JWT leaked across a seam"
    assert key not in result.sanitized_text, "long key leaked across a seam"
    assert "C" * 50 not in result.sanitized_text, "raw JWT signature fragment egressed"
    assert any(o == jwt for o, _ in result.pairs), "whole JWT not redacted as one pair"
    assert any(o == key for o, _ in result.pairs), "whole key not redacted as one pair"
    placeholders = [p for _, p in result.pairs]
    assert len(placeholders) == len(set(placeholders)), "bijection: placeholders must be distinct"


async def test_oversize_deliver_flag_oracle_only_finding_blocks() -> None:
    """M2: an oracle-only finding (no regex/local/gazetteer/rule hit) fails closed.

    Proves the deliver-flag rescan calls the oracle exactly as the normal path
    would; without it a name only the oracle recognises would egress verbatim.
    """
    name = "Ivan Petrov"  # plain name: no regex/checksum floor match
    client, captured = _client_returning_pairs([(name, "[NAME_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
        size_threshold_bytes=10,
        oversize_policy=OVERSIZE_DELIVER_FLAG,
        oversize_deliver_teams=frozenset({"t1"}),
    )
    big = f"internal memo regarding {name} and the plan " + "z" * 30
    with pytest.raises(OversizeContentError):
        await orch.sanitize(big, team_id="t1", conversation_id="c1")
    assert len(captured) == 1, "deliver-flag rescan must consult the oracle (M2)"


async def test_oversize_deliver_flag_marks_result_block_reason() -> None:
    """M1: a delivered oversize leaf carries the oversize:delivered marker."""
    client, _ = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
        size_threshold_bytes=10,
        oversize_policy=OVERSIZE_DELIVER_FLAG,
        oversize_deliver_teams=frozenset({"t1"}),
    )
    big = "the quick brown fox jumps over the lazy dog and then again"
    result = await orch.sanitize(big, team_id="t1", conversation_id="c1")
    assert result.skipped is True
    assert result.block_reason == "oversize:delivered"


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
