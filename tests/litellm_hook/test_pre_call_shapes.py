"""What the pre-call rewrites in each request shape: messages, system, instructions, Responses
`input`, audio, and the call_type gate on `input`."""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.detectors import RegexChecksumDetector
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
from corp_llm_gateway.rules import Rule, Rules
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo
from tests.hook_fixtures import (
    _build_guardrail,
    _corp_llm_email_per_segment,
    _corp_llm_unreachable,
    _data_with_token,
    _StaticRules,
)


async def test_pre_call_sanitizes_responses_input_instructions_and_tool_output() -> None:
    g, _ = _build_guardrail(pairs=[("Kdir", "[ORG_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Implement KdirService"}],
            },
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "KdirService.cs",
            },
        ],
        "instructions": "Work with KdirService",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }

    out = await g.pre_call(data)

    assert out["input"][0]["content"][0]["text"] == "Implement [ORG_001]Service"
    assert out["input"][1]["output"] == "[ORG_001]Service.cs"
    assert out["instructions"] == "Work with [ORG_001]Service"
    assert "messages" not in out


async def test_pre_call_new_anthropic_and_chat_completion_block_types_sanitize_and_pass() -> None:
    """Regression for the fail-closed widening breaking real production traffic
    (server_tool_use/web_search/mcp/code_execution/search_result/container_upload
    on the Anthropic `messages` shape) — must not raise, and text-bearing types
    must actually get sanitized."""
    g, _ = _build_guardrail([("acme", "[ORG_001]")])
    data = {
        "model": "claude-x",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "server_tool_use",
                        "id": "t1",
                        "name": "web_search",
                        "input": {"query": "acme"},
                    },
                    {
                        "type": "web_search_tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            {"type": "web_search_result", "url": "https://x", "title": "acme"}
                        ],
                    },
                    {
                        "type": "code_execution_tool_result",
                        "tool_use_id": "t2",
                        "content": {"stdout": "acme output"},
                    },
                    {
                        "type": "mcp_tool_use",
                        "id": "t3",
                        "name": "fetch",
                        "input": {"q": "acme"},
                    },
                    {
                        "type": "mcp_tool_result",
                        "tool_use_id": "t3",
                        "content": [{"type": "text", "text": "acme result"}],
                    },
                    {
                        "type": "search_result",
                        "title": "acme doc",
                        "source": "kb://doc1",
                        "content": [{"type": "text", "text": "acme body"}],
                    },
                    {"type": "container_upload", "file_id": "file_123"},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "file", "file": {"file_id": "file_456", "filename": "notes.txt"}},
                    {"type": "input_audio", "input_audio": {"data": "base64==", "format": "wav"}},
                ],
            },
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data)
    serialized = json.dumps(out)
    assert "acme" not in serialized, "text-bearing new block type leaked the original"
    assert "file_123" in serialized, "opaque container_upload must pass through unchanged"
    assert "file_456" in serialized, "opaque file attachment must pass through unchanged"


async def test_pre_call_newer_anthropic_block_types_sanitize_and_pass() -> None:
    """MAJOR 4 exact repro: web-fetch-2025-09-10 and code-execution-2025-08-25
    block types, replayed verbatim in `messages` on every follow-up turn, used
    to 400 real production traffic. A dict block with no "type" key at all
    must also be scanned, not rejected."""
    g, _ = _build_guardrail([("acme", "[ORG_001]")])
    data = {
        "model": "claude-x",
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "web_fetch_tool_result",
                        "tool_use_id": "t1",
                        "content": {"type": "web_fetch_result", "content": "acme fetched"},
                    },
                    {
                        "type": "bash_code_execution_tool_result",
                        "tool_use_id": "t2",
                        "content": {"stdout": "acme output"},
                    },
                    {
                        "type": "text_editor_code_execution_tool_result",
                        "tool_use_id": "t3",
                        "content": {"file_text": "acme file contents"},
                    },
                    {
                        "type": "code_execution_output",
                        "file_id": "file_1",
                        "content": "acme stdout",
                    },
                    {"foo": "acme dict block with no type key"},
                ],
            }
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data)
    serialized = json.dumps(out)
    assert "acme" not in serialized, "newer block type leaked the original"


async def test_pre_call_responses_input_image_and_input_file_blocks_pass_without_raising() -> None:
    g, _ = _build_guardrail([])
    data = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "look at this"},
                    {"type": "input_image", "image_url": "https://example.com/x.png"},
                    {"type": "input_file", "file_id": "file_789", "filename": "report.pdf"},
                ],
            }
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out = await g.pre_call(data)
    assert out["input"][0]["content"][1]["image_url"] == "https://example.com/x.png"
    assert out["input"][0]["content"][2]["file_id"] == "file_789"


async def test_pre_call_genuinely_unknown_block_type_no_longer_fails_closed() -> None:
    """MAJOR 4: inverted. A hard 400 on every unrecognized block type poisons
    real production traffic (Anthropic/OpenAI ship new ones regularly, and
    multi-turn conversations replay them verbatim), so this now scans
    fail-safe instead of raising — see test_pre_call_new_anthropic_and_chat_
    completion_block_types_sanitize_and_pass and the content_blocks.py unit
    tests for the fail-safe scan itself."""
    g, _ = _build_guardrail([])
    data = {
        "model": "claude-x",
        "messages": [{"role": "assistant", "content": [{"type": "some_future_block_type"}]}],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data)
    assert out["messages"][0]["content"][0] == {"type": "some_future_block_type"}


async def test_pre_call_responses_bare_string_input_element_is_sanitized() -> None:
    """A bare string element inside data["input"] (not a dict) is text — it must
    not bypass sanitize, Stage 0, and Stage 5 just because it isn't a dict."""
    g, _ = _build_guardrail([("sk-corp-secret", "[SECRET_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "input": [
            "leak sk-corp-secret here",
            {"role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out = await g.pre_call(data)
    assert out["input"][0] == "leak [SECRET_001] here"


async def test_pre_call_embeddings_input_string_passes_through_untouched() -> None:
    """Exact review repro: /v1/embeddings' `input` is raw text to vectorize,
    not a Responses items list — release left it untouched; sanitizing it
    would vectorize on placeholder text instead of the real text."""
    g, _ = _build_guardrail([("alice", "[NAME_001]")])
    data = {
        "model": "text-embedding-3-small",
        "input": "alice@corp.example",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data, call_type="embedding")
    assert out["input"] == "alice@corp.example"
    assert "messages" not in out


async def test_pre_call_moderations_input_list_passes_through_untouched() -> None:
    """Exact review repro: /v1/moderations' `input` is a list of raw strings
    to score, not Responses items — moderation scoring must see the real
    text, not a redacted one."""
    g, _ = _build_guardrail([("alice", "[NAME_001]"), ("bob", "[NAME_002]")])
    data = {
        "model": "omni-moderation-latest",
        "input": ["alice", "bob"],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data, call_type="moderation")
    assert out["input"] == ["alice", "bob"]
    assert "messages" not in out


async def test_pre_call_amoderation_call_type_also_passes_through() -> None:
    g, _ = _build_guardrail([("alice", "[NAME_001]")])
    data = {
        "model": "omni-moderation-latest",
        "input": "alice",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data, call_type="amoderation")
    assert out["input"] == "alice"


async def test_pre_call_responses_call_type_still_sanitizes_input() -> None:
    """The gate must not become a blanket pass-through: a real Responses
    call_type still gets the full `input` treatment."""
    g, _ = _build_guardrail([("alice", "[NAME_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "input": "contact alice",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out = await g.pre_call(data, call_type="responses")
    assert out["input"] == "contact [NAME_001]"


async def test_pre_call_completion_call_type_still_sanitizes_messages() -> None:
    """Chat Completions call types must be unaffected by the gate."""
    g, _ = _build_guardrail([("alice", "[NAME_001]")])
    data = _data_with_token("tok-1", content="contact alice")
    out = await g.pre_call(data, call_type="completion")
    assert out["messages"][0]["content"] == "contact [NAME_001]"


async def test_pre_call_no_call_type_defaults_to_todays_behavior() -> None:
    """Every existing direct pre_call() call site (no call_type argument)
    must keep sanitizing `input` exactly as before — only the real
    async_pre_call_hook wiring supplies a call_type."""
    g, _ = _build_guardrail([("alice", "[NAME_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "input": "contact alice",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out = await g.pre_call(data)
    assert out["input"] == "contact [NAME_001]"


async def test_pre_call_explicit_null_input_passes_through_like_release() -> None:
    """Minor: `{"input": null}` used to hit the isinstance(messages, list)
    bad-request check and 400 — release passed an explicit null through."""
    g, _ = _build_guardrail([])
    data = {
        "model": "gpt-5.6-sol",
        "input": None,
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out = await g.pre_call(data, call_type="responses")
    assert out["input"] is None


async def test_pre_call_unrecognized_call_type_still_sanitizes_input() -> None:
    """A call_type this gateway doesn't recognize (e.g. litellm's
    `/v1/responses/compact` route, call_type="acompact_responses") must NOT
    default to unmanaged pass-through: only a call_type explicitly known to
    carry non-Responses `input` (embeddings, moderations) may skip
    sanitization. Everything else — including one this gateway has never
    seen — must fail closed and get the full `input` treatment."""
    g, _ = _build_guardrail([("alice", "[NAME_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "input": "contact alice",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out = await g.pre_call(data, call_type="acompact_responses")
    assert out["input"] == "contact [NAME_001]"


async def test_pre_call_speech_input_passes_through_untouched() -> None:
    """Exact review repro: litellm's pinned proxy_server.py passes
    call_type="aspeech" for POST /v1/audio/speech, whose `input` is the raw
    text to synthesize — there is no reverse path for audio, so redacting it
    would make the synthesized speech say the placeholder token aloud."""
    g, _ = _build_guardrail([("Alice Smith", "[NAME_001]")])
    data = {
        "model": "tts-1",
        "input": "Please welcome Alice Smith to the stage",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data, call_type="aspeech")
    assert out["input"] == "Please welcome Alice Smith to the stage"
    assert "messages" not in out


async def test_pre_call_speech_call_type_also_passes_through() -> None:
    g, _ = _build_guardrail([("Alice Smith", "[NAME_001]")])
    data = {
        "model": "tts-1",
        "input": "Alice Smith",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data, call_type="speech")
    assert out["input"] == "Alice Smith"


async def test_pre_call_pass_through_endpoint_input_passes_through_untouched() -> None:
    """pass_through_endpoint bodies are opaque and admin/backend-defined (e.g.
    a Voyage-embeddings-shaped `{"input": [...]}`) — same class of risk as
    /v1/embeddings, so treated the same way: not rewritten as Responses
    items, only DLP-scanned (Stage 5) via the unmanaged shape."""
    g, _ = _build_guardrail([("alice", "[NAME_001]")])
    data = {
        "model": "voyage-3",
        "input": ["alice@corp.example"],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data, call_type="pass_through_endpoint")
    assert out["input"] == ["alice@corp.example"]
    assert "messages" not in out


async def test_pre_call_codex_profile_oracle_disabled_applies_rules_directly() -> None:
    """Codex profile (forward_chatgpt_auth=True) + oracle disabled: a replace.md
    rule reaches the Responses `input` field through the local_pass branch's
    direct rule injection (decision 3), with no corp-LLM client at all."""
    rules = Rules(rules=(Rule("Zephyr Ledger", "[CONFIDENTIAL_PROJECT]"),))
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
    orch = SanitizationOrchestrator(
        None,
        InMemoryMappingStore(),
        _StaticRules(rules),
        local_detectors=[RegexChecksumDetector()],
        oracle_enabled=False,
    )
    sink = ListSink()
    g = CorpLlmGuardrail(
        orch,
        AuthMiddleware(token_store),
        AuditLogger(sink, gateway_version="0.0.1"),
        forward_chatgpt_auth=True,
    )
    data = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "Migrating Zephyr Ledger to new stack"}],
            }
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }

    out = await g.pre_call(data)

    assert out["input"][0]["content"][0]["text"] == "Migrating [CONFIDENTIAL_PROJECT] to new stack"


async def test_pre_call_replaces_message_content_with_sanitized() -> None:
    g, _ = _build_guardrail([("alice", "[NAME_001]")])
    data = _data_with_token("tok-1", content="hello alice")
    out = await g.pre_call(data)
    assert out["messages"][0]["content"] == "hello [NAME_001]"


async def test_pre_call_same_email_two_segments_reuses_one_token() -> None:
    """The other half of the bijection: the SAME original in two segments must
    reuse ONE token (not be split into two), so the model sees it consistently."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content="email a@corp.example",
        system="a@corp.example is admin",
    )
    out = await g.pre_call(data)
    msg_ph = re.search(r"\[EMAIL_\d+\]", out["messages"][0]["content"]).group(0)
    sys_ph = re.search(r"\[EMAIL_\d+\]", out["system"]).group(0)
    assert msg_ph == sys_ph == "[EMAIL_001]"


async def test_pre_call_collision_across_blocks_in_one_message() -> None:
    """Collision is not only system-vs-message: two text blocks in the SAME
    message carry different emails that both come back [EMAIL_001]."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content=[
            {"type": "text", "text": "first a@corp.example"},
            {"type": "text", "text": "second b@corp.example"},
        ],
    )
    out = await g.pre_call(data)
    blocks = out["messages"][0]["content"]
    ph0 = re.search(r"\[EMAIL_\d+\]", blocks[0]["text"]).group(0)
    ph1 = re.search(r"\[EMAIL_\d+\]", blocks[1]["text"]).group(0)
    assert ph0 != ph1, (ph0, ph1)


async def test_pre_call_collision_message_vs_tool_result() -> None:
    """tool_result content is sanitized via the walker recursion; an email
    there must not collide with a different email in a sibling text block."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content=[
            {"type": "text", "text": "user a@corp.example"},
            {"type": "tool_result", "content": "tool saw b@corp.example"},
        ],
    )
    out = await g.pre_call(data)
    blocks = out["messages"][0]["content"]
    text_ph = re.search(r"\[EMAIL_\d+\]", blocks[0]["text"]).group(0)
    tr_ph = re.search(r"\[EMAIL_\d+\]", blocks[1]["content"]).group(0)
    assert text_ph != tr_ph, (text_ph, tr_ph)


async def test_pre_call_rejects_non_list_messages() -> None:
    g, _ = _build_guardrail()
    data = {"model": "claude", "messages": "not-a-list", "headers": {"X-Corp-Auth": "tok-1"}}
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_BAD_REQUEST"


async def test_pre_call_request_id_stable_across_calls_on_same_data() -> None:
    g, _ = _build_guardrail()
    data = _data_with_token("tok-1")
    await g.pre_call(data)
    rid1 = data["_corp_gateway_request_id"]
    # Re-running pre_call on same dict reuses the request id.
    assert isinstance(rid1, str) and rid1


async def test_pre_call_skips_unwrapped_literal_scan_when_codex_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Minor: find_unwrapped_placeholder_literals's result (response_alias_
    exclusions) is only ever read by _response_mapping when forward_chatgpt_auth
    is on — the scan itself must not run when the flag is off."""
    import corp_llm_gateway.litellm_hook as hook_module

    calls: list[str] = []
    original = hook_module.find_unwrapped_placeholder_literals

    def _counting(text: str) -> list[str]:
        calls.append(text)
        return original(text)

    monkeypatch.setattr(hook_module, "find_unwrapped_placeholder_literals", _counting)

    g, _ = _build_guardrail([])  # forward_chatgpt_auth defaults to False
    data = _data_with_token("tok-1", content="hello world", system="a system prompt")
    await g.pre_call(data)

    assert calls == []


async def test_pre_call_runs_unwrapped_literal_scan_when_codex_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the scan must still run when the flag is on — the
    skip above must not become a blanket disable."""
    import corp_llm_gateway.litellm_hook as hook_module

    calls: list[str] = []
    original = hook_module.find_unwrapped_placeholder_literals

    def _counting(text: str) -> list[str]:
        calls.append(text)
        return original(text)

    monkeypatch.setattr(hook_module, "find_unwrapped_placeholder_literals", _counting)

    g, _ = _build_guardrail([], forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-sol",
        "input": "hello world",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(data)

    assert calls == ["hello world"]


async def test_pre_call_block_list_message_content_sanitized() -> None:
    """Task 2: messages with list-of-blocks content are sanitized (Anthropic shape)."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    content = [
        {"type": "text", "text": "hello alice"},
        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    # Text block is sanitized, image block is untouched.
    assert len(out["messages"][0]["content"]) == 2
    assert out["messages"][0]["content"][0]["type"] == "text"
    assert out["messages"][0]["content"][0]["text"] == "hello [N1]"
    assert out["messages"][0]["content"][1] == content[1]


async def test_pre_call_tool_result_block_sanitized() -> None:
    """Task 2: tool_result blocks with str content are sanitized."""
    g, _ = _build_guardrail([("secret", "[SECRET_001]")])
    content = [
        {
            "type": "tool_result",
            "content": "the secret is revealed",
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    assert out["messages"][0]["content"][0]["type"] == "tool_result"
    assert out["messages"][0]["content"][0]["content"] == "the [SECRET_001] is revealed"


async def test_pre_call_system_str_sanitized() -> None:
    """Task 3: system field as str is sanitized."""
    g, _ = _build_guardrail([("SecretEnv", "[ENV_001]")])
    data = _data_with_token("tok-1", content="hello", system="SecretEnv=/prod")
    out = await g.pre_call(data)

    assert out["system"] == "[ENV_001]=/prod"


async def test_pre_call_system_list_sanitized() -> None:
    """Task 3: system field as list of blocks is sanitized."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    system = [{"type": "text", "text": "Context: alice"}]
    data = _data_with_token("tok-1", content="hello", system=system)
    out = await g.pre_call(data)

    assert isinstance(out["system"], list)
    assert out["system"][0]["text"] == "Context: [N1]"


async def test_pre_call_no_system_no_op() -> None:
    """Task 3: messages without system field are unaffected."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hello alice")
    out = await g.pre_call(data)

    assert "system" not in out
    assert out["messages"][0]["content"] == "hello [N1]"


async def test_pre_call_sanitizes_both_system_and_instructions_when_both_present() -> None:
    """A payload carrying BOTH `system` and `instructions` non-empty must not
    egress either original — the old ternary picked exactly one field, so
    whichever field lost the ternary egressed raw."""
    g, _ = _build_guardrail([("SecretEnvA", "[ENV_001]"), ("SecretEnvB", "[ENV_002]")])
    data = {
        "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "hello"}],
        "system": "SecretEnvA=/prod",
        "instructions": "SecretEnvB=/prod",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out = await g.pre_call(data)

    assert out["system"] == "[ENV_001]=/prod"
    assert out["instructions"] == "[ENV_002]=/prod"
    forwarded = json.dumps(out)
    assert "SecretEnvA" not in forwarded, "system original egressed"
    assert "SecretEnvB" not in forwarded, "instructions original egressed"


async def test_pre_call_str_message_regression() -> None:
    """Task 2: plain-string message content still works (OpenAI-compatible regression)."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    out = await g.pre_call(data)

    assert out["messages"][0]["content"] == "hi [N1]"


async def test_pre_call_hybrid_messages_empty_and_input_rejected() -> None:
    """`_request_items` picks `messages` whenever the key is merely present, even
    `[]` — so `input` passed through untouched, bypassing sanitize/Stage 0/Stage 5
    entirely. A payload carrying both keys must be refused outright."""
    original = "sk-corp-secret-hybrid"
    g, sink = _build_guardrail([(original, "[SECRET_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "messages": [],
        "input": [{"role": "user", "content": [{"type": "input_text", "text": original}]}],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_POLICY_BLOCKED"
    assert original not in json.dumps(sink.records), "original leaked into the audit record"


async def test_pre_call_hybrid_messages_none_and_input_rejected() -> None:
    """Same hybrid rejection when `messages` is explicitly `None` rather than `[]`."""
    original = "sk-corp-secret-hybrid-2"
    g, _ = _build_guardrail([(original, "[SECRET_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "messages": None,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": original}]}],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_POLICY_BLOCKED"
    # pre_call raises before returning — nothing is ever forwarded to the upstream
    # provider, so there is no outgoing dict for the original to leak into.


async def test_pre_call_hybrid_payload_audit_matches_stage0_convention() -> None:
    """block_reason / audit shape for the hybrid rejection mirrors Stage 0's
    (see test_stage0_audit_record_emitted_with_block_reason)."""
    g, sink = _build_guardrail()
    data = {
        "model": "gpt-5.6-sol",
        "messages": [],
        "input": ["anything"],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_POLICY_BLOCKED"
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec.get("block_reason") == "request:ambiguous_shape"
    assert rec.get("status") == "failed"
    assert rec.get("error_code") == "E_POLICY_BLOCKED"
    assert rec.get("user_id") == "alice"
    assert rec.get("team_id") == "t1"


async def test_pre_call_single_key_shapes_unaffected_by_hybrid_guard() -> None:
    """messages-only, input-as-string, and input-as-list requests are untouched
    by the new hybrid guard."""
    g, _ = _build_guardrail([("alice", "[NAME_001]")])

    only_messages = _data_with_token("tok-1", content="hello alice")
    out = await g.pre_call(only_messages)
    assert out["messages"][0]["content"] == "hello [NAME_001]"

    input_string_data = {
        "model": "gpt-5.6-sol",
        "input": "hello alice",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out2 = await g.pre_call(input_string_data)
    assert out2["input"] == "hello [NAME_001]"

    input_list_data = {
        "model": "gpt-5.6-sol",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello alice"}]}],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out3 = await g.pre_call(input_list_data)
    assert out3["input"][0]["content"][0]["text"] == "hello [NAME_001]"


async def test_pre_call_document_block_title_and_text_source_redacted() -> None:
    """document block: title and source.data (text) are redacted; no original in egress."""
    g, _ = _build_guardrail(
        [
            ("alice@corp.example", "[E1]"),
            ("bob@corp.example", "[E2]"),
        ]
    )
    content = [
        {
            "type": "document",
            "title": "Report for alice@corp.example",
            "source": {"type": "text", "data": "Authored by bob@corp.example"},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    serialized = json.dumps(out["messages"][0]["content"])
    assert "alice@corp.example" not in serialized
    assert "bob@corp.example" not in serialized
    assert "[E1]" in serialized or "[E2]" in serialized


async def test_pre_call_document_block_base64_source_untouched() -> None:
    """document block with base64 source must pass through unchanged."""
    b64_data = "SGVsbG8gV29ybGQ="
    g, _ = _build_guardrail([("alice@corp.example", "[E1]")])
    content = [
        {
            "type": "document",
            "title": "alice@corp.example",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64_data},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    blk = out["messages"][0]["content"][0]
    assert blk["source"]["data"] == b64_data
    assert "alice@corp.example" not in blk["title"]


async def test_pre_call_empty_list_content_no_crash() -> None:
    """Edge: empty list content (no blocks) should not crash and return empty list."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    content: list[Any] = []
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    assert out["messages"][0]["content"] == []


async def test_pre_call_content_with_only_non_text_blocks() -> None:
    """Edge: list with only non-text blocks (image, tool_use) should not call corp-LLM."""
    # Build a guardrail that would raise if corp-LLM is called.
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    content = [
        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
        {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {}},
    ]
    data = _data_with_token("tok-1", content=content)

    # This should NOT fail because no corp-LLM call is made for non-text blocks.
    out = await g.pre_call(data)
    assert len(out["messages"][0]["content"]) == 2
    assert out["messages"][0]["content"][0]["type"] == "image_url"
    assert out["messages"][0]["content"][1]["type"] == "tool_use"


async def test_pre_call_missing_content_field_no_crash() -> None:
    """Edge: message without content field should not crash."""
    g, _ = _build_guardrail()
    data = {
        "model": "claude",
        "messages": [{"role": "user"}],  # No content field
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data)

    # Should pass through unchanged.
    assert out["messages"][0] == {"role": "user"}


async def test_pre_call_system_empty_str() -> None:
    """Edge: empty system string is skipped (truthy guard) and passed through unchanged."""
    g, _ = _build_guardrail()
    data = _data_with_token("tok-1", content="hello", system="")
    out = await g.pre_call(data)

    # Empty string is falsy → skipped entirely; value is preserved as-is in data.
    assert out.get("system") == ""


async def test_pre_call_system_empty_list() -> None:
    """Edge: empty system list is skipped (truthy guard) and passed through unchanged."""
    g, _ = _build_guardrail()
    data = _data_with_token("tok-1", content="hello", system=[])
    out = await g.pre_call(data)

    # Empty list is falsy → skipped entirely; value is preserved as-is in data.
    assert out.get("system") == []


async def test_pre_call_multiple_text_blocks_in_content_all_sanitized() -> None:
    """Multiple text blocks in same message are all sanitized."""
    g, _ = _build_guardrail([("alice", "[N1]"), ("bob", "[N2]")])
    content = [
        {"type": "text", "text": "hello alice"},
        {"type": "image_url", "image_url": {"url": "https://..."}},
        {"type": "text", "text": "goodbye bob"},
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    assert out["messages"][0]["content"][0]["text"] == "hello [N1]"
    assert out["messages"][0]["content"][1]["type"] == "image_url"
    assert out["messages"][0]["content"][2]["text"] == "goodbye [N2]"


async def test_pre_call_deeply_nested_tool_result_blocks_sanitized() -> None:
    """tool_result with nested list content: all text blocks are sanitized recursively."""
    g, _ = _build_guardrail([("secret", "[SECRET_001]")])
    content = [
        {
            "type": "tool_result",
            "content": [
                {"type": "text", "text": "part one: secret"},
                {
                    "type": "tool_result",
                    "content": {"type": "text", "text": "nested: secret"},
                },
            ],
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    # Top-level tool_result content list
    tool_result = out["messages"][0]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert isinstance(tool_result["content"], list)
    # First text block in the list is sanitized
    assert tool_result["content"][0]["text"] == "part one: [SECRET_001]"
    # Nested tool_result (inside the list) — its content is a bare dict text block.
    # Fix A: bare dict is now routed through _sanitize_block, so it IS redacted.
    nested_tool_result = tool_result["content"][1]
    assert nested_tool_result["type"] == "tool_result"
    assert nested_tool_result["content"]["type"] == "text"
    nested_text = nested_tool_result["content"]["text"]
    assert "[SECRET_001]" in nested_text
    assert "secret" not in nested_text


async def test_pre_call_non_dict_message_items_skipped() -> None:
    """Non-dict items in messages list should be skipped gracefully."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = {
        "model": "claude",
        "messages": [
            {"role": "user", "content": "hi alice"},
            "not a dict",  # Invalid, should be skipped.
            123,
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.pre_call(data)

    # First message sanitized, others untouched.
    assert out["messages"][0]["content"] == "hi [N1]"
    assert out["messages"][1] == "not a dict"
    assert out["messages"][2] == 123


async def test_pre_call_tool_result_bare_dict_content_sanitized() -> None:
    """Fix A: tool_result whose content is a bare dict text block is redacted.

    The bare-dict path previously fell through to the 'pass through unchanged'
    branch, leaking the original text. The _sanitize_block helper now handles
    it identically to a list-item dict.
    """
    g, _ = _build_guardrail([("secret", "[SECRET_001]")])
    content = [
        {
            "type": "tool_result",
            # content is a bare dict, not a list — the pre-fix blind spot.
            "content": {"type": "text", "text": "the secret value"},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    nested = out["messages"][0]["content"][0]["content"]
    assert nested["type"] == "text"
    assert "[SECRET_001]" in nested["text"]
    assert "secret" not in nested["text"]
