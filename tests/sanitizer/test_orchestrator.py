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
from corp_llm_gateway.storage import InMemoryMappingStore, PlaceholderMapping


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


async def test_oversize_deliver_flag_oracle_disabled_local_finding_blocks_no_oracle_call() -> None:
    """oracle_enabled=False: the deliver-flag rescan skips the oracle entirely but
    still fails closed on a LOCAL finding (oracle-off mirror of
    test_oversize_deliver_flag_oracle_only_finding_blocks — local findings still
    apply, they just aren't backstopped by the oracle)."""
    email = "leak@corp.example"
    detector = _StaticFindingDetector([Finding(email, "EMAIL", 0, len(email), 0.95)])
    client, captured = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(Rules(rules=())),
        size_threshold_bytes=10,
        oversize_policy=OVERSIZE_DELIVER_FLAG,
        oversize_deliver_teams=frozenset({"t1"}),
        local_detectors=[detector],
        oracle_enabled=False,
    )
    big = f"{email} plus some extra padding " + "z" * 30
    with pytest.raises(OversizeContentError):
        await orch.sanitize(big, team_id="t1", conversation_id="c1")
    assert len(captured) == 0, "oracle must NOT be called when disabled"


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


# ---------------------------------------------------------------------------
# Defect #7 — rule matching restored to substring semantics (decision 2)
# ---------------------------------------------------------------------------


def test_rule_matches_as_infix_substring_release_semantics() -> None:
    """A rule word must redact inside a larger identifier, per release/1.0.x's
    plain `r.pattern in text` substring test — the PR's identifier-prefix
    anchor (`(?<![^\\W_])`) blocked this (a rule `Ledger` no longer matched
    `MyLedger`); decision 2 drops the anchor."""
    from corp_llm_gateway.sanitizer.orchestrator import _rule_matches

    rules = Rules(rules=(Rule("Ledger", "[PROJECT_LEDGER]"),))
    matches = _rule_matches(rules, "MyLedger status update")
    assert [m.original for m in matches] == ["Ledger"]


def test_rule_matches_phrase_case_insensitively() -> None:
    """Retained widening: case-insensitive matching redacts strictly more than
    release's case-sensitive substring test."""
    from corp_llm_gateway.sanitizer.orchestrator import _rule_matches

    rules = Rules(rules=(Rule("Project Polaris", "[CONFIDENTIAL]"),))
    matches = _rule_matches(rules, "the project polaris rollout")
    assert [m.original for m in matches] == ["project polaris"]


async def test_rules_match_case_insensitively_and_apply_replacement_verbatim() -> None:
    rules = Rules(
        rules=(
            Rule("kdir", "companynameabc"),
            Rule("betadirect", "companynameabd"),
            Rule("beta direct", "company name abe"),
            Rule("zephyr ledger", "confidential project acn"),
        )
    )
    client, captured = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        gazetteer=Gazetteer({}),
    )

    result = await orch.sanitize(
        "mkdir -p KdirService1; BetadirectClient; beta direct; Zephyr Ledger zephyr leDger",
        team_id="t1",
        conversation_id="c1",
    )

    assert len(captured) == 0
    # No case-preservation (decision 4): the configured replacement is used verbatim.
    # Decision 2 (defect #7) drops the PR's identifier-prefix anchor, so "kdir"
    # matches as a plain case-insensitive substring — including inside "mkdir",
    # same as release/1.0.x's `r.pattern in text` would (the PR's own anchored
    # regex previously kept "mkdir" intact; that narrowing is no longer applied).
    assert result.sanitized_text == (
        "mcompanynameabc -p companynameabcService1; companynameabdClient; "
        "company name abe; confidential project acn confidential project acn"
    )


# ---------------------------------------------------------------------------
# Defect #5 — longest span wins across ONE pool of rules + findings (decision 1)
# ---------------------------------------------------------------------------


async def test_finding_wins_over_partially_overlapping_rule_gazetteer_branch() -> None:
    """Mechanism (a): a finding partially overlapping a rule must not be
    dropped outright — the LONGER span wins. Gazetteer no-hit branch, where the
    now-deleted `_filter_findings_overlapping_rules` used to run."""
    gaz = Gazetteer({})  # empty — never hits, oracle stays skipped
    rules = Rules(rules=(Rule("Alice", "[EMPLOYEE_001]"),))
    finding = Finding("Alice Smith", "PERSON", 8, 19, 0.9)
    client, captured = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        gazetteer=gaz,
        local_detectors=[_StaticFindingDetector([finding])],
    )

    result = await orch.sanitize("Contact Alice Smith today", team_id="t1", conversation_id="c1")

    assert len(captured) == 0, "no gazetteer hit → oracle must not be called"
    assert result.sanitized_text == "Contact [PERSON_001] today"
    assert "Smith" not in result.sanitized_text


async def test_rule_no_longer_steals_span_from_longer_finding_oracle_disabled() -> None:
    """Mechanism (b), reviewer's exact repro: `_plan_replacements` reserved every
    rule span first and unconditionally, so a short rule matching INSIDE a
    longer finding stole the span and the finding's pair vanished from
    `used_pairs` — losing its Cache-B mapping entirely, not just its
    redaction. Runs on the oracle-DISABLED arm, where no filter ever ran, so
    this can't pass merely because `_filter_findings_overlapping_rules` is
    gone — the span-ordering fix is what's under test. Real
    `RegexChecksumDetector`, not a stub."""
    rules = Rules(rules=(Rule("acme", "[COMPANY]"),))
    orch = SanitizationOrchestrator(
        None,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        local_detectors=[RegexChecksumDetector()],
        oracle_enabled=False,
    )

    result = await orch.sanitize(
        "escalate to ops@acme-corp.com please", team_id="t1", conversation_id="c1"
    )

    assert result.sanitized_text == "escalate to [EMAIL_001] please"
    assert ("ops@acme-corp.com", "[EMAIL_001]") in result.pairs
    assert "[COMPANY]" not in result.sanitized_text


async def test_oracle_on_and_off_produce_identical_sanitized_output() -> None:
    """The oracle toggle (F3) is a latency/availability knob; it must not change
    local-detection outcomes. Pre-fix, the oracle-ON arm dropped an overlapping
    finding BEFORE `_merge_local` (mechanism a) while oracle-OFF dropped it
    AFTER (mechanism b) — same finding lost, but the discarded finding
    consumed a placeholder number in one arm and not the other, so the
    SURVIVING finding's label numbering diverged between the two arms."""
    rules = Rules(rules=(Rule("Alice", "[EMPLOYEE_001]"),))
    text = "Alice Smith met Bob"
    findings = [
        Finding("Alice Smith", "PERSON", 0, len("Alice Smith"), 0.9),
        Finding("Bob", "PERSON", text.index("Bob"), text.index("Bob") + 3, 0.9),
    ]

    orch_off = SanitizationOrchestrator(
        None,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        local_detectors=[_StaticFindingDetector(findings)],
        oracle_enabled=False,
    )
    off = await orch_off.sanitize(text, team_id="t1", conversation_id="c1")

    client, captured = _client_returning_pairs([])
    orch_on = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        local_detectors=[_StaticFindingDetector(findings)],
    )
    on = await orch_on.sanitize(text, team_id="t1", conversation_id="c2")

    assert len(captured) == 1, "oracle must be called when enabled (no gazetteer configured)"
    assert on.sanitized_text == off.sanitized_text
    assert on.pairs == off.pairs


async def test_cache_a_hit_reproduces_span_selection_bit_for_bit() -> None:
    """The Cache-A hit path re-runs `_plan_replacements` with the already
    used-pairs-filtered `cached.pairs`; span selection (and thus the
    deterministic tiebreak) must reproduce the miss path exactly."""
    rules = Rules(rules=(Rule("Alice", "[EMPLOYEE_001]"),))
    text = "Alice Smith met Bob"
    findings = [
        Finding("Alice Smith", "PERSON", 0, len("Alice Smith"), 0.9),
        Finding("Bob", "PERSON", text.index("Bob"), text.index("Bob") + 3, 0.9),
    ]
    orch = SanitizationOrchestrator(
        None,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        local_detectors=[_StaticFindingDetector(findings)],
        oracle_enabled=False,
    )

    miss = await orch.sanitize(text, team_id="t1", conversation_id="c1")
    hit = await orch.sanitize(text, team_id="t1", conversation_id="c2")

    assert hit.cache_a_hit is True
    assert miss.sanitized_text == "[PERSON_001] met [PERSON_002]"
    assert hit.sanitized_text == miss.sanitized_text
    assert hit.pairs == miss.pairs
    assert hit.applied_spans == miss.applied_spans


async def test_longer_local_finding_wins_over_overlapping_rule_span() -> None:
    """Renamed from `test_rule_spans_win_over_overlapping_local_findings`
    (decision 1 inverts the old "rules always win" policy): the ORG finding's
    21-char span fully covers the 10-char "Betadirect" rule match, so the
    finding now wins that span; the non-overlapping "Zephyr Ledger" rule is
    unaffected."""
    text = "Betadirect работает в Zephyr Ledger"
    rules = Rules(
        rules=(
            Rule("betadirect", "companynameabd"),
            Rule("zephyr ledger", "confidential project acn"),
        )
    )
    local_findings = [
        Finding("Betadirect работает в", "ORG", 0, len("Betadirect работает в"), 0.99),
        Finding("Zephyr", "LOCATION", text.index("Zephyr"), text.index("Zephyr") + 6, 0.99),
    ]
    client, captured = _client_returning_pairs([])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        gazetteer=Gazetteer({}),
        local_detectors=[_StaticFindingDetector(local_findings)],
    )

    result = await orch.sanitize(text, team_id="t1", conversation_id="c1")

    assert len(captured) == 0
    assert result.sanitized_text == "[ORG_001] confidential project acn"
    assert result.pairs == (
        ("Zephyr Ledger", "confidential project acn"),
        ("Betadirect работает в", "[ORG_001]"),
    )


async def test_oracle_mapping_preserves_non_overlapping_occurrence_and_cache_hit() -> None:
    """A rule overlap must not discard the same oracle original elsewhere."""
    text = "Alice Smith met Alice"
    rules = Rules(rules=(Rule("Alice Smith", "[CONTRACTOR_001]"),))
    client, captured = _client_returning_pairs([("Alice", "[PERSON_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
    )

    first = await orch.sanitize(text, team_id="t1", conversation_id="c1")
    cached = await orch.sanitize(text, team_id="t1", conversation_id="c2")

    assert first.sanitized_text == "[CONTRACTOR_001] met [PERSON_001]"
    assert first.pairs == (
        ("Alice Smith", "[CONTRACTOR_001]"),
        ("Alice", "[PERSON_001]"),
    )
    assert cached.sanitized_text == first.sanitized_text
    assert cached.pairs == first.pairs
    assert cached.applied_spans == first.applied_spans
    assert cached.cache_a_hit is True
    assert len(captured) == 1


async def test_fully_rule_covered_oracle_pair_is_not_stored() -> None:
    rules = Rules(rules=(Rule("Alice Smith", "[CONTRACTOR_001]"),))
    client, _ = _client_returning_pairs([("Alice", "[PERSON_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
    )

    result = await orch.sanitize("Alice Smith", team_id="t1", conversation_id="c1")

    assert result.sanitized_text == "[CONTRACTOR_001]"
    assert result.pairs == (("Alice Smith", "[CONTRACTOR_001]"),)


async def test_longer_oracle_original_wins_over_shorter_rule_span() -> None:
    """Renamed from `test_shorter_rule_span_wins_over_longer_oracle_original`
    (decision 1): a longer oracle finding now beats a shorter, overlapping
    rule — previously the rule won unconditionally regardless of span length
    (defect #5, mechanism b)."""
    rules = Rules(rules=(Rule("Alice", "[EMPLOYEE_001]"),))
    client, _ = _client_returning_pairs([("Alice Smith", "[PERSON_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
    )

    result = await orch.sanitize("Alice Smith", team_id="t1", conversation_id="c1")

    assert result.sanitized_text == "[PERSON_001]"
    assert result.pairs == (("Alice Smith", "[PERSON_001]"),)


async def test_rule_replacement_is_not_rescanned_by_oracle_pair() -> None:
    rules = Rules(rules=(Rule("Alice Smith", "AliceAlias"),))
    client, _ = _client_returning_pairs([("Alice", "[PERSON_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
    )

    result = await orch.sanitize(
        "Alice Smith met Alice and Alice", team_id="t1", conversation_id="c1"
    )

    assert result.sanitized_text == "AliceAlias met [PERSON_001] and [PERSON_001]"


async def test_oracle_rule_overlap_is_span_aware_in_chunked_path() -> None:
    text = "Alice Smith met Alice"
    rules = Rules(rules=(Rule("Alice Smith", "[CONTRACTOR_001]"),))
    client, _ = _client_returning_pairs([("Alice", "[PERSON_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        size_threshold_bytes=1,
        oversize_policy=OVERSIZE_CHUNK,
        chunk_window_chars=16,
        chunk_overlap_chars=8,
    )

    result = await orch.sanitize(text, team_id="t1", conversation_id="c1")

    assert result.sanitized_text == "[CONTRACTOR_001] met [PERSON_001]"
    assert result.pairs == (
        ("Alice Smith", "[CONTRACTOR_001]"),
        ("Alice", "[PERSON_001]"),
    )


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


# ---------------------------------------------------------------------------
# D3 (SECURITY-CRITICAL): Cache-A profile fingerprint — cross-profile bleed
# ---------------------------------------------------------------------------

_D3_TEXT = "Deploy Sistema to prod"
_D3_SECRET = "Sistema"


def _permissive_orch(store: InMemoryMappingStore) -> SanitizationOrchestrator:
    """Orchestrator whose profile redacts NOTHING (oracle returns no pairs).

    Carries the SAME detector class as `_strict_orch`, configured to find
    nothing. The Cache-A policy fingerprint keys detector identity by class, so
    these two orchestrators are policy-identical to it — which is exactly the
    residue the D3 profile fingerprint has to cover.
    """
    client, _ = _client_returning_pairs([])
    return SanitizationOrchestrator(
        client,
        store,
        _StaticRulesLoader(Rules(rules=())),
        local_detectors=[_StaticFindingDetector([])],
    )


def _strict_orch(store: InMemoryMappingStore) -> tuple[SanitizationOrchestrator, list[dict]]:
    """Orchestrator whose profile MUST redact _D3_SECRET (local detector finds it)."""
    client, captured = _client_returning_pairs([])
    idx = _D3_TEXT.index(_D3_SECRET)
    detector = _StaticFindingDetector(
        [Finding(_D3_SECRET, "PRODUCT", idx, idx + len(_D3_SECRET), 0.95)]
    )
    orch = SanitizationOrchestrator(
        client,
        store,
        _StaticRulesLoader(Rules(rules=())),
        local_detectors=[detector],
    )
    return orch, captured


def _profile_fp(profile_ids: tuple[str, ...]) -> str:
    from corp_llm_gateway.profiles import (
        PolicyKnobs,
        ProfileBundle,
        bundle_fingerprint,
    )
    from corp_llm_gateway.sanitizer.allowlist import Allowlist

    return bundle_fingerprint(
        ProfileBundle(
            detectors=(),
            gazetteer=None,
            rules=Rules(rules=()),
            allowlist=Allowlist(()),
            policy=PolicyKnobs(),
            profile_ids=profile_ids,
        )
    )


async def test_cross_profile_cache_bleed_without_fingerprint() -> None:
    """REPRO: same team+rules+text, two profiles, ONE shared store, NO fingerprint.

    A permissive profile seeds Cache A with an un-redacted result; a strict
    profile that MUST redact _D3_SECRET then hits that entry and egresses the
    raw term — a cross-jurisdiction leak (this is the bug D3 closes).
    """
    store = InMemoryMappingStore()
    permissive = _permissive_orch(store)
    strict, _ = _strict_orch(store)

    r_perm = await permissive.sanitize(_D3_TEXT, team_id="t1", conversation_id="c-perm")
    assert _D3_SECRET in r_perm.sanitized_text, "permissive profile leaves the term as-is"

    r_strict = await strict.sanitize(_D3_TEXT, team_id="t1", conversation_id="c-strict")
    assert r_strict.cache_a_hit is True, "strict request wrongly reuses the permissive entry"
    assert _D3_SECRET in r_strict.sanitized_text, "LEAK: term that must be redacted egressed"


async def test_profile_fingerprint_prevents_cross_profile_reuse() -> None:
    """FIX: distinct profile fingerprints → distinct Cache-A keys → no bleed."""
    store = InMemoryMappingStore()
    permissive = _permissive_orch(store)
    strict, _ = _strict_orch(store)

    fp_permissive = _profile_fp(("us-base",))
    fp_strict = _profile_fp(("ru-152fz",))
    assert fp_permissive != fp_strict

    r_perm = await permissive.sanitize(
        _D3_TEXT, team_id="t1", conversation_id="c-perm", profile_fingerprint=fp_permissive
    )
    assert _D3_SECRET in r_perm.sanitized_text

    r_strict = await strict.sanitize(
        _D3_TEXT, team_id="t1", conversation_id="c-strict", profile_fingerprint=fp_strict
    )
    assert r_strict.cache_a_hit is False, "different fingerprint must miss the permissive entry"
    assert _D3_SECRET not in r_strict.sanitized_text, "strict profile redacts the term"
    assert any(o == _D3_SECRET for o, _ in r_strict.pairs)


async def test_same_profile_fingerprint_preserves_cache_hit() -> None:
    """Dedup still works: same fingerprint + same text → Cache-A hit, oracle called once."""
    store = InMemoryMappingStore()
    strict, captured = _strict_orch(store)
    fp = _profile_fp(("ru-152fz",))

    r1 = await strict.sanitize(_D3_TEXT, team_id="t1", conversation_id="c1", profile_fingerprint=fp)
    r2 = await strict.sanitize(_D3_TEXT, team_id="t1", conversation_id="c2", profile_fingerprint=fp)
    assert r1.cache_a_hit is False
    assert r2.cache_a_hit is True, "same profile + text must dedup"
    assert r2.sanitized_text == r1.sanitized_text
    assert _D3_SECRET not in r2.sanitized_text
    assert len(captured) == 1, "cache A must save the second corp-LLM call"


async def test_none_fingerprint_omits_profile_discriminator() -> None:
    """An omitted fingerprint and explicit None share the no-profile key."""
    from corp_llm_gateway.sanitizer.orchestrator import _content_hash

    rules = Rules(rules=(Rule("alice", "[N1]"),))
    assert _content_hash("t1", rules, "x") == _content_hash("t1", rules, "x", None)


def test_content_hash_includes_cache_algorithm_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corp_llm_gateway.sanitizer import orchestrator

    rules = Rules(rules=(Rule("alice", "[N1]"),))
    current = orchestrator._content_hash("t1", rules, "x")
    monkeypatch.setattr(orchestrator, "_CACHE_A_ALGORITHM_VERSION", b"previous-version")

    assert orchestrator._content_hash("t1", rules, "x") != current


async def test_content_hash_folds_fingerprint_into_key() -> None:
    """A profile fingerprint changes the Cache-A key; distinct fps stay distinct."""
    from corp_llm_gateway.sanitizer.orchestrator import _content_hash

    rules = Rules(rules=())
    base = _content_hash("t1", rules, "x")
    fp_a = _content_hash("t1", rules, "x", "fpA")
    fp_b = _content_hash("t1", rules, "x", "fpB")
    assert fp_a != base, "a fingerprint must not collide with the no-profile key"
    assert fp_a != fp_b, "different fingerprints must not collide"
    assert fp_a == _content_hash("t1", rules, "x", "fpA"), "same inputs must be stable"


async def test_content_hash_folds_oracle_mode_into_key() -> None:
    """P1 fix: the oracle enabled/disabled bit changes the Cache-A key."""
    from corp_llm_gateway.sanitizer.orchestrator import _content_hash

    rules = Rules(rules=())
    legacy = _content_hash("t1", rules, "x")
    on = _content_hash("t1", rules, "x", None, True)
    off = _content_hash("t1", rules, "x", None, False)
    assert on != off, "oracle on vs off must not collide"
    assert on != legacy, "an explicit oracle bit must not collide with the untagged legacy key"
    assert off != legacy, "an explicit oracle bit must not collide with the untagged legacy key"
    assert on == _content_hash("t1", rules, "x", None, True), "same inputs must be stable"


def test_content_hash_discriminates_on_both_oracle_mode_and_algorithm_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both Task 2 discriminators are orthogonal and both live: oracle_enabled keys
    the detection mode, _CACHE_A_ALGORITHM_VERSION invalidates Cache-A across the
    substitution-semantics change. Neither discriminator subsumes the other."""
    from corp_llm_gateway.sanitizer import orchestrator

    rules = Rules(rules=())
    on_v1 = orchestrator._content_hash("t1", rules, "x", None, True)
    off_v1 = orchestrator._content_hash("t1", rules, "x", None, False)
    assert on_v1 != off_v1, "oracle mode must discriminate at a fixed algorithm version"

    monkeypatch.setattr(orchestrator, "_CACHE_A_ALGORITHM_VERSION", b"previous-version")
    on_v2 = orchestrator._content_hash("t1", rules, "x", None, True)
    off_v2 = orchestrator._content_hash("t1", rules, "x", None, False)
    assert on_v2 != off_v2, "oracle mode must discriminate at a different algorithm version too"
    assert on_v1 != on_v2, "algorithm version must discriminate at a fixed oracle mode"
    assert off_v1 != off_v2, "algorithm version must discriminate at a fixed oracle mode"


async def test_none_fingerprint_preserves_dedup_behavior() -> None:
    """Default (None) fingerprint keeps the pre-D3 shared-dedup behavior intact."""
    client, captured = _client_returning_pairs([("alice", "[N1]")])
    orch = SanitizationOrchestrator(
        client, InMemoryMappingStore(), _StaticRulesLoader(Rules(rules=()))
    )
    r1 = await orch.sanitize("hello alice", team_id="t1", conversation_id="c1")
    r2 = await orch.sanitize("hello alice", team_id="t1", conversation_id="c2")
    assert r1.cache_a_hit is False
    assert r2.cache_a_hit is True
    assert len(captured) == 1


# Cache A — a superseded algorithm version must not be replayed -------------

# The value shipped before the detector-coverage boundary (BANK_CARD, CODE-segment
# NER, corp NER). Pinned as a literal on purpose: it is the key an OLDER build
# wrote under, so it must not track the current constant.
_SUPERSEDED_CACHE_A_VERSION = b"span-aware-v1"

# Synthetic Visa: 4276 1234 5678 901 + Luhn check digit 4. Not an issued card.
_STALE_ENTRY_CARD = "4276123456789014"


async def test_cache_a_entry_from_superseded_algorithm_version_is_not_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry written by a build with narrower detector coverage must miss.

    Seeds the store exactly as the previous build would have: an empty mapping
    (that build had no BANK_CARD rule) under that build's Cache-A key. Serving it
    would egress the card unredacted until the ~10h TTL expired.
    """
    from corp_llm_gateway.sanitizer import orchestrator

    store = InMemoryMappingStore()
    rules = Rules(rules=())
    text = f"Charge the pilot budget to {_STALE_ENTRY_CARD} tomorrow."

    with monkeypatch.context() as m:
        m.setattr(orchestrator, "_CACHE_A_ALGORITHM_VERSION", _SUPERSEDED_CACHE_A_VERSION)
        stale_key = orchestrator._content_hash("t1", rules, text, None, False)
    await store.set_dedup(stale_key, PlaceholderMapping(pairs=()), ttl_seconds=36000)

    orch = SanitizationOrchestrator(
        None,
        store,
        _StaticRulesLoader(rules),
        local_detectors=[RegexChecksumDetector()],
        oracle_enabled=False,
    )
    result = await orch.sanitize(text, team_id="t1", conversation_id="c1")

    assert result.cache_a_hit is False, "a superseded-version entry must not be served"
    assert _STALE_ENTRY_CARD not in result.sanitized_text, "LEAK: stale entry replayed the card"
    assert any(o == _STALE_ENTRY_CARD for o, _ in result.pairs)


# Cache A — oracle mode must not be replayed across a toggle (P1 fix) -------


async def test_oracle_mode_change_invalidates_cache_a_entry() -> None:
    """An entry written with the oracle OFF must not be reused once the oracle
    is re-enabled — reuse would silently skip an oracle-only finding."""
    store = InMemoryMappingStore()
    detector = RegexChecksumDetector()  # finds nothing in the test text below

    off = SanitizationOrchestrator(
        None,
        store,
        _StaticRulesLoader(Rules(rules=())),
        local_detectors=[detector],
        oracle_enabled=False,
    )
    client, captured = _client_returning_pairs([("Project Nightingale", "[PRODUCT_001]")])
    on = SanitizationOrchestrator(
        client,
        store,
        _StaticRulesLoader(Rules(rules=())),
        local_detectors=[detector],
        oracle_enabled=True,
    )

    text = "Deploy Project Nightingale to prod."
    r_off = await off.sanitize(text, team_id="t1", conversation_id="c-off")
    assert r_off.pairs == (), "oracle disabled + no local finding → nothing redacted"

    r_on = await on.sanitize(text, team_id="t1", conversation_id="c-on")
    assert r_on.cache_a_hit is False, "oracle mode change must miss the oracle-off cache entry"
    assert "Project Nightingale" not in r_on.sanitized_text
    assert len(captured) == 1, "the oracle must actually run once re-enabled"


# Cache A — a RUNTIME detector-set change must not be replayed ---------------

# The algorithm-version constant only retires entries across a BUILD boundary.
# `CORP_NER_ENABLED` adds a whole detector at RUNTIME, so two pods on the same
# image and the same Redis can hold different coverage under one version.

_R9_TERM = "Project Secret"
_R9_TEXT = "Deploy Project Secret today"


class _BlindDetector(PIIDetector):
    """Stands in for the narrow config: finds nothing."""

    async def detect(self, text: str) -> list[Finding]:
        return []


class _ProjectTermDetector(PIIDetector):
    """Stands in for the added detector: finds one fixed corp term."""

    async def detect(self, text: str) -> list[Finding]:
        start = text.find(_R9_TERM)
        if start < 0:
            return []
        return [
            Finding(
                text=_R9_TERM,
                label="PROJECT",
                start=start,
                end=start + len(_R9_TERM),
                score=1.0,
            )
        ]


def _r9_orch(store: InMemoryMappingStore, detectors: list[PIIDetector]) -> SanitizationOrchestrator:
    return SanitizationOrchestrator(
        None,
        store,
        _StaticRulesLoader(Rules(rules=())),
        local_detectors=detectors,
        oracle_enabled=False,
    )


async def test_cache_a_not_shared_between_narrow_and_wide_detector_sets() -> None:
    """Same algorithm version, one shared store, detector set widened at runtime.

    The narrow orchestrator stores an empty mapping for the text; the wide one
    must NOT serve that entry, or the term egresses unredacted for the ~10h TTL.
    """
    store = InMemoryMappingStore()
    narrow = _r9_orch(store, [_BlindDetector()])
    wide = _r9_orch(store, [_BlindDetector(), _ProjectTermDetector()])

    r_narrow = await narrow.sanitize(_R9_TEXT, team_id="t1", conversation_id="c-narrow")
    assert r_narrow.cache_a_hit is False
    assert r_narrow.pairs == (), "narrow config finds nothing and caches that"

    r_wide = await wide.sanitize(_R9_TEXT, team_id="t1", conversation_id="c-wide")
    assert r_wide.cache_a_hit is False, "a narrower config's entry must not be served"
    assert _R9_TERM not in r_wide.sanitized_text, "LEAK: narrow entry replayed the term"
    assert any(o == _R9_TERM for o, _ in r_wide.pairs)

    # Control: the wide detector really does redact against a COLD store, so the
    # assertions above cannot pass just because detection silently stopped.
    cold = _r9_orch(InMemoryMappingStore(), [_BlindDetector(), _ProjectTermDetector()])
    r_cold = await cold.sanitize(_R9_TEXT, team_id="t1", conversation_id="c-cold")
    assert r_cold.cache_a_hit is False
    assert _R9_TERM not in r_cold.sanitized_text
    assert any(o == _R9_TERM for o, _ in r_cold.pairs)


async def test_cache_a_still_shared_between_identically_configured_orchestrators() -> None:
    """The fingerprint must not fragment the cache across equal configs."""
    store = InMemoryMappingStore()
    first = _r9_orch(store, [_BlindDetector(), _ProjectTermDetector()])
    second = _r9_orch(store, [_BlindDetector(), _ProjectTermDetector()])

    r1 = await first.sanitize(_R9_TEXT, team_id="t1", conversation_id="c1")
    r2 = await second.sanitize(_R9_TEXT, team_id="t1", conversation_id="c2")
    assert r1.cache_a_hit is False
    assert r2.cache_a_hit is True, "equal policy must derive the same Cache-A key"
    assert r2.sanitized_text == r1.sanitized_text


async def test_cache_a_keyed_by_code_safe_detector_subset() -> None:
    """Narrowing the CODE-segment subset is a coverage change the key must see."""
    store = InMemoryMappingStore()
    detectors: list[PIIDetector] = [_BlindDetector(), _ProjectTermDetector()]
    wide = SanitizationOrchestrator(
        None,
        store,
        _StaticRulesLoader(Rules(rules=())),
        local_detectors=detectors,
        code_safe_detectors=detectors,
        oracle_enabled=False,
    )
    narrow_code = SanitizationOrchestrator(
        None,
        store,
        _StaticRulesLoader(Rules(rules=())),
        local_detectors=detectors,
        code_safe_detectors=[detectors[0]],
        oracle_enabled=False,
    )
    await wide.sanitize(_R9_TEXT, team_id="t1", conversation_id="c1")
    r2 = await narrow_code.sanitize(_R9_TEXT, team_id="t1", conversation_id="c2")
    assert r2.cache_a_hit is False, "a different code-safe subset must not share the key"


async def test_cache_a_keyed_by_gazetteer_and_allowlist_and_oracle_trigger() -> None:
    """Gazetteer terms, allowlist entries and the oracle trigger all change what
    the request path redacts, so each must change the Cache-A key."""
    from corp_llm_gateway.sanitizer.allowlist import Allowlist

    def fp(**kwargs: object) -> str:
        orch = SanitizationOrchestrator(
            None,
            InMemoryMappingStore(),
            _StaticRulesLoader(Rules(rules=())),
            local_detectors=[_BlindDetector()],
            oracle_enabled=False,
            **kwargs,  # type: ignore[arg-type]
        )
        assert orch._policy_fingerprint is not None
        return orch._policy_fingerprint

    base = fp()
    assert fp(gazetteer=Gazetteer({"nightingale": "PRODUCT"})) != base
    assert fp(gazetteer=Gazetteer({"nightingale": "PRODUCT"})) != fp(
        gazetteer=Gazetteer({"nightingale": "REGULATED"})
    )
    assert fp(allowlist=Allowlist(["ivan@example.com"])) != base
    assert fp(oracle_trigger="always") != base
    assert fp() == base, "equal configs must be stable"


async def test_cache_a_disabled_when_policy_fingerprint_cannot_be_computed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail closed: an uncomputable fingerprint disables Cache A entirely —
    never falls back to a key that ignores the policy."""
    import logging

    from corp_llm_gateway.sanitizer import orchestrator

    def _boom(_detector: object) -> str:
        raise RuntimeError("fingerprint source unavailable")

    store = InMemoryMappingStore()
    with pytest.MonkeyPatch.context() as m:
        m.setattr(orchestrator, "_detector_identity", _boom)
        with caplog.at_level(logging.WARNING):
            orch = _r9_orch(store, [_BlindDetector(), _ProjectTermDetector()])

    assert orch._policy_fingerprint is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("cache_a_disabled" in m for m in warnings)
    assert not any(_R9_TERM in m for m in warnings), "M1-14: no user content in logs"

    r1 = await orch.sanitize(_R9_TEXT, team_id="t1", conversation_id="c1")
    r2 = await orch.sanitize(_R9_TEXT, team_id="t1", conversation_id="c2")
    assert r1.cache_a_hit is False
    assert r2.cache_a_hit is False, "cache A must be off, not keyed on a constant"
    assert store._dedup == {}, "a disabled cache must not write entries either"
    assert _R9_TERM not in r2.sanitized_text


def test_policy_fingerprint_is_stable_across_python_hash_seeds() -> None:
    """Pods share one Redis: the fingerprint must not depend on PYTHONHASHSEED
    (i.e. must never use the salted builtin hash())."""
    import os
    import subprocess
    import sys

    script = (
        "from corp_llm_gateway.rules import Rules\n"
        "from corp_llm_gateway.rules.gazetteer import Gazetteer\n"
        "from corp_llm_gateway.sanitizer.allowlist import Allowlist\n"
        "from corp_llm_gateway.detectors import RegexChecksumDetector\n"
        "from corp_llm_gateway.sanitizer import SanitizationOrchestrator\n"
        "from corp_llm_gateway.storage import InMemoryMappingStore\n"
        "class L:\n"
        "    async def load(self, team_id): return Rules(rules=())\n"
        "o = SanitizationOrchestrator(\n"
        "    None, InMemoryMappingStore(), L(),\n"
        "    local_detectors=[RegexChecksumDetector()],\n"
        "    gazetteer=Gazetteer({'nightingale': 'PRODUCT'}),\n"
        "    allowlist=Allowlist(['ivan@example.com']),\n"
        "    oracle_enabled=False,\n"
        ")\n"
        "print(o._policy_fingerprint)\n"
    )

    def run(seed: str) -> str:
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return out.stdout.strip()

    first = run("0")
    assert first and first != "None"
    assert first == run("12345"), "fingerprint must be identical across processes"


# Cache A — the gazetteer's LEMMATIZER capability is part of the policy --------

# `term_signature()` covers the CONFIGURED terms, but matching runs through
# `_lemmatize_word`, which depends on the lazily-loaded pymorphy3 / spaCy
# handles. Those are absent without the `ner` extra (documented: NER needs
# Python 3.12, 3.14 degrades gracefully), so two pods can hold IDENTICAL term
# signatures and still match different text. If the model-less pod seeds Cache A
# first, the model-backed pod replays its empty mapping for the ~10h TTL.

_R11_TERM = "договор"
_R11_TEXT = "подписан договоры сегодня"
# Inflected form: matched only when a lemmatizer is available.
_R11_SURFACE = "договоры"


class _FakeParse:
    def __init__(self, normal_form: str) -> None:
        self.normal_form = normal_form


class _FakeMorph:
    """Stands in for pymorphy3 on a pod that HAS the `ner` extra installed."""

    def parse(self, word: str) -> list[_FakeParse]:
        return [_FakeParse(word.lower().removesuffix("ы"))]


def _gaz_orch(store: InMemoryMappingStore, gazetteer: Gazetteer) -> SanitizationOrchestrator:
    return SanitizationOrchestrator(
        None,
        store,
        _StaticRulesLoader(Rules(rules=())),
        gazetteer=gazetteer,
        oracle_enabled=False,
    )


async def test_cache_a_not_shared_across_gazetteer_lemmatizer_capability() -> None:
    from corp_llm_gateway.rules import gazetteer as gaz_module

    store = InMemoryMappingStore()  # ONE shared Cache A, as two pods share Redis

    # Both capabilities are simulated, never taken from the ambient interpreter,
    # so the test means the same thing with and without the `ner` extra.
    with pytest.MonkeyPatch.context() as m:
        m.setattr(gaz_module, "_try_load_ru_morph", lambda: None)
        blind_gaz = Gazetteer({_R11_TERM: "REGULATED"})
        blind = _gaz_orch(store, blind_gaz)
        r_blind = await blind.sanitize(_R11_TEXT, team_id="t1", conversation_id="c-blind")
        assert r_blind.pairs == (), "without a lemmatizer the inflected form is not matched"

    with pytest.MonkeyPatch.context() as m:
        m.setattr(gaz_module, "_try_load_ru_morph", _FakeMorph)
        lemma_gaz = Gazetteer({_R11_TERM: "REGULATED"})
        assert blind_gaz.term_signature() == lemma_gaz.term_signature(), (
            "the terms are identical — only the lemmatizer differs"
        )
        lemma = _gaz_orch(store, lemma_gaz)
        r_lemma = await lemma.sanitize(_R11_TEXT, team_id="t1", conversation_id="c-lemma")

        assert r_lemma.cache_a_hit is False, "a model-less pod's entry must not be served"
        assert _R11_SURFACE not in r_lemma.sanitized_text, (
            "LEAK: the model-less entry replayed the gazetteer term unredacted"
        )

        # Control: against a COLD store the lemmatizing config really does redact,
        # so the assertion above cannot pass by detection silently failing.
        cold = _gaz_orch(InMemoryMappingStore(), Gazetteer({_R11_TERM: "REGULATED"}))
        r_cold = await cold.sanitize(_R11_TEXT, team_id="t1", conversation_id="c-cold")
        assert _R11_SURFACE not in r_cold.sanitized_text


async def test_cache_a_still_shared_across_equal_lemmatizer_capability() -> None:
    """The capability probe must not fragment the cache between equal pods."""
    store = InMemoryMappingStore()
    first = _gaz_orch(store, Gazetteer({_R11_TERM: "REGULATED"}))
    second = _gaz_orch(store, Gazetteer({_R11_TERM: "REGULATED"}))

    r1 = await first.sanitize(_R11_TEXT, team_id="t1", conversation_id="c1")
    r2 = await second.sanitize(_R11_TEXT, team_id="t1", conversation_id="c2")
    assert r1.cache_a_hit is False
    assert r2.cache_a_hit is True


# Cache A — dual_ner's ENGINE CAPABILITY is part of the policy ---------------

# Same hole as the gazetteer's lemmatizer, on the PRIMARY NER path. With
# `CORP_LLM_REQUIRE_NER` off (the default) a DualNerDetector whose engines
# cannot load their models disables them, logs, and returns `[]` — so a
# model-less pod seeds Cache A with an EMPTY mapping under a key that folds
# only the class name. A model-backed pod on the same Redis replays it and a
# PERSON only NER catches egresses in the clear for the ~10h TTL.

_R12_TERM = "Иванов"
_R12_TEXT = "договор подписал Иванов вчера"

_FAKE_RU_MODELS = ("segmenter", "tagger")
_FAKE_EN_NLP = "nlp"


def _fake_infer_ru(_models: object, text: str) -> list[Finding]:
    """Stands in for Natasha on a pod that HAS the `ner` extra installed."""
    start = text.find(_R12_TERM)
    if start < 0:
        return []
    return [
        Finding(
            text=_R12_TERM,
            label="PERSON",
            start=start,
            end=start + len(_R12_TERM),
            score=0.8,
        )
    ]


def _no_ner_models(*_args: object, **_kwargs: object) -> object:
    raise RuntimeError("ner_ru requires the 'ner' extra: pip install -e '.[ner]'")


def _patch_ner_models_present(m: pytest.MonkeyPatch) -> None:
    """Simulate a pod WITH both NER engines, never the ambient interpreter, so
    the test means the same thing on 3.14 (no models) and on 3.12/CI."""
    from corp_llm_gateway.detectors import ner_en, ner_ru

    m.setattr(ner_ru, "_load_natasha", lambda: _FAKE_RU_MODELS)
    m.setattr(ner_ru, "_infer_ru", _fake_infer_ru)
    m.setattr(ner_en, "_load_spacy", lambda: _FAKE_EN_NLP)
    m.setattr(ner_en, "_infer_en", lambda _nlp, _text: [])


def _patch_ner_models_absent(m: pytest.MonkeyPatch) -> None:
    from corp_llm_gateway.detectors import ner_en, ner_ru

    m.setattr(ner_ru, "_load_natasha", _no_ner_models)
    m.setattr(ner_en, "_load_spacy", _no_ner_models)


def _dual_ner_orch(store: InMemoryMappingStore) -> SanitizationOrchestrator:
    from corp_llm_gateway.detectors import DualNerDetector

    return SanitizationOrchestrator(
        None,
        store,
        _StaticRulesLoader(Rules(rules=())),
        # require_ner=False pins the DOCUMENTED default (fail open): a disabled
        # engine returns [] instead of raising, which is what seeds the bad entry.
        local_detectors=[DualNerDetector(require_ner=False)],
        oracle_enabled=False,
    )


async def test_cache_a_not_shared_across_dual_ner_engine_capability() -> None:
    store = InMemoryMappingStore()  # ONE shared Cache A, as two pods share Redis

    with pytest.MonkeyPatch.context() as m:
        _patch_ner_models_absent(m)
        blind = _dual_ner_orch(store)
        r_blind = await blind.sanitize(_R12_TEXT, team_id="t1", conversation_id="c-blind")
        assert r_blind.pairs == (), "a model-less pod finds nothing and caches that"

    with pytest.MonkeyPatch.context() as m:
        _patch_ner_models_present(m)
        full = _dual_ner_orch(store)
        r_full = await full.sanitize(_R12_TEXT, team_id="t1", conversation_id="c-full")

        assert r_full.cache_a_hit is False, "a model-less pod's entry must not be served"
        assert _R12_TERM not in r_full.sanitized_text, (
            "LEAK: the model-less entry replayed the PERSON unredacted"
        )
        assert any(o == _R12_TERM for o, _ in r_full.pairs)

        # Control: against a COLD store the model-backed config really does
        # redact, so the assertions above cannot pass by detection silently
        # stopping.
        cold = _dual_ner_orch(InMemoryMappingStore())
        r_cold = await cold.sanitize(_R12_TEXT, team_id="t1", conversation_id="c-cold")
        assert r_cold.cache_a_hit is False
        assert _R12_TERM not in r_cold.sanitized_text
        assert any(o == _R12_TERM for o, _ in r_cold.pairs)


async def test_cache_a_still_shared_across_equal_dual_ner_capability() -> None:
    """The capability probe must not fragment the cache between equal pods."""
    with pytest.MonkeyPatch.context() as m:
        _patch_ner_models_present(m)
        store = InMemoryMappingStore()
        first = _dual_ner_orch(store)
        second = _dual_ner_orch(store)

        r1 = await first.sanitize(_R12_TEXT, team_id="t1", conversation_id="c1")
        r2 = await second.sanitize(_R12_TEXT, team_id="t1", conversation_id="c2")
        assert r1.cache_a_hit is False
        assert r2.cache_a_hit is True, "equal capability must derive the same Cache-A key"
        assert r2.sanitized_text == r1.sanitized_text


class _BadSignatureDetector(_ProjectTermDetector):
    """A detector whose capability probe is broken."""

    def policy_signature(self) -> tuple[str, ...]:
        raise RuntimeError("capability probe unavailable")


class _NonStringSignatureDetector(_ProjectTermDetector):
    def policy_signature(self) -> tuple[str, ...]:
        return (object(),)  # type: ignore[return-value]


@pytest.mark.parametrize(
    "detector", [_BadSignatureDetector(), _NonStringSignatureDetector()], ids=["raises", "non-str"]
)
async def test_unusable_policy_signature_disables_cache_a(detector: PIIDetector) -> None:
    """A detector that cannot state its capability must take Cache A OFF, never
    fall back to class identity — that fallback is exactly the replay hole."""
    store = InMemoryMappingStore()
    orch = _r9_orch(store, [detector])
    assert orch._policy_fingerprint is None

    r1 = await orch.sanitize(_R9_TEXT, team_id="t1", conversation_id="c1")
    r2 = await orch.sanitize(_R9_TEXT, team_id="t1", conversation_id="c2")
    assert r1.cache_a_hit is False
    assert r2.cache_a_hit is False
    assert store._dedup == {}
    assert _R9_TERM not in r2.sanitized_text


# ---------------------------------------------------------------------------
# Task 13 — rule-matching performance: _rule_pattern compile caching
# ---------------------------------------------------------------------------


def test_rule_pattern_compile_is_cached() -> None:
    """`_rule_pattern` must not recompile the same rule source on every call —
    it was measured recompiling on every call at ~11-22 ms/request for
    200 rules x 10.8 KB against a ~6 ms p50 CPU budget."""
    from corp_llm_gateway.sanitizer.orchestrator import _rule_pattern

    _rule_pattern.cache_clear()
    _rule_pattern("cache-test-unique-source-task13")
    hits_after_first_call = _rule_pattern.cache_info().hits
    _rule_pattern("cache-test-unique-source-task13")
    hits_after_second_call = _rule_pattern.cache_info().hits
    assert hits_after_second_call > hits_after_first_call


async def test_detect_reuses_threaded_rule_matches_instead_of_recomputing() -> None:
    """`sanitize()` computes `_rule_matches` once and threads it into `_detect()`;
    a rule match must still be applied (this pins the threading didn't drop it),
    and the chunked path is untouched by this change (covered separately)."""
    rules = Rules(rules=(Rule("acme", "[COMPANY_001]"),))
    orch = SanitizationOrchestrator(
        None,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        gazetteer=Gazetteer({}),
        oracle_enabled=False,
    )
    result = await orch.sanitize("contact acme today", team_id="t1", conversation_id="c1")
    assert result.pairs == (("acme", "[COMPANY_001]"),)
    assert result.sanitized_text == "contact [COMPANY_001] today"


async def test_sanitize_computes_rule_matches_only_once_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sanitize()` and `_detect()` operate on the SAME full text, so
    `_rule_matches` must be threaded through rather than recomputed."""
    from corp_llm_gateway.sanitizer import orchestrator as orch_module

    calls: list[str] = []
    original = orch_module._rule_matches

    def _counting(rules: Rules, text: str) -> tuple:
        calls.append(text)
        return original(rules, text)

    monkeypatch.setattr(orch_module, "_rule_matches", _counting)

    rules = Rules(rules=(Rule("acme", "[COMPANY_001]"),))
    orch = SanitizationOrchestrator(
        None,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        gazetteer=Gazetteer({}),
        oracle_enabled=False,
    )
    await orch.sanitize("contact acme today", team_id="t1", conversation_id="c1")
    assert calls == ["contact acme today"], (
        "rule_matches must be computed exactly once for the shared full text"
    )


# ---- Critical 2: O(n^2) span selection blocks the event loop ---------------


def test_non_overlapping_spans_overlap_and_add_boundary_conditions() -> None:
    """Unit-level correctness of the replacement span tracker: empty, single,
    touching-but-not-overlapping, and a genuine overlap."""
    from corp_llm_gateway.sanitizer.orchestrator import _NonOverlappingSpans

    spans = _NonOverlappingSpans()
    assert spans.overlaps(0, 1) is False  # empty tracker: nothing occupied

    spans.add(10, 20)
    assert spans.overlaps(10, 20) is True  # identical span
    assert spans.overlaps(0, 10) is False  # touches at the boundary, no overlap
    assert spans.overlaps(20, 30) is False  # touches at the boundary, no overlap
    assert spans.overlaps(5, 11) is True  # overlaps the start
    assert spans.overlaps(19, 25) is True  # overlaps the end
    assert spans.overlaps(12, 18) is True  # fully contained

    spans.add(0, 10)
    spans.add(20, 30)
    assert spans.overlaps(9, 21) is True  # spans both neighbors
    assert spans.overlaps(30, 40) is False  # past every accepted span


def test_non_overlapping_spans_zero_length_candidate_diverges_at_left_edge() -> None:
    """Pin a known, narrow divergence from the old linear scan's semantics
    (`any(start < used_end and end > used_start for ...)`): a zero-length
    candidate (start == end) sitting exactly at an accepted span's own start
    is flagged as overlapping here (bisect finds the accepted span at the
    insertion point and its end is past the candidate's start), but the old
    strict `end > used_start` check said False for that same query. Every
    other zero-length position (right edge, fully inside, before, after)
    agrees with the old semantics — only the exact left-edge touch differs.
    Unreachable in practice: `_rule_matches`/`_plan_replacements` only ever
    add spans from a non-empty regex match, so start < end always holds for
    every candidate this engine actually produces (0 divergences observed in
    a 200k-case fuzz against the old linear scan)."""
    from corp_llm_gateway.sanitizer.orchestrator import _NonOverlappingSpans

    spans = _NonOverlappingSpans()
    spans.add(10, 20)
    assert spans.overlaps(10, 10) is True
    assert spans.overlaps(20, 20) is False
    assert spans.overlaps(15, 15) is True
    assert spans.overlaps(5, 5) is False
    assert spans.overlaps(25, 25) is False


def test_rule_matches_stays_within_budget_on_large_duplicate_heavy_text() -> None:
    """CRITICAL 2 regression: a single short rule term repeated ~20k times in a
    100 KiB leaf used to make `_rule_matches`' O(candidates x accepted) overlap
    scan take multiple SYNCHRONOUS seconds (measured ~5s pre-fix on this exact
    shape), blocking the asyncio event loop against a ~6ms p50 / 4s p99 budget
    (CLAUDE.md — no GPU escape hatch). Generous 1s ceiling: pre-fix this test
    fails by roughly 5x; post-fix it completes in tens of milliseconds."""
    import time

    from corp_llm_gateway.sanitizer.orchestrator import _rule_matches

    text = ("corp " * 20400)[:101997]
    rules = Rules(rules=(Rule("corp", "[C]"),))

    start = time.perf_counter()
    matches = _rule_matches(rules, text)
    elapsed = time.perf_counter() - start

    assert len(matches) > 10000
    assert elapsed < 1.0, f"_rule_matches took {elapsed:.3f}s, budget is 1.0s"


def test_plan_replacements_stays_within_budget_on_large_duplicate_heavy_text() -> None:
    """Same CRITICAL 2 regression for `_plan_replacements`'s candidate-pool
    selection (measured ~5.6s pre-fix on this exact shape)."""
    import time

    from corp_llm_gateway.sanitizer.orchestrator import _plan_replacements, _rule_matches

    text = ("corp " * 20400)[:101997]
    rules = Rules(rules=(Rule("corp", "[C]"),))
    rule_matches = _rule_matches(rules, text)
    pairs = tuple(dict.fromkeys((m.original, m.replacement) for m in rule_matches))

    start = time.perf_counter()
    plan = _plan_replacements(text, pairs, rule_matches)
    elapsed = time.perf_counter() - start

    assert len(plan.pairs) == 1
    assert "corp" not in plan.sanitized_text
    assert elapsed < 1.0, f"_plan_replacements took {elapsed:.3f}s, budget is 1.0s"
