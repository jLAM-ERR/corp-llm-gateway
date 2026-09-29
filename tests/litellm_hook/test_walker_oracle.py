"""Plan 20260926 Task 4: litellm's guardrail translation handlers as an oracle for our walker.

litellm 1.101.0 ships a per-endpoint handler (``llms/*/guardrail_translation/handler.py``)
that pulls the text out of a request and hands it to a guardrail's ``apply_guardrail``;
``unified_guardrail.py:185-191`` picks the handler by call type. We never adopt that path
(hazard 7), but it is an independent reading of the same wire formats. Every leaf it
exposes must be one our pre-call rewrites: ``litellm ⊆ ours``.

Each fixture carries a distinct canary in every text-capable position, so the sets are
computed by canary membership, not by path guessing:

- litellm, rewrite tier: canaries in ``texts`` (and chat ``tool_calls``), the leaves the
  handler maps back one by one. Pinned exactly per fixture and a subset of ours, no
  exceptions.
- litellm, handed tier: canaries anywhere in what ``apply_guardrail`` receives
  (``structured_messages``, ``tools``, ``images`` too). ``structured_messages`` and
  ``tools`` are not only read: when a guardrail returns them the handler writes them back
  over the request wholesale (chat ``handler.py:165-190``, anthropic ``:480-520``,
  responses ``:382-391``), so every leaf in them is one a guardrail on that path could
  rewrite. Must be a subset of ours except the carve-outs in ``CARVE_OUTS``, each a
  documented row.
- ours: canaries gone from the request after ``_pre_call_impl``, with a placeholder in
  their place.

The litellm-only and ours-only sets are pinned per fixture. Changing an expected set is
the assertion evolving with a walker or a litellm change, never a way to pass: a new
litellm-only leaf is a gap to close in the walker or a carve-out to document here and in
``docs/security.md``; a lost ours-only leaf is a walker regression.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest

pytest.importorskip("litellm.proxy.proxy_server", reason="litellm proxy not installed")

import litellm
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.llms import load_guardrail_translation_mappings
from litellm.types.utils import CallTypes

from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
from tests.test_litellm_hook import _build_guardrail

CANARY = re.compile(r"CNRY_[A-Za-z0-9_]+")
PLACEHOLDER = re.compile(r"\[CANARY_\d{3}\]")
TOKEN = "tok-oracle"

# Leaves litellm hands a guardrail that we do not rewrite. Each is a row of
# docs/security.md §"Not sanitized / deferred".
_TOOLS = '`data["tools"]` row: declarations untouched on all three routes, matched by name'
_IMAGE = "`image` / `image_url` / `input_image` row: binary payload or a low-risk URL"
_FILE = "`input_file` / `file` row: attachment reference, filenames are not scanned"
_AUDIO = "`input_audio` / `output_audio` row: base64 audio"
_THINKING = "`thinking` row: Anthropic signs the block, it must replay byte-identical"
_NAME = "`messages[].name` row: participant name, not rewritten — open, follow-up"
CARVE_OUTS: dict[str, str] = {
    "CNRY_a_tool_desc": _TOOLS,
    "CNRY_a_tool_schema": _TOOLS,
    "CNRY_a_tool_input_example": _TOOLS,
    "CNRY_a_image_url": _IMAGE,
    "CNRY_a_tool_result_image": _IMAGE,
    "CNRY_a_thinking": _THINKING,
    "CNRY_c_tool_desc": _TOOLS,
    "CNRY_c_tool_schema": _TOOLS,
    "CNRY_c_image_url": _IMAGE,
    "CNRY_c_file_name": _FILE,
    "CNRY_c_input_audio": _AUDIO,
    "CNRY_c_user_name": _NAME,
    "CNRY_c_assistant_name": _NAME,
    "CNRY_r_tool_desc": _TOOLS,
    "CNRY_r_image_url": _IMAGE,
    "CNRY_r_file_name": _FILE,
}


def _args(**kwargs: Any) -> str:
    return json.dumps(kwargs)


ANTHROPIC: dict[str, Any] = {
    "model": "claude-sonnet-4-5",
    "max_tokens": 64,
    "system": [
        {"type": "text", "text": "CNRY_a_system_block_1"},
        {"type": "text", "text": "CNRY_a_system_block_2", "cache_control": {"type": "ephemeral"}},
    ],
    "messages": [
        {"role": "user", "content": "CNRY_a_user_str"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "CNRY_a_user_text"},
                {
                    "type": "text",
                    "text": "CNRY_a_cited_text",
                    "citations": [
                        {
                            "type": "char_location",
                            "cited_text": "CNRY_a_citation_cited",
                            "document_title": "CNRY_a_citation_title",
                            "document_index": 0,
                            "start_char_index": 0,
                            "end_char_index": 4,
                        }
                    ],
                },
                {
                    "type": "document",
                    "source": {
                        "type": "text",
                        "media_type": "text/plain",
                        "data": "CNRY_a_document_text",
                    },
                    "title": "CNRY_a_document_title",
                    "context": "CNRY_a_document_context",
                },
                {
                    "type": "document",
                    "source": {
                        "type": "content",
                        "content": [{"type": "text", "text": "CNRY_a_document_content"}],
                    },
                },
                {"type": "image", "source": {"type": "url", "url": "https://x/CNRY_a_image_url"}},
                {
                    "type": "search_result",
                    "source": "https://x/doc",
                    "title": "CNRY_a_search_title",
                    "content": [{"type": "text", "text": "CNRY_a_search_content"}],
                },
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "CNRY_a_thinking", "signature": "sig"},
                {"type": "redacted_thinking", "data": "opaque"},
                {"type": "text", "text": "CNRY_a_assistant_text"},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "lookup",
                    "input": {"q": "CNRY_a_tool_use_input", "nested": ["CNRY_a_tool_use_nested"]},
                },
                {
                    "type": "server_tool_use",
                    "id": "srvtoolu_1",
                    "name": "web_search",
                    "input": {"query": "CNRY_a_server_tool_query"},
                },
                {
                    "type": "mcp_tool_use",
                    "id": "mcptoolu_1",
                    "name": "fetch",
                    "server_name": "srv",
                    "input": {"q": "CNRY_a_mcp_tool_input"},
                },
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "srvtoolu_1",
                    "content": [
                        {
                            "type": "web_search_result",
                            "url": "https://x/r",
                            "title": "CNRY_a_web_result_title",
                            "encrypted_content": "opaque",
                        }
                    ],
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "CNRY_a_tool_result_str"},
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_1",
                    "is_error": True,
                    "content": [
                        {"type": "text", "text": "CNRY_a_tool_result_block"},
                        {
                            "type": "image",
                            "source": {"type": "url", "url": "https://x/CNRY_a_tool_result_image"},
                        },
                    ],
                },
                {
                    "type": "mcp_tool_result",
                    "tool_use_id": "mcptoolu_1",
                    "content": [{"type": "text", "text": "CNRY_a_mcp_tool_result"}],
                },
            ],
        },
        {"role": "assistant", "content": "CNRY_a_assistant_str"},
    ],
    "tools": [
        {
            "name": "lookup",
            "description": "CNRY_a_tool_desc",
            "input_schema": {
                "type": "object",
                "properties": {"q": {"type": "string", "description": "CNRY_a_tool_schema"}},
            },
            "input_examples": [{"q": "CNRY_a_tool_input_example"}],
        }
    ],
}

ANTHROPIC_SYSTEM_STR: dict[str, Any] = {
    "model": "claude-sonnet-4-5",
    "max_tokens": 64,
    "system": "CNRY_as_system_str",
    "messages": [{"role": "user", "content": "CNRY_as_user_str"}],
}

CHAT: dict[str, Any] = {
    "model": "gpt-4o-mini",
    "messages": [
        {"role": "system", "content": "CNRY_c_system_str"},
        {"role": "system", "content": [{"type": "text", "text": "CNRY_c_system_part"}]},
        {"role": "developer", "content": "CNRY_c_developer_str"},
        {"role": "user", "name": "CNRY_c_user_name", "content": "CNRY_c_user_str"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "CNRY_c_user_part"},
                {"type": "image_url", "image_url": {"url": "https://x/CNRY_c_image_url"}},
                {"type": "file", "file": {"filename": "CNRY_c_file_name", "file_id": "file_1"}},
                {
                    "type": "input_audio",
                    "input_audio": {"data": "CNRY_c_input_audio", "format": "wav"},
                },
            ],
        },
        {
            "role": "assistant",
            "name": "CNRY_c_assistant_name",
            "content": "CNRY_c_assistant_str",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": _args(q="CNRY_c_tool_call_args")},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "CNRY_c_tool_str"},
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": [{"type": "text", "text": "CNRY_c_tool_part"}],
        },
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "CNRY_c_assistant_part"}],
        },
        {
            "role": "assistant",
            "content": None,
            "function_call": {"name": "lookup", "arguments": _args(q="CNRY_c_legacy_fn_args")},
        },
        {"role": "function", "name": "lookup", "content": "CNRY_c_legacy_fn_result"},
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "CNRY_c_tool_desc",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string", "description": "CNRY_c_tool_schema"}},
                },
            },
        }
    ],
    "functions": [{"name": "lookup", "description": "CNRY_c_legacy_fn_desc"}],
    "prediction": {"type": "content", "content": "CNRY_c_prediction"},
}

# The stream flag changes nothing on the request side: same leaves as the unary twin.
CHAT_STREAM_CONTROL: dict[str, Any] = {
    "model": "gpt-4o-mini",
    "stream": True,
    "stream_options": {"include_usage": True},
    "messages": [
        {"role": "system", "content": "CNRY_cs_system_str"},
        {"role": "user", "content": [{"type": "text", "text": "CNRY_cs_user_part"}]},
    ],
}
CHAT_UNARY_TWIN: dict[str, Any] = {
    **{k: v for k, v in CHAT_STREAM_CONTROL.items() if k not in ("stream", "stream_options")},
    "messages": [
        {"role": "system", "content": "CNRY_cu_system_str"},
        {"role": "user", "content": [{"type": "text", "text": "CNRY_cu_user_part"}]},
    ],
}

RESPONSES: dict[str, Any] = {
    "model": "gpt-4o-mini",
    "instructions": "CNRY_r_instructions",
    "prompt": {"id": "pmpt_1", "variables": {"city": "CNRY_r_prompt_variable"}},
    "input": [
        {"role": "user", "content": "CNRY_r_message_str"},
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "CNRY_r_input_text"},
                {"type": "input_image", "image_url": "https://x/CNRY_r_image_url"},
                {"type": "input_file", "filename": "CNRY_r_file_name", "file_id": "file_1"},
            ],
        },
        {"role": "developer", "content": [{"type": "input_text", "text": "CNRY_r_developer"}]},
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [{"type": "summary_text", "text": "CNRY_r_reasoning_summary"}],
            "content": [{"type": "reasoning_text", "text": "CNRY_r_reasoning_text"}],
            "encrypted_content": "opaque",
        },
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": _args(q="CNRY_r_function_call_args"),
        },
        {"type": "function_call_output", "call_id": "call_1", "output": "CNRY_r_fc_output_str"},
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": [{"type": "input_text", "text": "CNRY_r_fc_output_part"}],
        },
        {
            "type": "custom_tool_call",
            "call_id": "call_2",
            "name": "shell",
            "input": "CNRY_r_custom_tool_input",
        },
        {"type": "custom_tool_call_output", "call_id": "call_2", "output": "CNRY_r_custom_output"},
        {
            "type": "mcp_call",
            "id": "mcp_1",
            "server_label": "srv",
            "name": "fetch",
            "arguments": _args(q="CNRY_r_mcp_args"),
            "output": "CNRY_r_mcp_output",
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "CNRY_r_assistant_output_text"}],
        },
    ],
    "tools": [
        {
            "type": "function",
            "name": "lookup",
            "description": "CNRY_r_tool_desc",
            "parameters": {"type": "object"},
        }
    ],
}

RESPONSES_INPUT_STR: dict[str, Any] = {
    "model": "gpt-4o-mini",
    "input": "CNRY_rs_input_str",
    "instructions": "CNRY_rs_instructions",
}

FIXTURES: dict[str, tuple[CallTypes, str, dict[str, Any]]] = {
    "anthropic": (CallTypes.anthropic_messages, "anthropic_messages", ANTHROPIC),
    "anthropic-system-str": (
        CallTypes.anthropic_messages,
        "anthropic_messages",
        ANTHROPIC_SYSTEM_STR,
    ),
    "chat": (CallTypes.acompletion, "acompletion", CHAT),
    "chat-stream-control": (CallTypes.acompletion, "acompletion", CHAT_STREAM_CONTROL),
    "chat-unary-twin": (CallTypes.acompletion, "acompletion", CHAT_UNARY_TWIN),
    "responses": (CallTypes.aresponses, "aresponses", RESPONSES),
    "responses-input-str": (CallTypes.aresponses, "aresponses", RESPONSES_INPUT_STR),
}

# Measured on litellm 1.101.0: the leaves each handler maps back one by one.
EXPECTED_REWRITE: dict[str, set[str]] = {
    "anthropic": {
        "CNRY_a_assistant_str",
        "CNRY_a_assistant_text",
        "CNRY_a_cited_text",
        "CNRY_a_tool_result_block",
        "CNRY_a_tool_result_str",
        "CNRY_a_user_str",
        "CNRY_a_user_text",
    },
    "anthropic-system-str": {"CNRY_as_user_str"},
    "chat": {
        "CNRY_c_assistant_part",
        "CNRY_c_assistant_str",
        "CNRY_c_developer_str",
        "CNRY_c_legacy_fn_result",
        "CNRY_c_system_part",
        "CNRY_c_system_str",
        "CNRY_c_tool_call_args",
        "CNRY_c_tool_part",
        "CNRY_c_tool_str",
        "CNRY_c_user_part",
        "CNRY_c_user_str",
    },
    "chat-stream-control": {"CNRY_cs_system_str", "CNRY_cs_user_part"},
    "chat-unary-twin": {"CNRY_cu_system_str", "CNRY_cu_user_part"},
    "responses": {
        "CNRY_r_assistant_output_text",
        "CNRY_r_developer",
        "CNRY_r_input_text",
        "CNRY_r_message_str",
        "CNRY_r_reasoning_text",
    },
    "responses-input-str": {"CNRY_rs_input_str"},
}
# Every entry is a CARVE_OUTS key.
EXPECTED_LITELLM_ONLY: dict[str, set[str]] = {
    "anthropic": {
        "CNRY_a_image_url",
        "CNRY_a_thinking",
        "CNRY_a_tool_desc",
        "CNRY_a_tool_input_example",
        "CNRY_a_tool_result_image",
        "CNRY_a_tool_schema",
    },
    "anthropic-system-str": set(),
    "chat": {
        "CNRY_c_assistant_name",
        "CNRY_c_file_name",
        "CNRY_c_image_url",
        "CNRY_c_input_audio",
        "CNRY_c_tool_desc",
        "CNRY_c_tool_schema",
        "CNRY_c_user_name",
    },
    "chat-stream-control": set(),
    "chat-unary-twin": set(),
    "responses": {"CNRY_r_file_name", "CNRY_r_image_url", "CNRY_r_tool_desc"},
    "responses-input-str": set(),
}
# Leaves litellm's handlers drop on the floor (not even in `structured_messages`) and we
# rewrite: document blocks, search results, server and MCP tool input and results, MCP
# calls, reasoning summaries.
EXPECTED_OURS_ONLY: dict[str, set[str]] = {
    "anthropic": {
        "CNRY_a_document_content",
        "CNRY_a_document_context",
        "CNRY_a_document_text",
        "CNRY_a_document_title",
        "CNRY_a_mcp_tool_input",
        "CNRY_a_mcp_tool_result",
        "CNRY_a_search_content",
        "CNRY_a_search_title",
        "CNRY_a_server_tool_query",
        "CNRY_a_web_result_title",
    },
    "anthropic-system-str": set(),
    "chat": set(),
    "chat-stream-control": set(),
    "chat-unary-twin": set(),
    "responses": {"CNRY_r_mcp_args", "CNRY_r_mcp_output", "CNRY_r_reasoning_summary"},
    "responses-input-str": set(),
}
# In neither walker. Legacy chat `functions` are tool declarations (the `data["tools"]`
# row). The rest are open findings, not carve-outs — free text that reaches the provider
# as the client sent it, each an "open — follow-up" row in docs/security.md: a text
# block's `citations`, chat `prediction.content` and Responses `prompt.variables`.
EXPECTED_NEITHER: dict[str, set[str]] = {
    "anthropic": {"CNRY_a_citation_cited", "CNRY_a_citation_title"},
    "anthropic-system-str": set(),
    "chat": {"CNRY_c_legacy_fn_desc", "CNRY_c_prediction"},
    "chat-stream-control": set(),
    "chat-unary-twin": set(),
    "responses": {"CNRY_r_prompt_variable"},
    "responses-input-str": set(),
}


def canaries(value: Any) -> set[str]:
    return set(CANARY.findall(json.dumps(value, default=str)))


class OracleRecorder(CustomGuardrail):
    """Test-only: records what litellm hands a guardrail, returns it unchanged. Never a
    callback, never related to ``CorpLlmGuardrail``."""

    def __init__(self) -> None:
        super().__init__(guardrail_name="walker-oracle-recorder")
        # The widest scope litellm offers: nothing skipped.
        self.skip_system_message_in_guardrail = False
        self.skip_tool_message_in_guardrail = False
        self.calls: list[dict[str, Any]] = []

    async def apply_guardrail(
        self, inputs: Any, request_data: dict[str, Any], input_type: Any, logging_obj: Any = None
    ) -> Any:
        self.calls.append(copy.deepcopy(dict(inputs)))
        return inputs


async def litellm_leaves(call_type: CallTypes, body: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(rewrite tier, handed tier) for ``body``, driven as ``unified_guardrail`` does."""
    recorder = OracleRecorder()
    handler = load_guardrail_translation_mappings()[call_type]()
    await handler.process_input_messages(data=copy.deepcopy(body), guardrail_to_apply=recorder)
    assert recorder not in litellm.callbacks
    assert len(recorder.calls) == 1
    (inputs,) = recorder.calls
    rewrite = canaries(inputs.get("texts")) | canaries(inputs.get("tool_calls"))
    return rewrite, canaries(inputs)


async def our_leaves(call_type: str, body: dict[str, Any]) -> set[str]:
    """Canaries our pre-call replaced with a placeholder."""
    present = sorted(canaries(body))
    guardrail, _ = _build_guardrail(
        [(c, f"[CANARY_{i:03d}]") for i, c in enumerate(present, 1)], valid_token=TOKEN
    )
    data = copy.deepcopy(body)
    data["headers"] = {"X-Corp-Auth": TOKEN}
    out = await guardrail._pre_call_impl(data, call_type=call_type)
    sent = {key: out.get(key) for key in body}
    assert set(out) >= set(body)
    left = canaries(sent)
    rewritten = set(present) - left
    assert len(PLACEHOLDER.findall(json.dumps(sent))) >= len(rewritten)
    return rewritten


def test_every_expectation_names_exactly_the_fixtures() -> None:
    for expected in (EXPECTED_REWRITE, EXPECTED_LITELLM_ONLY, EXPECTED_OURS_ONLY, EXPECTED_NEITHER):
        assert set(expected) == set(FIXTURES)


def test_the_recorder_is_not_our_guardrail() -> None:
    assert not issubclass(OracleRecorder, CorpLlmGuardrail)
    assert not hasattr(CorpLlmGuardrail, "apply_guardrail")


@pytest.mark.parametrize("name", sorted(FIXTURES))
async def test_every_leaf_litellm_exposes_is_one_we_rewrite(name: str) -> None:
    litellm_type, our_type, body = FIXTURES[name]
    rewrite, handed = await litellm_leaves(litellm_type, body)
    ours = await our_leaves(our_type, body)
    litellm_only = handed - ours
    ours_only = ours - handed
    neither = canaries(body) - handed - ours
    print(
        f"\n[{name}] litellm rewrite tier: {sorted(rewrite)}"
        f"\n[{name}] litellm handed tier: {sorted(handed)}"
        f"\n[{name}] ours: {sorted(ours)}"
        f"\n[{name}] litellm-only: {sorted(litellm_only)}"
        f"\n[{name}] ours-only: {sorted(ours_only)}"
        f"\n[{name}] neither: {sorted(neither)}"
    )

    assert rewrite, "vacuous: litellm exposed nothing to rewrite"
    assert rewrite == EXPECTED_REWRITE[name]
    assert rewrite <= ours
    assert litellm_only <= set(CARVE_OUTS), sorted(litellm_only - set(CARVE_OUTS))
    assert litellm_only == EXPECTED_LITELLM_ONLY[name]
    assert ours_only == EXPECTED_OURS_ONLY[name]
    assert neither == EXPECTED_NEITHER[name]


async def test_the_stream_flag_changes_no_leaf() -> None:
    def unprefixed(leaves: set[str]) -> set[str]:
        return {c.replace("CNRY_cs_", "CNRY_").replace("CNRY_cu_", "CNRY_") for c in leaves}

    streamed = await litellm_leaves(CallTypes.acompletion, CHAT_STREAM_CONTROL)
    unary = await litellm_leaves(CallTypes.acompletion, CHAT_UNARY_TWIN)
    assert [unprefixed(s) for s in streamed] == [unprefixed(s) for s in unary]
    assert unprefixed(await our_leaves("acompletion", CHAT_STREAM_CONTROL)) == unprefixed(
        await our_leaves("acompletion", CHAT_UNARY_TWIN)
    )
    assert unprefixed(canaries(CHAT_STREAM_CONTROL)) == unprefixed(canaries(CHAT_UNARY_TWIN))


async def test_a_bare_string_input_item_is_rewritten_though_litellm_cannot_read_it() -> None:
    """A bare string inside a Responses ``input`` list is text our walker rewrites; litellm's
    handler cannot even translate it (``transformation.py:1413`` calls ``.get`` on it), so
    a guardrail on that path would fail the request instead."""
    body = {
        "model": "gpt-4o-mini",
        "input": ["CNRY_rb_bare_item", {"role": "user", "content": "CNRY_rb_message"}],
    }

    with pytest.raises(AttributeError):
        await litellm_leaves(CallTypes.aresponses, body)
    assert await our_leaves("aresponses", body) == {"CNRY_rb_bare_item", "CNRY_rb_message"}
