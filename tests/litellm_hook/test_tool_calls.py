"""Tool calls in a request: OpenAI tool_calls / function_call arguments, Anthropic
tool_use.input, Responses tool-call items."""

import json

import pytest

from corp_llm_gateway.litellm_hook import GuardrailHttpException
from tests.hook_fixtures import (
    _assistant_dict_args_msg,
    _assistant_tool_call_msg,
    _build_guardrail,
    _corp_llm_email_per_segment,
    _corp_llm_returning,
    _data_with_token,
)


async def test_pre_call_responses_input_item_tool_calls_field_is_sanitized() -> None:
    """A Responses `input` item carrying Chat-Completions-shaped tool_calls (e.g.
    a client mixing chat history into `input`) must still be sanitized — dropped
    entirely by field-name-only registry lookup without this coverage."""
    g, _ = _build_guardrail([("topsecretvalue", "[SECRET_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "run", "arguments": '{"k":"topsecretvalue"}'},
                    }
                ],
            }
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out = await g.pre_call(data)
    assert "topsecretvalue" not in json.dumps(out), (
        "tool_calls on a Responses item leaked the original"
    )


async def test_pre_call_responses_custom_tool_call_dict_input_is_sanitized() -> None:
    """custom_tool_call.input off-spec as a dict must be scanned, not silently
    skipped (the exact defect #1 leak class, unpatched for the non-str case)."""
    g, _ = _build_guardrail([("topsecretvalue", "[SECRET_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "type": "custom_tool_call",
                "call_id": "c1",
                "name": "apply_patch",
                "input": {"cmd": "topsecretvalue"},
            }
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out = await g.pre_call(data)
    assert "topsecretvalue" not in json.dumps(out), (
        "dict-shaped custom_tool_call.input leaked the original"
    )


async def test_pre_call_openai_tool_calls_arguments_sanitized() -> None:
    """F4: a tool-call-only assistant message has function.arguments sanitized."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="do it", model="gpt-4o")
    data["messages"].append(_assistant_tool_call_msg(json.dumps({"user": "hi alice", "id": 7})))
    out = await g.pre_call(data)

    new_args = json.loads(out["messages"][1]["tool_calls"][0]["function"]["arguments"])
    assert new_args == {"user": "hi [N1]", "id": 7}
    assert "alice" not in json.dumps(out["messages"][1])


async def test_pre_call_legacy_function_call_arguments_sanitized() -> None:
    """F4: legacy message.function_call.arguments is sanitized."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi", model="gpt-4o")
    data["messages"].append(
        {
            "role": "assistant",
            "content": None,
            "function_call": {"name": "f", "arguments": json.dumps({"note": "call alice"})},
        }
    )
    out = await g.pre_call(data)

    new_args = json.loads(out["messages"][1]["function_call"]["arguments"])
    assert new_args == {"note": "call [N1]"}


async def test_pre_call_tool_calls_secret_in_arguments_caught_by_dlp() -> None:
    """F4 repro (inverted): an sk- secret in tool_calls arguments is rescanned by Stage 5.

    Pre-fix, collect_text excluded tool_calls so the secret bypassed the DLP guard.
    """
    g, _ = _build_guardrail([])  # oracle redacts nothing → secret survives to Stage 5
    secret = "sk-" + "a" * 40
    data = _data_with_token("tok-1", content="hello", model="gpt-4o")
    data["messages"].append(_assistant_tool_call_msg(json.dumps({"key": secret})))

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_DLP_BLOCKED"


async def test_pre_call_tool_calls_invalid_json_arguments_sanitized_whole() -> None:
    """F4 edge: non-JSON arguments are sanitized as a single leaf (never egress raw)."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi", model="gpt-4o")
    data["messages"].append(_assistant_tool_call_msg("raw alice text"))
    out = await g.pre_call(data)

    assert out["messages"][1]["tool_calls"][0]["function"]["arguments"] == "raw [N1] text"


async def test_pre_call_anthropic_tool_use_still_sanitized() -> None:
    """Regression: Anthropic tool_use.input stays sanitized; the F4 path is additive."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    content = [
        {"type": "text", "text": "using tool"},
        {"type": "tool_use", "id": "tu1", "name": "save", "input": {"who": "hi alice"}},
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    assert out["messages"][0]["content"][1]["input"] == {"who": "hi [N1]"}
    assert "tool_calls" not in out["messages"][0]


async def test_pre_call_tool_calls_dict_arguments_sanitized() -> None:
    """A3 review: dict-shaped function.arguments has its string leaves sanitized
    (stays a dict; non-str scalars untouched)."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="do it", model="gpt-4o")
    data["messages"].append(_assistant_dict_args_msg({"user": "hi alice", "id": 7}))
    out = await g.pre_call(data)

    new_args = out["messages"][1]["tool_calls"][0]["function"]["arguments"]
    assert new_args == {"user": "hi [N1]", "id": 7}
    assert "alice" not in json.dumps(out["messages"][1])


async def test_pre_call_tool_calls_dict_arguments_secret_caught_by_dlp() -> None:
    """A3 review repro: an sk- secret in DICT-shaped arguments is rescanned by Stage 5.

    Pre-fix, collect_tool_call_text skipped non-str arguments, so the secret was
    neither sanitized nor DLP-scanned and egressed raw.
    """
    g, _ = _build_guardrail([])  # oracle redacts nothing → DLP is the backstop
    secret = "sk-" + "a" * 40
    data = _data_with_token("tok-1", content="hello", model="gpt-4o")
    data["messages"].append(_assistant_dict_args_msg({"key": secret}))

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_DLP_BLOCKED"


async def test_pre_call_tool_calls_unrecognized_scalar_arguments_fails_closed() -> None:
    """A3 review: a genuinely unrecognized arguments shape (bare scalar) fails closed
    rather than egressing un-scanned."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi", model="gpt-4o")
    data["messages"].append(_assistant_dict_args_msg(1234))  # type: ignore[arg-type]

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_BAD_REQUEST"


async def test_pre_call_deep_nesting_returns_400_e_bad_request() -> None:
    """A tool_use input nested > _MAX_JSON_DEPTH must return 400 E_BAD_REQUEST."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_returning([]))
    # Build a dict nested 66 levels deep (exceeds _MAX_JSON_DEPTH=64).
    deep: dict = {"v": "leaf"}
    for _ in range(66):
        deep = {"k": deep}
    content = [{"type": "tool_use", "id": "t1", "name": "fn", "input": deep}]
    data = _data_with_token("tok-1", content=content)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 400
    assert ei.value.error_code == "E_BAD_REQUEST"


async def test_tool_use_input_nested_dict_in_dict_sanitized() -> None:
    """Nested dict-in-dict: all string leaves are sanitized; non-str scalars unchanged."""
    g, _ = _build_guardrail([("secret", "[SEC_001]")])
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {
                "outer": {"inner": "secret"},
                "count": 42,
                "active": True,
                "note": None,
            },
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    inp = out["messages"][0]["content"][0]["input"]
    assert inp["outer"]["inner"] == "[SEC_001]"
    assert inp["count"] == 42
    assert inp["active"] is True
    assert inp["note"] is None


async def test_tool_use_input_list_of_strings_sanitized() -> None:
    """List of strings inside tool_use.input: every element is sanitized."""
    g, _ = _build_guardrail([("secret", "[SEC_001]")])
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"items": ["secret", "harmless", "secret"]},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    assert out["messages"][0]["content"][0]["input"]["items"] == [
        "[SEC_001]",
        "harmless",
        "[SEC_001]",
    ]


async def test_tool_use_input_dict_keys_not_altered() -> None:
    """Dict keys in tool_use.input must NOT be sanitized — only values.

    The key name "to" is a common English word; even if the corp-LLM were to
    redact it, we verify it stays unchanged. The value carries the PII email.
    """
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"to": "addr@corp.example"},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    inp = out["messages"][0]["content"][0]["input"]
    # Key "to" must survive unchanged.
    assert "to" in inp, f"key was altered: {inp!r}"
    # Value must be sanitized.
    assert "addr@corp.example" not in inp["to"], f"value not sanitized: {inp['to']!r}"


async def test_tool_use_input_no_pii_passes_through() -> None:
    """tool_use.input with no PII: block structure preserved, no corp-LLM call for values."""
    g, _ = _build_guardrail([("secret", "[SEC_001]")])
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "get_weather",
            "input": {"city": "Moscow", "units": "metric"},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    inp = out["messages"][0]["content"][0]["input"]
    assert inp["city"] == "Moscow"
    assert inp["units"] == "metric"


async def test_tool_use_input_image_block_still_passes_through() -> None:
    """image_url block alongside tool_use is still unchanged (regression)."""
    g, _ = _build_guardrail([("secret", "[SEC_001]")])
    image_block = {"type": "image_url", "image_url": {"url": "https://img.example/x.png"}}
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"note": "secret"},
        },
        image_block,
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    assert out["messages"][0]["content"][1] == image_block
    assert out["messages"][0]["content"][0]["input"]["note"] == "[SEC_001]"


async def test_pre_call_local_shell_call_action_command_is_sanitized() -> None:
    """CRITICAL 1 repro from the review, reproduced end-to-end through pre_call:
    a `local_shell_call` item's `action.command`/`action.env` must be scanned,
    not silently forwarded because `action` isn't a registered field name.

    NOTE: `desanitize_responses_payload` (the response walker) stays field-
    name-gated for `action`/`command` — a pre-existing, documented, non-leak
    asymmetry (placeholder survives instead of an original leaking; see the
    content_blocks.py discovered-follow-up note), not part of this fix."""
    g, _ = _build_guardrail([("acme-corp-secret", "[SECRET_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "type": "local_shell_call",
                "call_id": "call_1",
                "status": "completed",
                "action": {
                    "type": "exec",
                    "command": ["bash", "-lc", "echo acme-corp-secret"],
                    "env": {"TOKEN": "acme-corp-secret"},
                },
            }
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out = await g.pre_call(data)
    assert "acme-corp-secret" not in json.dumps(out), (
        "local_shell_call.action.command/env leaked the original"
    )
