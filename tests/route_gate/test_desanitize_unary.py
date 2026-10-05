"""The response reversal on a unary response: a request sanitised by the hook, its JSON
response restored by the desanitiser (through `tests/response_restore.py`)."""

import json
import logging
from typing import Any

import pytest

from tests.hook_fixtures import (
    _build_guardrail,
    _corp_llm_email_per_segment,
    _data_with_token,
    _restore_object,
    _terminal,
)
from tests.response_restore import respond_unary, restore_unary


async def test_post_call_unary_unexpected_error_returns_opaque_500(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Major: restoring a response runs AFTER placeholders are replaced by originals, so
    an unexpected exception there is the one place raw content could plausibly leak. The
    reversal (the ASGI desanitiser) answers a content-free 500 ``E_INTERNAL`` and writes
    one ``failed`` record with that code."""
    g, sink = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    def _boom(response: Any, mapping: Any) -> Any:
        raise RuntimeError(f"reconstruct failed for alice: {response!r}")

    import corp_llm_gateway.litellm_hook as hook_mod

    monkeypatch.setattr(hook_mod, "_apply_reverse_to_response", _boom)
    terminal, records = _terminal()
    caplog.clear()  # the pre-call's own lines name the user, who is also "alice"
    with caplog.at_level(logging.DEBUG):
        status, body = await respond_unary(
            g, data, {"choices": [{"message": {"content": "hello [N1]!"}}]}, terminal=terminal
        )

    assert status == 500
    assert body["error"]["code"] == "E_INTERNAL"
    assert "alice" not in json.dumps(body) and "alice" not in caplog.text
    assert sink.records == []
    (record,) = records.records
    assert (record["status"], record["error_code"]) == ("failed", "E_INTERNAL")


async def test_pre_call_responses_custom_tool_call_input_replay_does_not_leak() -> None:
    """Turn 1: the model's custom_tool_call.input carries a placeholder, desanitized
    for the client. Turn 2: the client (Codex-style) replays that exact item in
    `input` — the original must be re-sanitized, never egress raw."""
    original = "sk-corp-secret-token-001"
    g, _ = _build_guardrail([(original, "[SECRET_001]")])
    turn1_data = {
        "model": "gpt-5.6-sol",
        "input": f"Store the token {original} in config.py",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(turn1_data)
    turn1_response = {
        "output": [
            {
                "type": "custom_tool_call",
                "call_id": "call_1",
                "name": "apply_patch",
                "input": "*** Add File: config.py\n+API_KEY = [SECRET_001]",
            }
        ]
    }
    restored = await restore_unary(g, turn1_data, turn1_response)
    assert restored["output"][0]["input"] == f"*** Add File: config.py\n+API_KEY = {original}"

    turn2_data = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "type": "custom_tool_call",
                "call_id": "call_1",
                "name": "apply_patch",
                "input": restored["output"][0]["input"],
            },
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out = await g.pre_call(turn2_data)

    assert original not in json.dumps(out), "raw secret leaked on custom_tool_call.input replay"


async def test_pre_call_responses_reasoning_summary_replay_does_not_leak() -> None:
    """Same replay shape as above for reasoning.summary[].text."""
    original = "internal-codename-zephyr"
    g, _ = _build_guardrail([(original, "[PROJECT_001]")])
    turn1_data = {
        "model": "gpt-5.6-sol",
        "input": f"Summarize plans for {original}",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(turn1_data)
    turn1_response = {
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [{"type": "summary_text", "text": "Working on [PROJECT_001] rollout"}],
            }
        ]
    }
    restored = await restore_unary(g, turn1_data, turn1_response)
    assert restored["output"][0]["summary"][0]["text"] == f"Working on {original} rollout"

    turn2_data = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": restored["output"][0]["summary"],
            },
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    out = await g.pre_call(turn2_data)

    assert original not in json.dumps(out), "raw original leaked on reasoning.summary replay"


async def test_post_call_unary_restores_bracket_stripped_identifier() -> None:
    """Bare-alias restoration is a Codex-only behavior (defect #6)."""
    original = "KdirCorpCalculatorService"
    g, _ = _build_guardrail([(original, "[LOCATION_007]")], forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-luna",
        "input": f"Create class {original}",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(data)
    response = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Created LOCATION_007"}],
            },
            {
                "type": "custom_tool_call",
                "input": "*** Add File: LOCATION_007.cs",
            },
        ]
    }

    out = await restore_unary(g, data, response)

    assert out["output"][0]["content"][0]["text"] == f"Created {original}"
    assert out["output"][1]["input"] == f"*** Add File: {original}.cs"


async def test_post_call_does_not_restore_user_supplied_bare_placeholder() -> None:
    original = "KdirCorpCalculatorService"
    g, _ = _build_guardrail([(original, "[LOCATION_007]")], forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-luna",
        "input": f"Keep literal LOCATION_007 and create {original}",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(data)

    out = await restore_unary(
        g,
        data,
        {"output": [{"type": "message", "content": [{"text": "LOCATION_007"}]}]},
    )

    assert out["output"][0]["content"][0]["text"] == "LOCATION_007"


async def test_post_call_unary_bare_alias_does_not_corrupt_containing_identifier() -> None:
    """Repro from defect #6(i): MY_PROJECT_001 merely CONTAINS the bare alias
    PROJECT_001 — an unbounded str.replace mangles it into MY_Zephyr Ledger."""
    original = "Zephyr Ledger"
    g, _ = _build_guardrail([(original, "[PROJECT_001]")], forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-luna",
        "input": f"track {original}",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(data)
    text = "const MY_PROJECT_001 = 1; // see PROJECT_001"
    response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}

    out = await restore_unary(g, data, response)

    assert (
        out["output"][0]["content"][0]["text"] == "const MY_PROJECT_001 = 1; // see Zephyr Ledger"
    )


async def test_post_call_unary_bare_alias_relocation_repro() -> None:
    original = "SecretPlace"
    g, _ = _build_guardrail([(original, "[LOCATION_007]")], forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-luna",
        "input": f"see {original}",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(data)
    text = "RELOCATION_0071 and PICKUP_LOCATION_007X"
    response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}

    out = await restore_unary(g, data, response)

    assert out["output"][0]["content"][0]["text"] == text


async def test_post_call_unary_bracket_stripped_identifier_not_restored_when_codex_flag_off() -> (
    None
):
    """Default (non-Codex) path: bare-alias restoration must be OFF, so
    responses stay byte-identical to release/1.0.x — no `forward_chatgpt_auth`."""
    original = "KdirCorpCalculatorService"
    g, _ = _build_guardrail([(original, "[LOCATION_007]")])
    data = {
        "model": "gpt-5.6-luna",
        "input": f"Create class {original}",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(data)

    out = await restore_unary(
        g,
        data,
        {"output": [{"type": "message", "content": [{"text": "LOCATION_007"}]}]},
    )

    assert out["output"][0]["content"][0]["text"] == "LOCATION_007"


async def test_post_call_unary_reverses_placeholder() -> None:
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = {"choices": [{"message": {"role": "assistant", "content": "hello [N1]!"}}]}
    out = await restore_unary(g, data, response)
    assert out["choices"][0]["message"]["content"] == "hello alice!"


async def test_post_call_unary_reverses_responses_output_and_preserves_encrypted() -> None:
    g, _ = _build_guardrail([("Kdir", "[ORG_001]")])
    data = {
        "model": "gpt-5.6-sol",
        "input": "Implement KdirService",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    await g.pre_call(data)
    response = {
        "id": "resp_1",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Created [ORG_001]Service"}],
            },
            {
                "type": "function_call",
                "name": "write_file",
                "arguments": '{"path":"[ORG_001]Service.cs"}',
            },
            {"type": "reasoning", "encrypted_content": "[ORG_001]"},
        ],
    }

    out = await restore_unary(g, data, response)

    assert out["output"][0]["content"][0]["text"] == "Created KdirService"
    assert json.loads(out["output"][1]["arguments"])["path"] == "KdirService.cs"
    assert out["output"][2]["encrypted_content"] == "[ORG_001]"


async def test_post_call_unary_no_state_returns_unchanged() -> None:
    g, _ = _build_guardrail()
    response = {"choices": [{"message": {"content": "no map"}}]}
    out = await restore_unary(g, {"_corp_gateway_request_id": "missing"}, response)
    assert out == response


async def test_post_call_unary_non_model_response_passes_through_unchanged_like_release() -> None:
    """release/1.0.x returned any non-str/non-dict response unchanged — an
    object with no ``model_dump`` still does, matching that baseline exactly."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = 12345
    out = _restore_object(g, data, response)
    assert out is response


async def test_post_call_unary_anthropic_native_block_response() -> None:
    """Task 4: Anthropic-native response with top-level content list is desanitized."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    # Anthropic native response shape: {"type":"message","content":[...]}
    response = {
        "type": "message",
        "content": [
            {"type": "text", "text": "hello [N1]!"},
            {"type": "image_url", "image_url": {"url": "https://..."}},
        ],
    }
    out = await restore_unary(g, data, response)

    assert out["content"][0]["type"] == "text"
    assert out["content"][0]["text"] == "hello alice!"
    assert out["content"][1] == response["content"][1]


async def test_post_call_unary_choices_list_content() -> None:
    """Task 4: choices with list-of-blocks message.content are desanitized."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    # OpenAI-compatible choices format with list content (gpt-4o multimodal).
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "response [N1] text"},
                        {"type": "image_url", "image_url": {"url": "https://..."}},
                    ]
                }
            }
        ]
    }
    out = await restore_unary(g, data, response)

    assert out["choices"][0]["message"]["content"][0]["type"] == "text"
    assert out["choices"][0]["message"]["content"][0]["text"] == "response alice text"
    original_image = response["choices"][0]["message"]["content"][1]
    assert out["choices"][0]["message"]["content"][1] == original_image


async def test_post_call_unary_choices_str_content_regression() -> None:
    """Task 4: choices with str message.content still work (OpenAI str regression)."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = {"choices": [{"message": {"content": "hello [N1]!"}}]}
    out = await restore_unary(g, data, response)

    assert out["choices"][0]["message"]["content"] == "hello alice!"


async def test_post_call_unary_gpt4o_multimodal_content_parts() -> None:
    """Task 4: OpenAI gpt-4o multimodal content-parts are handled (image untouched)."""
    g, _ = _build_guardrail([("user@example.com", "[EMAIL_001]")])
    data = _data_with_token("tok-1", content="contact user@example.com", model="gpt-4o")
    await g.pre_call(data)

    # gpt-4o-style response with mixed content types.
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "Email is [EMAIL_001]"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "https://example.com/image.png",
                                "detail": "high",
                            },
                        },
                    ]
                }
            }
        ]
    }
    out = await restore_unary(g, data, response)

    # Text part reversed, image part untouched.
    assert out["choices"][0]["message"]["content"][0]["text"] == "Email is user@example.com"
    assert (
        out["choices"][0]["message"]["content"][1]
        == response["choices"][0]["message"]["content"][1]
    )


async def test_post_call_unary_tool_calls_arguments_desanitized() -> None:
    """F4: placeholders in a response's tool_calls arguments are reversed."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": '{"who": "[N1]"}'},
                        }
                    ],
                }
            }
        ]
    }
    out = await restore_unary(g, data, response)

    new_args = json.loads(out["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
    assert new_args == {"who": "alice"}


async def test_post_call_unary_legacy_function_call_desanitized() -> None:
    """F4: placeholders in a response's legacy function_call arguments are reversed."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = {
        "choices": [{"message": {"function_call": {"name": "f", "arguments": '{"who": "[N1]"}'}}}]
    }
    out = await restore_unary(g, data, response)

    new_args = json.loads(out["choices"][0]["message"]["function_call"]["arguments"])
    assert new_args == {"who": "alice"}


async def test_post_call_unary_dict_arguments_desanitized() -> None:
    """A3 review: placeholders in dict-shaped response arguments are restored."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "f", "arguments": {"who": "[N1]"}},
                        }
                    ],
                }
            }
        ]
    }
    out = await restore_unary(g, data, response)

    new_args = out["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert new_args == {"who": "alice"}


async def test_post_call_unary_both_choices_and_content_ignores_content() -> None:
    """If response has BOTH choices AND top-level content, only choices path runs."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    # Malformed response with both choices and content.
    response = {
        "choices": [{"message": {"content": [{"type": "text", "text": "from choices [N1]"}]}}],
        "content": [{"type": "text", "text": "from top-level [N1]"}],
    }
    out = await restore_unary(g, data, response)

    # Only choices path is executed (per the if/elif in code).
    assert out["choices"][0]["message"]["content"][0]["text"] == "from choices alice"
    # Top-level content is untouched (may or may not be present in response).


async def test_post_call_unary_non_text_blocks_byte_identical() -> None:
    """Non-text blocks in post_call response are byte-identical to input."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    image_block = {
        "type": "image_url",
        "image_url": {"url": "https://example.com/image.png", "detail": "high"},
    }
    tool_use_block = {
        "type": "tool_use",
        "id": "t1",
        "name": "get_weather",
        "input": {"location": "NYC"},
    }
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "hello [N1]"},
                        image_block,
                        tool_use_block,
                    ]
                }
            }
        ]
    }
    out = await restore_unary(g, data, response)

    # Non-text blocks must be dict-equal (byte-identical).
    assert out["choices"][0]["message"]["content"][1] == image_block
    assert out["choices"][0]["message"]["content"][2] == tool_use_block


async def test_post_call_unary_placeholder_in_one_block_not_another() -> None:
    """Placeholder that appears in one text block is only reversed in that block."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "first block: [N1]"},
                        {"type": "text", "text": "second block: no placeholder"},
                    ]
                }
            }
        ]
    }
    out = await restore_unary(g, data, response)

    assert out["choices"][0]["message"]["content"][0]["text"] == "first block: alice"
    assert out["choices"][0]["message"]["content"][1]["text"] == "second block: no placeholder"


async def test_round_trip_list_content_preserves_structure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sanitize list-content message (pre_call), then desanitize response (post_call).

    Original structure must be preserved and content restored exactly.
    """
    g, _ = _build_guardrail([("alice", "[N1]"), ("bob", "[N2]")])
    content = [
        {"type": "text", "text": "hello alice"},
        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
        {"type": "text", "text": "goodbye bob"},
    ]
    data = _data_with_token("tok-1", content=content)

    with caplog.at_level(logging.INFO):
        await g.pre_call(data)

    # Simulate response from upstream.
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "hi [N1]"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/img.png"}},
                        {"type": "text", "text": "bye [N2]"},
                    ]
                }
            }
        ]
    }

    out = await restore_unary(g, data, response)

    # Verify round-trip: structure preserved, originals restored.
    out_content = out["choices"][0]["message"]["content"]
    assert len(out_content) == 3
    assert out_content[0]["type"] == "text"
    assert out_content[0]["text"] == "hi alice"
    assert out_content[1]["type"] == "image_url"
    assert out_content[1] == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/img.png"},
    }
    assert out_content[2]["type"] == "text"
    assert out_content[2]["text"] == "bye bob"


async def test_length_descending_placeholder_substitution_prevents_shadowing() -> None:
    """M1-9: longer placeholders must be reversed before shorter ones.

    If we have [EMAIL_1] and [EMAIL_12], reversing [EMAIL_1] first would
    shadow [EMAIL_12] and produce [EMAIL_12] → incorrect.
    Sort longest first to avoid this.
    """
    g, _ = _build_guardrail(
        [
            ("alice@corp.com", "[EMAIL_1]"),
            ("alice.smith@corp.com", "[EMAIL_12]"),
        ]
    )
    data = _data_with_token("tok-1", content="emails alice@corp.com and alice.smith@corp.com")
    await g.pre_call(data)

    response = {
        "choices": [
            {
                "message": {
                    "content": "[EMAIL_12] and [EMAIL_1]",
                }
            }
        ]
    }
    out = await restore_unary(g, data, response)

    # Verify BOTH are reversed correctly (not one shadows the other).
    result_text = out["choices"][0]["message"]["content"]
    assert "alice.smith@corp.com" in result_text
    assert "alice@corp.com" in result_text


async def test_post_call_unary_anthropic_native_with_multiple_content_types() -> None:
    """Anthropic native response: top-level content list with mixed types."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    response = {
        "type": "message",
        "content": [
            {"type": "text", "text": "hello [N1]"},
            {"type": "image_url", "image_url": {"url": "https://..."}},
            {"type": "text", "text": "footer [N1]"},
        ],
    }
    out = await restore_unary(g, data, response)

    assert out["content"][0]["text"] == "hello alice"
    assert out["content"][1] == response["content"][1]
    assert out["content"][2]["text"] == "footer alice"


async def test_post_call_unary_anthropic_native_top_level_content_str_reversed() -> None:
    """Fix B: Anthropic-native response with top-level content as a STR is reversed.

    Before fix B, the elif branch only matched list; a str top-level content
    with a placeholder would egress un-reversed.
    """
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)

    # Anthropic-native shape with str content (edge case but valid).
    response = {"type": "message", "content": "Hello [N1], your request was processed."}
    out = await restore_unary(g, data, response)

    assert out["content"] == "Hello alice, your request was processed."
    assert "[N1]" not in out["content"]


async def test_tool_use_input_round_trip_distinct_emails_restored() -> None:
    """M2: tool_use.input PII is redacted on egress and restored on unary reverse.

    A message with a tool_use block whose input carries two distinct emails.
    After pre_call: no original email appears in input values; the two emails
    get distinct tokens (allocator collision-split). After post_call_unary with
    a response whose tool_use input echoes those placeholders, both originals
    are restored.
    """
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "send",
            "input": {"to": "a@corp.example", "cc": ["b@corp.example"]},
        }
    ]
    data = _data_with_token("tok-1", content=content)
    out = await g.pre_call(data)

    tool_block = out["messages"][0]["content"][0]
    assert tool_block["type"] == "tool_use"
    to_val = tool_block["input"]["to"]
    cc_val = tool_block["input"]["cc"][0]

    # Originals must not appear in egress.
    assert "a@corp.example" not in to_val, f"to leaked: {to_val!r}"
    assert "b@corp.example" not in cc_val, f"cc leaked: {cc_val!r}"

    # Two distinct emails must get distinct tokens.
    assert to_val != cc_val, f"collision: both got {to_val!r}"

    # post_call_unary must restore both placeholders in a tool_use response block.
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "send",
                            "input": {"to": to_val, "cc": [cc_val]},
                        }
                    ]
                }
            }
        ]
    }
    result = await restore_unary(g, data, response)
    restored = result["choices"][0]["message"]["content"][0]["input"]
    assert restored["to"] == "a@corp.example", f"to not restored: {restored['to']!r}"
    assert restored["cc"] == ["b@corp.example"], f"cc not restored: {restored['cc']!r}"
