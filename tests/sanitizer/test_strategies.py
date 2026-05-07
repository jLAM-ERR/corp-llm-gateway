import json

import pytest

from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME
from corp_llm_gateway.sanitizer.strategies import (
    FunctionCallStrategy,
    JsonStrategy,
    RegexStrategy,
    StrategyError,
)


def _chat_response_with_tool_call(args: dict | str) -> dict:
    args_str = args if isinstance(args, str) else json.dumps(args)
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": SANITIZE_TOOL_NAME,
                                "arguments": args_str,
                            },
                        }
                    ]
                }
            }
        ]
    }


def _chat_response_with_text(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


# FunctionCallStrategy ------------------------------------------------------


async def test_function_call_extracts_pairs() -> None:
    raw = _chat_response_with_tool_call({
        "pairs": [
            {"original": "alice", "replacement": "[NAME_001]"},
            {"original": "bob", "replacement": "[NAME_002]"},
        ]
    })
    result = await FunctionCallStrategy().extract(raw)
    assert result.pairs == (("alice", "[NAME_001]"), ("bob", "[NAME_002]"))


async def test_function_call_empty_pairs_returns_empty() -> None:
    raw = _chat_response_with_tool_call({"pairs": []})
    result = await FunctionCallStrategy().extract(raw)
    assert result.pairs == ()


async def test_function_call_no_tool_calls_raises() -> None:
    raw = _chat_response_with_text("nothing here")
    with pytest.raises(StrategyError, match="no tool_calls"):
        await FunctionCallStrategy().extract(raw)


async def test_function_call_wrong_tool_name_raises() -> None:
    raw = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": "different_tool", "arguments": "{}"},
                        }
                    ]
                }
            }
        ]
    }
    with pytest.raises(StrategyError, match="not present"):
        await FunctionCallStrategy().extract(raw)


async def test_function_call_malformed_json_raises() -> None:
    raw = _chat_response_with_tool_call("{not json")
    with pytest.raises(StrategyError, match="not valid JSON"):
        await FunctionCallStrategy().extract(raw)


async def test_function_call_missing_pairs_field_raises() -> None:
    raw = _chat_response_with_tool_call({"other_field": []})
    with pytest.raises(StrategyError, match="missing or non-list"):
        await FunctionCallStrategy().extract(raw)


async def test_function_call_drops_pair_with_empty_original() -> None:
    raw = _chat_response_with_tool_call({
        "pairs": [
            {"original": "", "replacement": "[X]"},
            {"original": "alice", "replacement": "[NAME_001]"},
        ]
    })
    result = await FunctionCallStrategy().extract(raw)
    assert result.pairs == (("alice", "[NAME_001]"),)


# JsonStrategy --------------------------------------------------------------


async def test_json_strategy_pure_json_message() -> None:
    raw = _chat_response_with_text(
        '{"pairs": [{"original": "alice", "replacement": "[N1]"}]}'
    )
    result = await JsonStrategy().extract(raw)
    assert result.pairs == (("alice", "[N1]"),)


async def test_json_strategy_extracts_embedded_json() -> None:
    raw = _chat_response_with_text(
        'Here are the findings:\n```json\n'
        '{"pairs": [{"original": "alice", "replacement": "[N1]"}]}\n'
        '```'
    )
    result = await JsonStrategy().extract(raw)
    assert result.pairs == (("alice", "[N1]"),)


async def test_json_strategy_no_json_raises() -> None:
    raw = _chat_response_with_text("Just prose, no JSON here.")
    with pytest.raises(StrategyError, match="no JSON object"):
        await JsonStrategy().extract(raw)


async def test_json_strategy_malformed_json_raises() -> None:
    raw = _chat_response_with_text('Look: {pairs: not valid}')
    with pytest.raises(StrategyError):
        await JsonStrategy().extract(raw)


async def test_json_strategy_handles_nested_braces() -> None:
    raw = _chat_response_with_text(
        '{"pairs": [{"original": "a {b} c", "replacement": "[X]"}]}'
    )
    result = await JsonStrategy().extract(raw)
    assert result.pairs == (("a {b} c", "[X]"),)


# RegexStrategy -------------------------------------------------------------


async def test_regex_strategy_em_dash_lines() -> None:
    raw = _chat_response_with_text(
        "Findings:\n- alice → [NAME_001]\n- bob → [NAME_002]\n"
    )
    result = await RegexStrategy().extract(raw)
    assert result.pairs == (("alice", "[NAME_001]"), ("bob", "[NAME_002]"))


async def test_regex_strategy_ascii_arrow_also_accepted() -> None:
    raw = _chat_response_with_text("- alice -> [N1]")
    result = await RegexStrategy().extract(raw)
    assert result.pairs == (("alice", "[N1]"),)


async def test_regex_strategy_quoted_form() -> None:
    raw = _chat_response_with_text("- `alice cooper` → `[NAME_001]`")
    result = await RegexStrategy().extract(raw)
    assert result.pairs == (("alice cooper", "[NAME_001]"),)


async def test_regex_strategy_no_pairs_raises() -> None:
    raw = _chat_response_with_text("No findings here. The text is clean.")
    with pytest.raises(StrategyError, match="no `original -> replacement`"):
        await RegexStrategy().extract(raw)


async def test_regex_strategy_skips_non_matching_lines() -> None:
    raw = _chat_response_with_text(
        "Header\n- alice → [N1]\nSome explanation\n- bob → [N2]\nFooter"
    )
    result = await RegexStrategy().extract(raw)
    assert result.pairs == (("alice", "[N1]"), ("bob", "[N2]"))
