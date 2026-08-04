"""The Claude Code identity preamble must reach Anthropic byte-identical.

Anthropic's OAuth-authenticated ``/v1/messages`` route (``sk-ant-oat`` tokens)
accepts a request as a Claude Code request on the strength of the leading
``system`` block. Claude Code emits that line as its OWN block and re-recognises
it by EXACT string equality — ``strings`` on the installed binary
(``~/.local/share/claude/versions/2.1.221``) shows the three identity literals
collected in one ``Set`` and the block assembler pulling the block matching
``Set.has(...)`` out ahead of the rest, next to the ``x-anthropic-billing-header``
block. One redacted character inside it and the request stops being a Claude
Code request.

The gateway sanitizes ``data["system"]``, so it can mutate that block. These
tests drive the real ``pre_call`` — with the production detector set — over
realistic ``system`` shapes and pin four things: the leading identity block is
not rewritten; NOTHING ELSE is exempt (a padded copy, a later block,
``instructions``, a user message, a ``tool_result``, any request that cannot
reach the OAuth route at all); the billing-marker blocks ahead of it are
sanitized WHOLE, so nothing is reconstructed over a redaction; and the exempt
block is still scanned by Stage 0 and Stage 5.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.detectors import DualNerDetector, RegexChecksumDetector
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
from corp_llm_gateway.payload import DEFAULT_THRESHOLD_BYTES
from corp_llm_gateway.rules import Gazetteer, Rule, Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard
from corp_llm_gateway.sanitizer.identity_preamble import (
    CLAUDE_CODE_IDENTITY_PREAMBLES,
    is_identity_preamble,
    leading_identity_block_index,
)
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo

PREAMBLE = "You are Claude Code, Anthropic's official CLI for Claude."
# Canonical AWS example key id — matched by RegexChecksumDetector's AKIA pattern,
# so the adversarial assertions hold with or without the NER extras installed.
SECRET = "AKIAIOSFODNN7EXAMPLE"
# An operator replace.md rule matching inside the preamble. Deterministic on both
# interpreters (the NER extras are 3.12-only), and it is exactly the surface the
# carve-out must not be able to disable outside the leading system block.
RULE_TERM = "Claude Code"
RULE_REPLACEMENT = "[PRODUCT_001]"
BILLING_MARKER = "x-anthropic-billing-header:"
BILLING_BLOCK = {"type": "text", "text": f"{BILLING_MARKER} claude-code"}
# An operator rule matching inside the billing MARKER itself. Rule matching is a
# case-insensitive substring test, so this rewrites `x-anthropic-billing-header:`
# — the arrangement that makes the identity block the leading one.
MARKER_RULE_TERM = "anthropic"
MARKER_RULE_REPLACEMENT = "[ORG_999]"
# A confidential value a caller can park in a billing block, redacted ONLY by an
# operator rule whose pattern spans the marker/remainder boundary: no other
# detector touches it on either interpreter (checked with and without the NER
# extras), so the leak assertion below cannot be satisfied by something else.
BOUNDARY_SECRET = "Quadrant Seven"
BOUNDARY_BLOCK = f"{BILLING_MARKER} cost-center={BOUNDARY_SECRET}, team=core"
BOUNDARY_RULE_REPLACEMENT = "[PROJECT_007]"
# The bridge the carve-out exists for: it applies only while this is armed AND
# the model resolves to the Anthropic provider.
OAUTH_TOKEN = "sk-ant-oat01-fake-preamble"
ANTHROPIC_MODEL = "claude-sonnet-4-5"


class _StaticRulesLoader(RulesLoader):
    def __init__(self, rules: Rules | None = None) -> None:
        self._rules = rules or Rules(rules=())

    async def load(self, team_id: str) -> Rules:
        return self._rules


def _oracle_client() -> tuple[CorpLlmClient, list[int]]:
    """Mock corp-LLM oracle that returns no pairs and counts its calls."""
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
                                        "arguments": json.dumps({"pairs": []}),
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


def _rules() -> Rules:
    return Rules(rules=(Rule(pattern=RULE_TERM, replacement=RULE_REPLACEMENT),))


def _guardrail(
    *,
    rules: Rules | None = None,
    dlp_guard: DlpEgressGuard | None = None,
    size_threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
    forward_anthropic_auth: bool = True,
) -> tuple[CorpLlmGuardrail, ListSink, list[int]]:
    """The production wiring: `bootstrap._build_orchestrator`'s detector set."""
    client, call_count = _oracle_client()
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(rules),
        local_detectors=[RegexChecksumDetector(), DualNerDetector()],
        gazetteer=Gazetteer.from_defaults(),
        size_threshold_bytes=size_threshold_bytes,
    )
    token_store = InMemoryTokenStore()
    now = datetime.now(UTC)
    token_store.upsert(
        TokenInfo(
            corp_token="tok-1",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    sink = ListSink()
    guardrail = CorpLlmGuardrail(
        orch,
        AuthMiddleware(token_store),
        AuditLogger(sink, gateway_version="0.0.1"),
        dlp_guard=dlp_guard,
        forward_anthropic_auth=forward_anthropic_auth,
    )
    return guardrail, sink, call_count


def _data(
    *,
    system: Any = None,
    content: Any = "hello",
    instructions: str | None = None,
    model: str = ANTHROPIC_MODEL,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": f"Bearer {OAUTH_TOKEN}"},
    }
    if system is not None:
        data["system"] = system
    if instructions is not None:
        data["instructions"] = instructions
    return data


# ---- the leading system block survives -------------------------------------


async def test_preamble_block_is_byte_identical() -> None:
    g, _, call_count = _guardrail()
    system = [{"type": "text", "text": PREAMBLE}]
    out = await g.pre_call(_data(system=system))

    assert out["system"] == system
    assert out["system"][0]["text"] == PREAMBLE
    assert call_count[0] == 0, "no gazetteer hit ⇒ the oracle must not be called"


async def test_preamble_after_the_billing_header_block_is_byte_identical() -> None:
    """litellm keeps `x-anthropic-billing-header` blocks on the first-party route
    (`_filter_billing_headers_from_system` only runs for providers that reject
    them), so the identity block can arrive at index 1, not 0."""
    g, _, _ = _guardrail(rules=_rules())
    system = [dict(BILLING_BLOCK), {"type": "text", "text": PREAMBLE}]
    out = await g.pre_call(_data(system=system))

    assert out["system"][1]["text"] == PREAMBLE
    assert out["system"][0]["type"] == "text", "block order must be preserved"


async def test_preamble_as_plain_string_system_is_byte_identical() -> None:
    """Chat Completions sends `system` as a bare string, not a block list."""
    g, _, _ = _guardrail(rules=_rules())
    out = await g.pre_call(_data(system=PREAMBLE))

    assert out["system"] == PREAMBLE


@pytest.mark.parametrize("preamble", sorted(CLAUDE_CODE_IDENTITY_PREAMBLES))
async def test_every_known_identity_preamble_is_byte_identical(preamble: str) -> None:
    g, _, _ = _guardrail()
    out = await g.pre_call(_data(system=[{"type": "text", "text": preamble}]))
    assert out["system"][0]["text"] == preamble


# ---- the match is byte-exact: padding buys the caller nothing ---------------


async def test_whitespace_padded_preamble_is_not_carved_out() -> None:
    """Upstream matches the literal by exact equality, so a padded copy is not
    the thing being protected — it is sanitized like any other leaf."""
    g, _, _ = _guardrail(rules=_rules())
    padded = PREAMBLE + "\n"
    out = await g.pre_call(_data(system=[{"type": "text", "text": padded}]))

    assert RULE_TERM not in out["system"][0]["text"], "padded leaf skipped the detectors"


async def test_oversized_padded_preamble_still_hits_the_fail_closed_oversize_policy() -> None:
    """Whitespace tolerance would let an arbitrarily large leaf return unchanged,
    defeating the fail-closed oversize policy (M4)."""
    g, _, _ = _guardrail(size_threshold_bytes=100)
    padded = PREAMBLE + " " * 500

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(_data(system=[{"type": "text", "text": padded}]))
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_OVERSIZE_BLOCKED"


async def test_exact_preamble_is_far_below_the_size_threshold() -> None:
    """The exempt leaf is bounded by construction: it is byte-equal to a fixed
    literal, so no unbounded content can ride through the exemption."""
    for preamble in CLAUDE_CODE_IDENTITY_PREAMBLES:
        assert len(preamble.encode("utf-8")) < DEFAULT_THRESHOLD_BYTES


# ---- the carve-out reaches ONLY the leading system block --------------------


async def test_identity_literal_in_a_user_message_is_still_sanitized() -> None:
    """A user message is not the OAuth identity block; an operator rule matching
    inside it must still apply."""
    g, _, _ = _guardrail(rules=_rules())
    out = await g.pre_call(_data(content=PREAMBLE))

    assert RULE_TERM not in out["messages"][0]["content"]
    assert RULE_REPLACEMENT in out["messages"][0]["content"]


async def test_identity_literal_in_a_tool_result_is_still_sanitized() -> None:
    g, _, _ = _guardrail(rules=_rules())
    content = [
        {
            "type": "tool_result",
            "tool_use_id": "tu_1",
            "content": [{"type": "text", "text": PREAMBLE}],
        }
    ]
    out = await g.pre_call(_data(content=content))

    assert RULE_TERM not in json.dumps(out["messages"][0]["content"])


async def test_identity_literal_in_a_document_block_is_still_sanitized() -> None:
    g, _, _ = _guardrail(rules=_rules())
    content = [
        {
            "type": "document",
            "title": PREAMBLE,
            "source": {"type": "text", "media_type": "text/plain", "data": PREAMBLE},
        }
    ]
    out = await g.pre_call(_data(content=content))

    assert RULE_TERM not in json.dumps(out["messages"][0]["content"])


async def test_identity_literal_in_instructions_is_still_sanitized() -> None:
    """`instructions` is the Responses-API prompt field, not the Anthropic
    `system` block the OAuth route keys on."""
    g, _, _ = _guardrail(rules=_rules())
    out = await g.pre_call(_data(instructions=PREAMBLE))

    assert RULE_TERM not in out["instructions"]


async def test_identity_literal_in_a_non_leading_system_block_is_still_sanitized() -> None:
    """Claude Code puts the identity line first; a copy sitting after an ordinary
    prompt block is not the client's identity block."""
    g, _, _ = _guardrail(rules=_rules())
    system = [
        {"type": "text", "text": "Follow the team style guide."},
        {"type": "text", "text": PREAMBLE},
    ]
    out = await g.pre_call(_data(system=system))

    assert RULE_TERM not in out["system"][1]["text"]


# ---- the carve-out reaches ONLY the route that needs it ---------------------


async def test_identity_block_is_sanitized_when_the_bridge_is_off() -> None:
    """The exemption exists to keep Anthropic's OAuth preamble byte-exact. With
    the bridge off there is no OAuth request to protect, so the block is ordinary
    content."""
    g, _, _ = _guardrail(rules=_rules(), forward_anthropic_auth=False)
    out = await g.pre_call(_data(system=[{"type": "text", "text": PREAMBLE}]))

    assert RULE_TERM not in out["system"][0]["text"]
    assert RULE_REPLACEMENT in out["system"][0]["text"]


async def test_string_identity_system_is_sanitized_when_the_bridge_is_off() -> None:
    g, _, _ = _guardrail(rules=_rules(), forward_anthropic_auth=False)
    out = await g.pre_call(_data(system=PREAMBLE))

    assert RULE_TERM not in out["system"]
    assert RULE_REPLACEMENT in out["system"]


@pytest.mark.parametrize("model", ["gpt-4o", "corp-glm-5"])
async def test_identity_block_is_sanitized_for_a_non_anthropic_model(model: str) -> None:
    """Bridge armed, but the request resolves to another provider: pasting one of
    the three literals into `system` must not buy a bypass of replace.md /
    gazetteer / NER on an OpenAI or corp-vLLM call."""
    g, _, _ = _guardrail(rules=_rules())
    out = await g.pre_call(_data(system=[{"type": "text", "text": PREAMBLE}], model=model))

    assert RULE_TERM not in out["system"][0]["text"]
    assert RULE_REPLACEMENT in out["system"][0]["text"]


@pytest.mark.parametrize("model", ["gpt-4o", "corp-glm-5"])
async def test_string_identity_system_is_sanitized_for_a_non_anthropic_model(model: str) -> None:
    g, _, _ = _guardrail(rules=_rules())
    out = await g.pre_call(_data(system=PREAMBLE, model=model))

    assert RULE_TERM not in out["system"]


async def test_secret_behind_an_identity_block_is_redacted_off_the_bridge() -> None:
    g, _, _ = _guardrail(forward_anthropic_auth=False)
    system = [
        {"type": "text", "text": PREAMBLE},
        {"type": "text", "text": f"Deploy with access key {SECRET} against prod."},
    ]
    out = await g.pre_call(_data(system=system))

    assert SECRET not in json.dumps(out["system"])


# ---- the billing blocks ahead of it are sanitized WHOLE ---------------------


@pytest.mark.parametrize(
    "pattern",
    [
        f"{BILLING_MARKER} cost-center={BOUNDARY_SECRET}",  # whole marker + remainder
        f"header: cost-center={BOUNDARY_SECRET}",  # tail of the marker + remainder
    ],
)
async def test_a_rule_spanning_the_billing_marker_boundary_redacts(pattern: str) -> None:
    """The reason the marker is no longer held back and reattached.

    Rules are literal substring matches over the text handed to the orchestrator.
    Sanitizing only the caller remainder hid every pattern that reached back into
    the marker, and reattaching the marker afterwards rebuilt the full original
    on the way to Anthropic — a zero-originals-leak (M1-14) violation, and one
    Stage 5 cannot catch because it does not replay replace.md rules.
    """
    rules = Rules(rules=(Rule(pattern=pattern, replacement=BOUNDARY_RULE_REPLACEMENT),))
    g, _, _ = _guardrail(rules=rules)
    system = [
        {"type": "text", "text": BOUNDARY_BLOCK},
        {"type": "text", "text": PREAMBLE},
    ]
    out = await g.pre_call(_data(system=system))

    assert BOUNDARY_SECRET not in json.dumps(out["system"]), "boundary-spanning rule bypassed"
    assert BOUNDARY_RULE_REPLACEMENT in out["system"][0]["text"]
    assert out["system"][0]["text"].endswith(", team=core"), "the unmatched tail was dropped"
    assert out["system"][1]["text"] == PREAMBLE


async def test_a_rule_matching_the_billing_marker_keeps_its_rewrite() -> None:
    """A rewritten marker is left rewritten, even though the identity block then
    stops being a LEADING one and Anthropic's OAuth route may refuse the request.

    Restoring the marker cannot be told apart from restoring the boundary-spanning
    original above, so the refusal is the cheaper of the two failures.
    """
    rules = Rules(rules=(Rule(pattern=MARKER_RULE_TERM, replacement=MARKER_RULE_REPLACEMENT),))
    g, _, _ = _guardrail(rules=rules)
    system = [
        {"type": "text", "text": f"{BILLING_MARKER} cost-center=demo"},
        {"type": "text", "text": PREAMBLE},
    ]
    out = await g.pre_call(_data(system=system))

    rewritten = f"x-{MARKER_RULE_REPLACEMENT}-billing-header: cost-center=demo"
    assert out["system"][0]["text"] == rewritten
    assert leading_identity_block_index(out["system"]) is None
    assert out["system"][1]["text"] == PREAMBLE


async def test_two_billing_markers_ahead_of_the_identity_block_all_survive() -> None:
    """Nothing matches inside these blocks, so both reach egress byte-identical
    and the identity block is still the leading one."""
    g, _, _ = _guardrail(rules=_rules())
    system = [
        {"type": "text", "text": f"{BILLING_MARKER} a=1"},
        {"type": "text", "text": f"{BILLING_MARKER} b=2"},
        {"type": "text", "text": PREAMBLE},
    ]
    out = await g.pre_call(_data(system=system))

    assert [block["text"] for block in out["system"][:2]] == [
        f"{BILLING_MARKER} a=1",
        f"{BILLING_MARKER} b=2",
    ]
    assert leading_identity_block_index(out["system"]) == 2


async def test_a_billing_block_with_no_remainder_is_unchanged() -> None:
    g, _, _ = _guardrail(rules=_rules())
    system = [{"type": "text", "text": BILLING_MARKER}, {"type": "text", "text": PREAMBLE}]
    out = await g.pre_call(_data(system=system))

    assert out["system"][0]["text"] == BILLING_MARKER
    assert leading_identity_block_index(out["system"]) == 1


async def test_other_keys_on_a_billing_block_survive_sanitization() -> None:
    """Claude Code hangs `cache_control` off these blocks; the walker must not
    drop the rest of the block."""
    g, _, _ = _guardrail()
    system = [
        {
            "type": "text",
            "text": f"{BILLING_MARKER} cost-center=demo",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": PREAMBLE},
    ]
    out = await g.pre_call(_data(system=system))

    assert out["system"][0] == system[0]


async def test_a_secret_in_a_billing_block_is_still_redacted() -> None:
    """Nothing about these blocks is exempt — the marker must not become a
    smuggling lane."""
    g, _, _ = _guardrail()
    system = [
        {"type": "text", "text": f"{BILLING_MARKER} cost-center={SECRET}"},
        {"type": "text", "text": PREAMBLE},
    ]
    out = await g.pre_call(_data(system=system))

    assert SECRET not in json.dumps(out["system"])
    assert out["system"][0]["text"].startswith(BILLING_MARKER)
    assert out["system"][1]["text"] == PREAMBLE


async def test_a_rule_matching_the_billing_remainder_still_applies() -> None:
    g, _, _ = _guardrail(rules=_rules())
    system = [
        {"type": "text", "text": f"{BILLING_MARKER} tool={RULE_TERM}"},
        {"type": "text", "text": PREAMBLE},
    ]
    out = await g.pre_call(_data(system=system))

    assert out["system"][0]["text"] == f"{BILLING_MARKER} tool={RULE_REPLACEMENT}"


async def test_orchestrator_does_not_exempt_the_literal_on_its_own() -> None:
    """The per-leaf chokepoint has no field/position context, so it must not hold
    the carve-out — otherwise every walker path inherits it."""
    client, _ = _oracle_client()
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(_rules()),
        local_detectors=[RegexChecksumDetector()],
    )
    result = await orch.sanitize(PREAMBLE, team_id="t1", conversation_id="c1")

    assert RULE_TERM not in result.sanitized_text


# ---- adversarial: preamble plus a real secret in the same field ------------


async def test_secret_in_a_later_system_block_is_redacted_preamble_is_not() -> None:
    """The shape Claude Code actually sends: identity block first, then the rest."""
    g, _, _ = _guardrail()
    system = [
        {"type": "text", "text": PREAMBLE},
        {"type": "text", "text": f"Deploy with access key {SECRET} against prod."},
    ]
    out = await g.pre_call(_data(system=system))

    assert out["system"][0]["text"] == PREAMBLE, "identity block must survive byte-identical"
    assert SECRET not in json.dumps(out["system"]), "raw secret reached egress"


async def test_preamble_prefix_does_not_exempt_the_rest_of_the_leaf() -> None:
    """The carve-out is whole-leaf, so it cannot be used to smuggle a secret.

    Claude Code keeps the identity line in its own block, so this shape does not
    arise on the OAuth route; what matters is that a leaf merely STARTING with
    the preamble is sanitized like any other."""
    g, _, _ = _guardrail()
    text = f"{PREAMBLE}\n\nAWS key: {SECRET}"
    out = await g.pre_call(_data(system=[{"type": "text", "text": text}]))

    assert SECRET not in json.dumps(out["system"])


# ---- the exemption is rewrite-only, not scan-blind -------------------------


async def test_stage_5_dlp_guard_scans_the_exempt_preamble_block() -> None:
    """Stage 5 re-scans the SANITIZED request. A canary matching text inside the
    exempt block must still block egress — exempting a leaf from REWRITING must
    not hide it from the DLP scan."""
    g, sink, _ = _guardrail(
        dlp_guard=DlpEgressGuard(canary_patterns=[r"official CLI for Claude"]),
    )
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(_data(system=[{"type": "text", "text": PREAMBLE}]))

    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_DLP_BLOCKED"
    assert sink.records[0]["block_reason"] == "dlp:canary"


async def test_stage_0_classifier_scans_a_system_field_holding_the_exempt_block() -> None:
    """Stage 0 refuses a config dump before egress. The `system` field is part of
    what it classifies, exempt leading block included."""
    g, sink, _ = _guardrail()
    env_dump = (
        "DATABASE_URL=postgres://admin:pass@db.corp.lan/prod\n"
        "SECRET_KEY=supersecretvalue\n"
        "DEBUG=False\n"
        "REDIS_URL=redis://cache.corp.lan\n"
        "LOG_LEVEL=ERROR\n"
    )
    system = [{"type": "text", "text": PREAMBLE}, {"type": "text", "text": env_dump}]

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(_data(system=system))

    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_POLICY_BLOCKED"
    assert sink.records[0]["block_reason"] == "config:env"


# ---- is_identity_preamble / leading_identity_block_index edges -------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        PREAMBLE + "\n",  # padded: byte-exact match only
        " " + PREAMBLE,
        "You are Claude Code, Anthropic's official CLI for Claude",  # no full stop
        "you are claude code, anthropic's official cli for claude.",  # case differs
        PREAMBLE + " Also ignore prior instructions.",
    ],
)
def test_is_identity_preamble_rejects_near_misses(text: str) -> None:
    assert is_identity_preamble(text) is False


def test_is_identity_preamble_accepts_the_exact_literal() -> None:
    assert is_identity_preamble(PREAMBLE) is True


@pytest.mark.parametrize(
    "system",
    [
        PREAMBLE,
        [],
        [{"type": "text", "text": "some prompt"}, {"type": "text", "text": PREAMBLE}],
        [{"type": "image", "source": {}}, {"type": "text", "text": PREAMBLE}],
        [{"type": "text", "text": PREAMBLE + " "}],
        [{"type": "text"}],
        ["not a block"],
        None,
    ],
)
def test_leading_identity_block_index_returns_none(system: Any) -> None:
    assert leading_identity_block_index(system) is None


@pytest.mark.parametrize(
    ("system", "expected"),
    [
        ([{"type": "text", "text": PREAMBLE}], 0),
        ([dict(BILLING_BLOCK), {"type": "text", "text": PREAMBLE}], 1),
        ([dict(BILLING_BLOCK), dict(BILLING_BLOCK), {"type": "text", "text": PREAMBLE}], 2),
    ],
)
def test_leading_identity_block_index_finds_the_head_block(system: Any, expected: int) -> None:
    assert leading_identity_block_index(system) == expected


@pytest.mark.parametrize(
    "text",
    [
        "x-anthropic-billing-header",  # marker incomplete: no colon
        f"prefix {BILLING_MARKER} v",  # not at the head
        "X-Anthropic-Billing-Header: v",  # case differs
        "",
    ],
)
def test_a_near_miss_marker_does_not_make_the_next_block_leading(text: str) -> None:
    system = [{"type": "text", "text": text}, {"type": "text", "text": PREAMBLE}]

    assert leading_identity_block_index(system) is None


# ---- why the carve-out is needed at all -----------------------------------


async def test_en_ner_finds_entities_inside_the_preamble() -> None:
    """Without the carve-out the preamble IS rewritten: spaCy tags `Claude` as
    PERSON and `CLI` as ORG, both of which the gateway redacts. Pinned here so
    the byte-identical assertions above stay meaningful — this is the defect the
    carve-out exists for, and it only reproduces where the NER extras are
    installed (CI's Python 3.12 job)."""
    pytest.importorskip("spacy")

    findings = await DualNerDetector().detect(PREAMBLE)
    assert findings, "expected the EN NER to match inside the identity preamble"
    assert all(PREAMBLE[f.start : f.end] == f.text for f in findings)
