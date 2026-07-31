"""Unit tests for content block walker (task 1)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from corp_llm_gateway.sanitizer.content_blocks import (
    ContentTooDeepError,
    UnsanitizableContentBlockError,
    _collect_json_text,
    _sanitize_json,
    collect_responses_item_text,
    collect_text,
    desanitize_content,
    desanitize_responses_payload,
    sanitize_content,
    sanitize_responses_item,
)


@dataclass(frozen=True)
class MockSanitizeResult:
    """Mock SanitizeResult for testing."""

    sanitized_text: str
    pairs: tuple[tuple[str, str], ...] = ()
    cache_a_hit: bool = False
    skipped: bool = False


# ---- collect_text (read-only pre-scan) --------------------------------------


def test_collect_text_str() -> None:
    assert collect_text("hello world") == ["hello world"]


def test_collect_text_none() -> None:
    assert collect_text(None) == []


def test_collect_text_int() -> None:
    assert collect_text(42) == []


def test_collect_text_empty_list() -> None:
    assert collect_text([]) == []


def test_collect_text_list_text_block() -> None:
    assert collect_text([{"type": "text", "text": "hello"}]) == ["hello"]


def test_collect_text_list_skips_non_text_blocks() -> None:
    content = [
        {"type": "text", "text": "keep me"},
        {"type": "image_url", "image_url": {"url": "https://..."}},
        {"type": "tool_use", "id": "t1", "name": "fn", "input": {}},
    ]
    assert collect_text(content) == ["keep me"]


def test_collect_text_list_skips_non_dict_items() -> None:
    assert collect_text([{"type": "text", "text": "a"}, "plain", 1]) == ["a"]


def test_collect_text_tool_result_str_content() -> None:
    content = [{"type": "tool_result", "content": "some text"}]
    assert collect_text(content) == ["some text"]


def test_collect_text_tool_result_list_content() -> None:
    content = [
        {
            "type": "tool_result",
            "content": [
                {"type": "text", "text": "inner"},
                {"type": "image_url", "image_url": {"url": "https://..."}},
            ],
        }
    ]
    assert collect_text(content) == ["inner"]


def test_collect_text_bare_dict_text_block() -> None:
    assert collect_text({"type": "text", "text": "bare"}) == ["bare"]


def test_collect_text_bare_dict_tool_result() -> None:
    block = {"type": "tool_result", "content": "nested"}
    assert collect_text(block) == ["nested"]


def test_collect_text_bare_dict_non_text_type() -> None:
    assert collect_text({"type": "image_url", "image_url": {}}) == []


def test_collect_text_multiple_text_blocks() -> None:
    content = [
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ]
    assert collect_text(content) == ["first", "second"]


def test_collect_text_does_not_mutate() -> None:
    content = [{"type": "text", "text": "hello"}]
    collect_text(content)
    assert content == [{"type": "text", "text": "hello"}]


# ---- Async sanitize tests ---------------------------------------------------


async def test_sanitize_str_direct() -> None:
    """String content is sanitized directly."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text.replace("alice", "[N1]"), pairs=(("alice", "[N1]"),))

    new_content, results = await sanitize_content("hello alice", mock_sanitize)
    assert new_content == "hello [N1]"
    assert len(results) == 1
    assert results[0].pairs == (("alice", "[N1]"),)


async def test_sanitize_none_passes_through() -> None:
    """None content is passed through unchanged."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        raise AssertionError("should not be called")

    new_content, results = await sanitize_content(None, mock_sanitize)
    assert new_content is None
    assert results == []


async def test_sanitize_list_text_block() -> None:
    """Text block in a list is sanitized."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text.replace("alice", "[N1]"), pairs=(("alice", "[N1]"),))

    content = [{"type": "text", "text": "hello alice"}]
    new_content, results = await sanitize_content(content, mock_sanitize)

    assert len(new_content) == 1
    assert new_content[0]["type"] == "text"
    assert new_content[0]["text"] == "hello [N1]"
    assert len(results) == 1
    assert results[0].pairs == (("alice", "[N1]"),)


async def test_sanitize_list_non_text_blocks_pass_through() -> None:
    """Non-text blocks (image, tool_use, document) are passed through unchanged."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        raise AssertionError("should not be called for non-text blocks")

    content = [
        {"type": "text", "text": "sanitize me"},
        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
        {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {}},
        {"type": "document", "source": "pdf://..."},
    ]

    async def mock_sanitize_selective(text: str) -> MockSanitizeResult:
        if text == "sanitize me":
            return MockSanitizeResult("[SANITIZED]", pairs=(("sanitize me", "[SANITIZED]"),))
        raise AssertionError(f"unexpected text: {text}")

    new_content, results = await sanitize_content(content, mock_sanitize_selective)

    assert len(new_content) == 4
    assert new_content[0]["text"] == "[SANITIZED]"
    # Image, tool_use, document must be byte-identical
    assert new_content[1] == content[1]
    assert new_content[2] == content[2]
    assert new_content[3] == content[3]
    assert len(results) == 1


async def test_sanitize_tool_result_str_content() -> None:
    """tool_result block with str content is recursively sanitized."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(
            text.replace("secret", "[SECRET_001]"),
            pairs=(("secret", "[SECRET_001]"),),
        )

    content = [{"type": "tool_result", "content": "the secret is revealed"}]
    new_content, results = await sanitize_content(content, mock_sanitize)

    assert len(new_content) == 1
    assert new_content[0]["type"] == "tool_result"
    assert new_content[0]["content"] == "the [SECRET_001] is revealed"
    assert len(results) == 1


async def test_sanitize_tool_result_list_content() -> None:
    """tool_result block with list content (block list) is recursively sanitized."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text.replace("alice", "[N1]"), pairs=(("alice", "[N1]"),))

    content = [
        {
            "type": "tool_result",
            "content": [
                {"type": "text", "text": "alice knows secrets"},
                {"type": "image_url", "image_url": {"url": "https://..."}},
            ],
        }
    ]
    new_content, results = await sanitize_content(content, mock_sanitize)

    assert len(new_content) == 1
    result_block = new_content[0]
    assert result_block["type"] == "tool_result"
    assert isinstance(result_block["content"], list)
    assert len(result_block["content"]) == 2
    assert result_block["content"][0]["text"] == "[N1] knows secrets"
    # image_url must be unchanged
    assert result_block["content"][1] == content[0]["content"][1]
    assert len(results) == 1


async def test_sanitize_empty_list() -> None:
    """Empty list returns empty list and no results."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        raise AssertionError("should not be called")

    new_content, results = await sanitize_content([], mock_sanitize)
    assert new_content == []
    assert results == []


async def test_sanitize_list_with_non_dict_items() -> None:
    """Non-dict items in a list are passed through unchanged."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text.upper(), pairs=())

    content = [
        {"type": "text", "text": "hello"},
        "plain string",
        123,
        None,
        [],
    ]
    new_content, _ = await sanitize_content(content, mock_sanitize)

    assert len(new_content) == 5
    assert new_content[0]["text"] == "HELLO"
    assert new_content[1] == "plain string"
    assert new_content[2] == 123
    assert new_content[3] is None
    assert new_content[4] == []


async def test_sanitize_multiple_text_blocks() -> None:
    """Multiple text blocks accumulate results correctly."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text.replace("PII", "[PII_001]"), pairs=(("PII", "[PII_001]"),))

    content = [
        {"type": "text", "text": "first PII here"},
        {"type": "text", "text": "second PII here"},
        {"type": "text", "text": "third PII here"},
    ]
    new_content, results = await sanitize_content(content, mock_sanitize)

    assert len(new_content) == 3
    labels = ["first", "second", "third"]
    assert all(new_content[i]["text"] == f"{labels[i]} [PII_001] here" for i in range(3))
    assert len(results) == 3


# ---- Sync desanitize tests --------------------------------------------------


def test_desanitize_str_direct() -> None:
    """String content is reversed directly."""

    def reverse(text: str) -> str:
        return text.replace("[N1]", "alice")

    new_content = desanitize_content("hello [N1]", reverse)
    assert new_content == "hello alice"


def test_desanitize_none_passes_through() -> None:
    """None content is passed through unchanged."""

    def reverse(text: str) -> str:
        raise AssertionError("should not be called")

    new_content = desanitize_content(None, reverse)
    assert new_content is None


def test_desanitize_list_text_block() -> None:
    """Text block in a list is reversed."""

    def reverse(text: str) -> str:
        return text.replace("[N1]", "alice")

    content = [{"type": "text", "text": "hello [N1]"}]
    new_content = desanitize_content(content, reverse)

    assert len(new_content) == 1
    assert new_content[0]["type"] == "text"
    assert new_content[0]["text"] == "hello alice"


def test_desanitize_tool_result_str_content() -> None:
    """tool_result block with str content is recursively reversed."""

    def reverse(text: str) -> str:
        return text.replace("[SECRET_001]", "secret")

    content = [{"type": "tool_result", "content": "the [SECRET_001] is revealed"}]
    new_content = desanitize_content(content, reverse)

    assert len(new_content) == 1
    assert new_content[0]["type"] == "tool_result"
    assert new_content[0]["content"] == "the secret is revealed"


def test_desanitize_tool_result_list_content() -> None:
    """tool_result block with list content is recursively reversed."""

    def reverse(text: str) -> str:
        return text.replace("[N1]", "alice")

    content = [
        {
            "type": "tool_result",
            "content": [
                {"type": "text", "text": "[N1] knows secrets"},
                {"type": "image_url", "image_url": {"url": "https://..."}},
            ],
        }
    ]
    new_content = desanitize_content(content, reverse)

    assert len(new_content) == 1
    result_block = new_content[0]
    assert result_block["type"] == "tool_result"
    assert isinstance(result_block["content"], list)
    assert len(result_block["content"]) == 2
    assert result_block["content"][0]["text"] == "alice knows secrets"
    # image_url must be unchanged
    assert result_block["content"][1] == content[0]["content"][1]


def test_desanitize_empty_list() -> None:
    """Empty list returns empty list."""

    def reverse(text: str) -> str:
        raise AssertionError("should not be called")

    new_content = desanitize_content([], reverse)
    assert new_content == []


def test_desanitize_list_with_non_text_blocks_unchanged() -> None:
    """Non-text blocks (image, tool_use with empty input, document) pass through byte-identical."""

    def reverse(text: str) -> str:
        return text.replace("[N1]", "alice")

    content = [
        {"type": "text", "text": "hello [N1]"},
        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
        {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {}},
    ]
    new_content = desanitize_content(content, reverse)

    assert len(new_content) == 3
    assert new_content[0]["text"] == "hello alice"
    assert new_content[1] == content[1]
    assert new_content[2] == content[2]


# ---- _collect_json_text helper -----------------------------------------------


def test_collect_json_text_str() -> None:
    assert _collect_json_text("hello") == ["hello"]


def test_collect_json_text_int() -> None:
    assert _collect_json_text(42) == []


def test_collect_json_text_bool() -> None:
    assert _collect_json_text(True) == []


def test_collect_json_text_none() -> None:
    assert _collect_json_text(None) == []


def test_collect_json_text_flat_dict() -> None:
    assert _collect_json_text({"to": "a@x.com", "cc": "b@x.com"}) == ["a@x.com", "b@x.com"]


def test_collect_json_text_dict_preserves_only_values() -> None:
    # Keys must NOT appear in the collected strings.
    result = _collect_json_text({"email": "a@x.com"})
    assert result == ["a@x.com"]
    assert "email" not in result


def test_collect_json_text_list_of_strings() -> None:
    assert _collect_json_text(["a@x.com", "b@x.com"]) == ["a@x.com", "b@x.com"]


def test_collect_json_text_nested_dict() -> None:
    result = _collect_json_text({"outer": {"inner": "secret"}})
    assert result == ["secret"]


def test_collect_json_text_mixed_scalars() -> None:
    result = _collect_json_text({"name": "alice", "age": 30, "active": True, "note": None})
    assert result == ["alice"]


# ---- collect_text: tool_use.input inclusion ---------------------------------


def test_collect_text_tool_use_flat_input() -> None:
    content = [{"type": "tool_use", "id": "t1", "name": "send", "input": {"to": "a@x.com"}}]
    assert collect_text(content) == ["a@x.com"]


def test_collect_text_tool_use_list_input() -> None:
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "send",
            "input": {"cc": ["a@x.com", "b@x.com"]},
        }
    ]
    assert collect_text(content) == ["a@x.com", "b@x.com"]


def test_collect_text_tool_use_empty_input() -> None:
    content = [{"type": "tool_use", "id": "t1", "name": "fn", "input": {}}]
    assert collect_text(content) == []


def test_collect_text_tool_use_mixed_scalars_in_input() -> None:
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"email": "a@x.com", "count": 5, "active": True},
        }
    ]
    assert collect_text(content) == ["a@x.com"]


def test_collect_text_tool_use_keys_not_collected() -> None:
    content = [{"type": "tool_use", "id": "t1", "name": "fn", "input": {"email": "a@x.com"}}]
    result = collect_text(content)
    assert "email" not in result


def test_collect_text_bare_dict_tool_use() -> None:
    block = {"type": "tool_use", "id": "t1", "name": "fn", "input": {"to": "a@x.com"}}
    assert collect_text(block) == ["a@x.com"]


def test_collect_text_image_still_empty() -> None:
    content = [{"type": "image_url", "image_url": {"url": "https://..."}}]
    assert collect_text(content) == []


# ---- sanitize_content: tool_use.input sanitization --------------------------


async def test_sanitize_tool_use_flat_input() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(
            text.replace("a@x.com", "[EMAIL_001]"),
            pairs=(("a@x.com", "[EMAIL_001]"),),
        )

    content = [{"type": "tool_use", "id": "t1", "name": "send", "input": {"to": "a@x.com"}}]
    new_content, results = await sanitize_content(content, mock_sanitize)

    assert new_content[0]["input"]["to"] == "[EMAIL_001]"
    assert new_content[0]["id"] == "t1"
    assert new_content[0]["name"] == "send"
    assert len(results) == 1


async def test_sanitize_tool_use_dict_key_preserved() -> None:
    calls: list[str] = []

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        calls.append(text)
        return MockSanitizeResult(text.upper(), pairs=())

    content = [{"type": "tool_use", "id": "t1", "name": "fn", "input": {"email": "a@x.com"}}]
    new_content, _ = await sanitize_content(content, mock_sanitize)

    # Key "email" must be unchanged; only the value is sanitized.
    assert "email" in new_content[0]["input"]
    assert new_content[0]["input"]["email"] == "A@X.COM"
    assert "EMAIL" not in new_content[0]["input"]
    # sanitize_one must be called with the VALUE, not the key.
    assert "email" not in calls
    assert "a@x.com" in calls


async def test_sanitize_tool_use_list_input() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        replaced = text.replace("a@x.com", "[E1]").replace("b@x.com", "[E2]")
        return MockSanitizeResult(replaced, pairs=())

    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"cc": ["a@x.com", "b@x.com"]},
        }
    ]
    new_content, _ = await sanitize_content(content, mock_sanitize)

    assert new_content[0]["input"]["cc"] == ["[E1]", "[E2]"]


async def test_sanitize_tool_use_non_str_scalars_unchanged() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text, pairs=())

    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"count": 42, "active": True, "ratio": 0.5, "note": None},
        }
    ]
    new_content, results = await sanitize_content(content, mock_sanitize)

    inp = new_content[0]["input"]
    assert inp["count"] == 42
    assert inp["active"] is True
    assert inp["ratio"] == 0.5
    assert inp["note"] is None
    assert results == []


async def test_sanitize_tool_use_nested_dict() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text.replace("secret", "[SEC]"), pairs=())

    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"outer": {"inner": "secret"}},
        }
    ]
    new_content, _ = await sanitize_content(content, mock_sanitize)
    assert new_content[0]["input"]["outer"]["inner"] == "[SEC]"


async def test_sanitize_image_still_passes_through_unchanged() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        raise AssertionError("should not be called")

    content = [{"type": "image_url", "image_url": {"url": "https://..."}}]
    new_content, results = await sanitize_content(content, mock_sanitize)

    assert new_content[0] == content[0]
    assert results == []


# ---- desanitize_content: tool_use.input desanitization ----------------------


def test_desanitize_tool_use_flat_input() -> None:
    def reverse(text: str) -> str:
        return text.replace("[EMAIL_001]", "a@x.com")

    content = [{"type": "tool_use", "id": "t1", "name": "fn", "input": {"to": "[EMAIL_001]"}}]
    new_content = desanitize_content(content, reverse)

    assert new_content[0]["input"]["to"] == "a@x.com"
    assert new_content[0]["id"] == "t1"


def test_desanitize_tool_use_dict_key_preserved() -> None:
    def reverse(text: str) -> str:
        return text.upper()

    content = [{"type": "tool_use", "id": "t1", "name": "fn", "input": {"email": "[E1]"}}]
    new_content = desanitize_content(content, reverse)

    assert "email" in new_content[0]["input"]
    assert new_content[0]["input"]["email"] == "[E1]".upper()


def test_desanitize_tool_use_list_input() -> None:
    def reverse(text: str) -> str:
        return text.replace("[E1]", "a@x.com").replace("[E2]", "b@x.com")

    content = [{"type": "tool_use", "id": "t1", "name": "fn", "input": {"cc": ["[E1]", "[E2]"]}}]
    new_content = desanitize_content(content, reverse)

    assert new_content[0]["input"]["cc"] == ["a@x.com", "b@x.com"]


def test_desanitize_tool_use_non_str_scalars_unchanged() -> None:
    def reverse(text: str) -> str:
        return text.upper()

    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"count": 42, "active": True, "ratio": 0.5, "note": None},
        }
    ]
    new_content = desanitize_content(content, reverse)

    inp = new_content[0]["input"]
    assert inp["count"] == 42
    assert inp["active"] is True
    assert inp["ratio"] == 0.5
    assert inp["note"] is None


# ---- E (NIT): desanitize tool_use with non-empty placeholder input ----------


def test_desanitize_tool_use_nonempty_input_placeholder_rewritten() -> None:
    """Non-empty tool_use.input with a placeholder is actually rewritten."""

    def reverse(text: str) -> str:
        return text.replace("[EMAIL_001]", "a@x.com")

    content = [{"type": "tool_use", "id": "t1", "name": "fn", "input": {"to": "[EMAIL_001]"}}]
    new_content = desanitize_content(content, reverse)
    assert new_content[0]["input"]["to"] == "a@x.com"


# ---- A: document block tests ------------------------------------------------


async def test_sanitize_document_title_and_text_source() -> None:
    """document block: title and source.data (text) are redacted; no originals survive."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        t = text.replace("alice@corp.example", "[E1]").replace("bob@corp.example", "[E2]")
        pairs: list[tuple[str, str]] = []
        if "alice@corp.example" in text:
            pairs.append(("alice@corp.example", "[E1]"))
        if "bob@corp.example" in text:
            pairs.append(("bob@corp.example", "[E2]"))
        return MockSanitizeResult(t, pairs=tuple(pairs))

    block = {
        "type": "document",
        "title": "Report for alice@corp.example",
        "context": "Drafted by alice@corp.example",
        "source": {"type": "text", "data": "See bob@corp.example for details"},
    }
    content = [block]
    new_content, results = await sanitize_content(content, mock_sanitize)

    serialized = json.dumps(new_content)
    assert "alice@corp.example" not in serialized
    assert "bob@corp.example" not in serialized
    assert "[E1]" in serialized
    assert "[E2]" in serialized
    assert new_content[0]["title"] == "Report for [E1]"
    assert new_content[0]["context"] == "Drafted by [E1]"
    assert new_content[0]["source"]["data"] == "See [E2] for details"
    assert len(results) == 3


def test_desanitize_document_title_and_text_source() -> None:
    """document block: title, context, source.data placeholders are restored."""

    def reverse(text: str) -> str:
        return text.replace("[E1]", "alice@corp.example").replace("[E2]", "bob@corp.example")

    content = [
        {
            "type": "document",
            "title": "Report for [E1]",
            "context": "Drafted by [E1]",
            "source": {"type": "text", "data": "See [E2] for details"},
        }
    ]
    new_content = desanitize_content(content, reverse)

    assert new_content[0]["title"] == "Report for alice@corp.example"
    assert new_content[0]["context"] == "Drafted by alice@corp.example"
    assert new_content[0]["source"]["data"] == "See bob@corp.example for details"


async def test_sanitize_document_roundtrip_restores_originals() -> None:
    """Full round-trip: sanitize then desanitize returns originals in document block."""
    mapping: dict[str, str] = {}

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        replaced = text
        pairs: list[tuple[str, str]] = []
        for orig in ("alice@corp.example", "bob@corp.example"):
            if orig in replaced:
                ph = f"[E{len(mapping) + 1}]"
                mapping[ph] = orig
                replaced = replaced.replace(orig, ph)
                pairs.append((orig, ph))
        return MockSanitizeResult(replaced, pairs=tuple(pairs))

    def reverse(text: str) -> str:
        for ph, orig in mapping.items():
            text = text.replace(ph, orig)
        return text

    block = {
        "type": "document",
        "title": "Report for alice@corp.example",
        "source": {"type": "text", "data": "See bob@corp.example"},
    }
    new_content, _ = await sanitize_content([block], mock_sanitize)
    restored = desanitize_content(new_content, reverse)

    assert restored[0]["title"] == "Report for alice@corp.example"
    assert restored[0]["source"]["data"] == "See bob@corp.example"


async def test_sanitize_document_base64_source_untouched() -> None:
    """document with source.type==base64 must not be altered."""
    call_count = 0

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        nonlocal call_count
        call_count += 1
        return MockSanitizeResult(text, pairs=())

    b64_data = "SGVsbG8gV29ybGQ="
    block = {
        "type": "document",
        "title": "A title",
        "source": {"type": "base64", "media_type": "application/pdf", "data": b64_data},
    }
    new_content, _ = await sanitize_content([block], mock_sanitize)

    # title is sanitized (1 call), base64 data is NOT touched
    assert call_count == 1
    assert new_content[0]["source"]["data"] == b64_data


async def test_sanitize_document_url_source_untouched() -> None:
    """document with source.type==url must not be altered."""
    call_count = 0

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        nonlocal call_count
        call_count += 1
        return MockSanitizeResult(text, pairs=())

    block = {
        "type": "document",
        "title": "Doc",
        "source": {"type": "url", "url": "https://example.com/secret.pdf"},
    }
    new_content, _ = await sanitize_content([block], mock_sanitize)
    assert call_count == 1
    assert new_content[0]["source"]["url"] == "https://example.com/secret.pdf"


async def test_sanitize_document_content_source_recurses() -> None:
    """document with source.type==content recurses into the content block list."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        t = text.replace("alice@corp.example", "[E1]")
        pairs = (("alice@corp.example", "[E1]"),) if "alice@corp.example" in text else ()
        return MockSanitizeResult(t, pairs=pairs)

    block = {
        "type": "document",
        "source": {
            "type": "content",
            "content": [{"type": "text", "text": "Contact alice@corp.example"}],
        },
    }
    new_content, results = await sanitize_content([block], mock_sanitize)

    inner = new_content[0]["source"]["content"][0]["text"]
    assert inner == "Contact [E1]"
    assert "alice@corp.example" not in json.dumps(new_content)
    assert len(results) == 1


def test_collect_text_document_title_context_text_source() -> None:
    content = [
        {
            "type": "document",
            "title": "My title",
            "context": "Some context",
            "source": {"type": "text", "data": "Body text"},
        }
    ]
    result = collect_text(content)
    assert result == ["My title", "Some context", "Body text"]


def test_collect_text_document_base64_source_not_collected() -> None:
    content = [
        {
            "type": "document",
            "title": "Doc",
            "source": {"type": "base64", "data": "SGVsbG8="},
        }
    ]
    result = collect_text(content)
    assert result == ["Doc"]
    assert "SGVsbG8=" not in result


def test_collect_text_document_content_source_recurses() -> None:
    content = [
        {
            "type": "document",
            "source": {
                "type": "content",
                "content": [{"type": "text", "text": "inner text"}],
            },
        }
    ]
    result = collect_text(content)
    assert result == ["inner text"]


# ---- C: recursion depth guard tests -----------------------------------------


async def test_sanitize_json_depth_limit_raises() -> None:
    """_sanitize_json raises ContentTooDeepError beyond _MAX_JSON_DEPTH."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text, pairs=())

    # Build a dict nested 65 levels deep (exceeds _MAX_JSON_DEPTH=64).
    deep: dict = {"v": "leaf"}
    for _ in range(65):
        deep = {"k": deep}

    with pytest.raises(ContentTooDeepError):
        await _sanitize_json(deep, mock_sanitize)


def test_desanitize_json_depth_limit_caps_silently() -> None:
    """_desanitize_json silently caps at depth limit (no raise, no infinite loop)."""
    from corp_llm_gateway.sanitizer.content_blocks import _desanitize_json

    calls: list[str] = []

    def reverse(text: str) -> str:
        calls.append(text)
        return text.upper()

    # 65-deep nested dict — desanitize must return without raising.
    deep: dict = {"v": "leaf"}
    for _ in range(65):
        deep = {"k": deep}

    result = _desanitize_json(deep, reverse)
    # Must return something (not raise); the capped branch returns value unchanged.
    assert result is not None


def test_collect_json_text_depth_limit_caps_silently() -> None:
    """_collect_json_text silently returns [] at depth limit."""
    # 65-deep nested dict
    deep: dict = {"v": "leaf"}
    for _ in range(65):
        deep = {"k": deep}

    result = _collect_json_text(deep)
    # Must return a list (not raise); leaf may be missing (capped).
    assert isinstance(result, list)


# ---- D: M1-14 — no original survives sanitization for tool_use + document ---


async def test_no_original_in_sanitized_tool_use_and_document() -> None:
    """M1-14: raw PII must be absent from the serialized sanitized egress."""
    email1 = "alice@corp.example"
    email2 = "bob@corp.example"
    mapping_store: dict[str, str] = {}

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        replaced = text
        pairs: list[tuple[str, str]] = []
        for orig in (email1, email2):
            if orig in replaced:
                ph = f"[E{len(mapping_store) + 1}]"
                mapping_store[ph] = orig
                replaced = replaced.replace(orig, ph)
                pairs.append((orig, ph))
        return MockSanitizeResult(replaced, pairs=tuple(pairs))

    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "send_email",
            "input": {"to": email1, "subject": f"Hi {email1}"},
        },
        {
            "type": "document",
            "title": f"Report for {email2}",
            "source": {"type": "text", "data": f"Authored by {email2}"},
        },
    ]
    new_content, _ = await sanitize_content(content, mock_sanitize)
    serialized = json.dumps(new_content)

    assert email1 not in serialized, f"raw {email1!r} leaked into egress"
    assert email2 not in serialized, f"raw {email2!r} leaked into egress"
    assert "[E1]" in serialized or "[E2]" in serialized


# ---- F: reasoning_text/summary_text/refusal block widening (defect #1) ------


async def test_sanitize_reasoning_text_block() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text.replace("acme", "[ORG_001]"), pairs=(("acme", "[ORG_001]"),))

    content = [{"type": "reasoning_text", "text": "thinking about acme"}]
    new_content, results = await sanitize_content(content, mock_sanitize)
    assert new_content[0]["text"] == "thinking about [ORG_001]"
    assert len(results) == 1


async def test_sanitize_summary_text_block() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text.replace("acme", "[ORG_001]"), pairs=(("acme", "[ORG_001]"),))

    content = [{"type": "summary_text", "text": "plan for acme rollout"}]
    new_content, results = await sanitize_content(content, mock_sanitize)
    assert new_content[0]["text"] == "plan for [ORG_001] rollout"
    assert len(results) == 1


async def test_sanitize_refusal_block() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text.replace("acme", "[ORG_001]"), pairs=(("acme", "[ORG_001]"),))

    content = [{"type": "refusal", "refusal": "cannot share acme secrets"}]
    new_content, results = await sanitize_content(content, mock_sanitize)
    assert new_content[0]["refusal"] == "cannot share [ORG_001] secrets"
    assert len(results) == 1


def test_desanitize_reasoning_text_and_summary_text_and_refusal_blocks() -> None:
    def reverse(text: str) -> str:
        return text.replace("[ORG_001]", "acme")

    content = [
        {"type": "reasoning_text", "text": "thinking about [ORG_001]"},
        {"type": "summary_text", "text": "plan for [ORG_001]"},
        {"type": "refusal", "refusal": "cannot share [ORG_001] secrets"},
    ]
    new_content = desanitize_content(content, reverse)
    assert new_content[0]["text"] == "thinking about acme"
    assert new_content[1]["text"] == "plan for acme"
    assert new_content[2]["refusal"] == "cannot share acme secrets"


def test_collect_text_reasoning_text_and_summary_text_and_refusal_blocks() -> None:
    content = [
        {"type": "reasoning_text", "text": "a"},
        {"type": "summary_text", "text": "b"},
        {"type": "refusal", "refusal": "c"},
    ]
    assert collect_text(content) == ["a", "b", "c"]


# ---- G: unknown block type fails closed (compounding defect #1) ------------


async def test_sanitize_unknown_block_type_fails_closed() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        raise AssertionError("should not be called")

    content = [{"type": "some_future_block", "payload": "raw secret"}]
    with pytest.raises(UnsanitizableContentBlockError):
        await sanitize_content(content, mock_sanitize)


async def test_sanitize_known_opaque_block_types_still_pass_through() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        raise AssertionError("should not be called for opaque blocks")

    content = [
        {"type": "image", "source": "binary"},
        {"type": "image_url", "image_url": {"url": "https://..."}},
        {"type": "thinking", "thinking": "internal reasoning"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]
    new_content, results = await sanitize_content(content, mock_sanitize)
    assert new_content == content
    assert results == []


# ---- H: sanitize_responses_item / collect_responses_item_text --------------


async def test_sanitize_responses_item_custom_tool_call_input() -> None:
    """defect #1: custom_tool_call.input must be sanitized, not skipped."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        replaced = text.replace("sk-secret", "[SECRET_001]")
        pairs = (("sk-secret", "[SECRET_001]"),) if "sk-secret" in text else ()
        return MockSanitizeResult(replaced, pairs=pairs)

    item = {
        "type": "custom_tool_call",
        "call_id": "call_1",
        "name": "apply_patch",
        "input": "*** Add File: x\n+KEY=sk-secret",
    }
    new_item, results = await sanitize_responses_item(item, mock_sanitize)
    assert new_item["input"] == "*** Add File: x\n+KEY=[SECRET_001]"
    assert new_item["call_id"] == "call_1"
    assert len(results) == 1


async def test_sanitize_responses_item_reasoning_summary() -> None:
    """defect #1: reasoning.summary[].text must be sanitized, not skipped."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        replaced = text.replace("acme", "[ORG_001]")
        pairs = (("acme", "[ORG_001]"),) if "acme" in text else ()
        return MockSanitizeResult(replaced, pairs=pairs)

    item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "plan for acme"}],
    }
    new_item, results = await sanitize_responses_item(item, mock_sanitize)
    assert new_item["summary"][0]["text"] == "plan for [ORG_001]"
    assert len(results) == 1


async def test_sanitize_responses_item_function_call_arguments_json() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        replaced = text.replace("a@x.com", "[E1]")
        pairs = (("a@x.com", "[E1]"),) if "a@x.com" in text else ()
        return MockSanitizeResult(replaced, pairs=pairs)

    item = {
        "type": "function_call",
        "call_id": "call_1",
        "name": "send",
        "arguments": json.dumps({"to": "a@x.com"}),
    }
    new_item, results = await sanitize_responses_item(item, mock_sanitize)
    assert json.loads(new_item["arguments"]) == {"to": "[E1]"}
    assert len(results) == 1


async def test_sanitize_responses_item_function_call_output() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        replaced = text.replace("acme", "[ORG_001]")
        pairs = (("acme", "[ORG_001]"),) if "acme" in text else ()
        return MockSanitizeResult(replaced, pairs=pairs)

    item = {"type": "function_call_output", "call_id": "call_1", "output": "acme.cs"}
    new_item, results = await sanitize_responses_item(item, mock_sanitize)
    assert new_item["output"] == "[ORG_001].cs"
    assert len(results) == 1


async def test_sanitize_responses_item_message_content_unaffected_fields_kept() -> None:
    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text, pairs=())

    item = {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    new_item, _ = await sanitize_responses_item(item, mock_sanitize)
    assert new_item["role"] == "user"
    assert new_item["content"][0]["text"] == "hi"


def test_collect_responses_item_text_custom_tool_call_and_reasoning() -> None:
    assert collect_responses_item_text({"type": "custom_tool_call", "input": "patch text"}) == [
        "patch text"
    ]
    assert collect_responses_item_text(
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "plan"}]}
    ) == ["plan"]


def test_collect_responses_item_text_empty_item() -> None:
    assert collect_responses_item_text({"type": "reasoning", "summary": []}) == []


# ---- I: symmetry — sanitize field set == desanitize field set (defect #1) --


async def test_responses_registry_symmetry_sanitize_matches_desanitize() -> None:
    """Every field _RESPONSES_TEXT_FIELDS lists must be covered by BOTH
    sanitize_responses_item and desanitize_responses_payload — a field added to
    only one side is exactly the class of bug defect #1 was.

    Uses ``type: function_call_output`` so the "output" field's item-type gate
    (computer_call_output screenshots are deliberately NOT scanned) is satisfied."""
    from corp_llm_gateway.sanitizer.content_blocks import (
        _RESPONSES_OPAQUE_FIELDS,
        _RESPONSES_TEXT_FIELDS,
    )

    fields = sorted(_RESPONSES_TEXT_FIELDS - _RESPONSES_OPAQUE_FIELDS)
    item = {field: f"ORIGINAL_{field}" for field in fields}
    item["type"] = "function_call_output"

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        return MockSanitizeResult(text.replace("ORIGINAL", "PLACEHOLDER"), pairs=())

    sanitized, _ = await sanitize_responses_item(item, mock_sanitize)
    sanitized_fields = {f for f in fields if sanitized[f] != item[f]}
    assert sanitized_fields == set(fields), (
        f"sanitize_responses_item did not touch: {set(fields) - sanitized_fields}"
    )

    def reverse(text: str) -> str:
        return text.replace("PLACEHOLDER", "ORIGINAL")

    restored = desanitize_responses_payload(sanitized, reverse)
    restored_fields = {f for f in fields if restored[f] == item[f]}
    assert restored_fields == set(fields), (
        f"desanitize_responses_payload did not restore: {set(fields) - restored_fields}"
    )


def test_responses_block_list_fields_are_registered_text_fields() -> None:
    """A field routed through _RESPONSES_BLOCK_LIST_FIELDS (sanitize_content's
    block-aware walker) but NOT also in _RESPONSES_TEXT_FIELDS would be sanitized
    on egress and never restored on the response — desanitize_responses_payload
    only reverses registered fields. This is the realistic drift the flat-string
    symmetry test above can't catch (it never exercises the list/dict routing)."""
    from corp_llm_gateway.sanitizer.content_blocks import (
        _RESPONSES_BLOCK_LIST_FIELDS,
        _RESPONSES_TEXT_FIELDS,
    )

    assert _RESPONSES_BLOCK_LIST_FIELDS <= _RESPONSES_TEXT_FIELDS


async def test_responses_registry_symmetry_realistic_nested_shapes() -> None:
    """Exercise the actual list/dict routing (content/summary block-lists,
    output as a structured dict, arguments as JSON) instead of flat strings, so
    drift in the ROUTING itself — not just the field-name set — is caught."""

    async def mock_sanitize(text: str) -> MockSanitizeResult:
        replaced = text.replace("acme", "[ORG_001]")
        pairs = (("acme", "[ORG_001]"),) if "acme" in text else ()
        return MockSanitizeResult(replaced, pairs=pairs)

    # NOTE: "output" is spec'd as a plain string (matches OpenAI's
    # function_call_output.output shape). A dict/list "output" with arbitrary
    # tool-defined key names is handled by sanitize_responses_item's full-tree
    # scan (MAJOR 4 fail-safe) but desanitize_responses_payload's field-name-gated
    # recursion can't restore an unregistered nested key — that's a pre-existing,
    # out-of-scope, non-leak asymmetry (wrong direction: a placeholder survives
    # instead of an original leaking), not part of this fix.
    item = {
        "type": "function_call_output",
        "content": [{"type": "input_text", "text": "acme content"}],
        "summary": [{"type": "summary_text", "text": "acme summary"}],
        "output": "acme output",
        "input": "acme input",
        "arguments": json.dumps({"q": "acme arguments"}),
    }
    sanitized, _ = await sanitize_responses_item(item, mock_sanitize)
    assert sanitized["content"][0]["text"] == "[ORG_001] content"
    assert sanitized["summary"][0]["text"] == "[ORG_001] summary"
    assert sanitized["output"] == "[ORG_001] output"
    assert sanitized["input"] == "[ORG_001] input"
    assert json.loads(sanitized["arguments"]) == {"q": "[ORG_001] arguments"}

    def reverse(text: str) -> str:
        return text.replace("[ORG_001]", "acme")

    restored = desanitize_responses_payload(sanitized, reverse)
    assert restored["content"][0]["text"] == "acme content"
    assert restored["summary"][0]["text"] == "acme summary"
    assert restored["output"] == "acme output"
    assert restored["input"] == "acme input"
    assert json.loads(restored["arguments"]) == {"q": "acme arguments"}


# ---- J: Critical 1 — real block types must not fail closed ------------------


async def _sanitize_one_redact_acme(text: str) -> MockSanitizeResult:
    replaced = text.replace("acme", "[ORG_001]")
    pairs = (("acme", "[ORG_001]"),) if "acme" in text else ()
    return MockSanitizeResult(replaced, pairs=pairs)


async def test_sanitize_server_tool_use_input_is_scanned() -> None:
    block = {
        "type": "server_tool_use",
        "id": "t1",
        "name": "web_search",
        "input": {"query": "acme"},
    }
    new_content, results = await sanitize_content([block], _sanitize_one_redact_acme)
    assert new_content[0]["input"]["query"] == "[ORG_001]"
    assert len(results) == 1


async def test_sanitize_mcp_tool_use_input_is_scanned() -> None:
    block = {"type": "mcp_tool_use", "id": "t1", "name": "fetch", "input": {"q": "acme"}}
    new_content, results = await sanitize_content([block], _sanitize_one_redact_acme)
    assert new_content[0]["input"]["q"] == "[ORG_001]"
    assert len(results) == 1


async def test_sanitize_web_search_tool_result_content_is_scanned() -> None:
    block = {
        "type": "web_search_tool_result",
        "tool_use_id": "t1",
        "content": [{"type": "web_search_result", "url": "https://x", "title": "acme title"}],
    }
    new_content, results = await sanitize_content([block], _sanitize_one_redact_acme)
    assert new_content[0]["content"][0]["title"] == "[ORG_001] title"
    # _sanitize_json scans every string leaf: "type", "url", and "title".
    assert len(results) == 3


async def test_sanitize_web_search_tool_result_protects_encrypted_content() -> None:
    """Anthropic signs encrypted_content for replay — must not be rewritten."""
    block = {
        "type": "web_search_tool_result",
        "tool_use_id": "t1",
        "content": [{"type": "web_search_result", "encrypted_content": "acme-signed-blob"}],
    }
    new_content, _ = await sanitize_content([block], _sanitize_one_redact_acme)
    assert new_content[0]["content"][0]["encrypted_content"] == "acme-signed-blob"


async def test_sanitize_code_execution_tool_result_content_is_scanned() -> None:
    block = {
        "type": "code_execution_tool_result",
        "tool_use_id": "t2",
        "content": {"stdout": "acme output", "stderr": ""},
    }
    new_content, results = await sanitize_content([block], _sanitize_one_redact_acme)
    assert new_content[0]["content"]["stdout"] == "[ORG_001] output"
    # _sanitize_json scans every string leaf, including the sibling "stderr": "".
    assert len(results) == 2


async def test_sanitize_mcp_tool_result_content_is_scanned() -> None:
    block = {
        "type": "mcp_tool_result",
        "tool_use_id": "t3",
        "content": [{"type": "text", "text": "acme result"}],
    }
    new_content, results = await sanitize_content([block], _sanitize_one_redact_acme)
    assert new_content[0]["content"][0]["text"] == "[ORG_001] result"
    # _sanitize_json is not block-type-aware: it also scans the "type": "text"
    # discriminator string (harmless — it just doesn't match "acme").
    assert len(results) == 2


async def test_sanitize_search_result_title_and_content_are_scanned() -> None:
    block = {
        "type": "search_result",
        "title": "acme doc",
        "source": "kb://doc1",
        "content": [{"type": "text", "text": "acme body"}],
    }
    new_content, results = await sanitize_content([block], _sanitize_one_redact_acme)
    assert new_content[0]["title"] == "[ORG_001] doc"
    assert new_content[0]["content"][0]["text"] == "[ORG_001] body"
    assert new_content[0]["source"] == "kb://doc1"
    assert len(results) == 2


async def test_sanitize_container_upload_passes_through_unchanged() -> None:
    block = {"type": "container_upload", "file_id": "file_123"}

    async def fail(text: str) -> MockSanitizeResult:
        raise AssertionError("container_upload has no scannable text")

    new_content, results = await sanitize_content([block], fail)
    assert new_content[0] == block
    assert results == []


@pytest.mark.parametrize(
    "block",
    [
        {"type": "input_image", "image_url": "https://example.com/x.png"},
        {"type": "input_file", "file_id": "file_789", "filename": "report.pdf"},
        {"type": "file", "file": {"file_id": "file_456", "filename": "notes.txt"}},
        {"type": "input_audio", "input_audio": {"data": "base64==", "format": "wav"}},
        {"type": "output_audio", "data": "base64=="},
    ],
)
async def test_sanitize_opaque_openai_block_types_pass_through_unchanged(
    block: dict[str, object],
) -> None:
    async def fail(text: str) -> MockSanitizeResult:
        raise AssertionError(f"{block['type']} has no scannable text")

    new_content, results = await sanitize_content([block], fail)
    assert new_content[0] == block
    assert results == []


async def test_sanitize_still_fails_closed_on_genuinely_unknown_type() -> None:
    """The widened allowlist must not become a blanket pass-through."""

    async def fail(text: str) -> MockSanitizeResult:
        raise AssertionError("should not be called")

    with pytest.raises(UnsanitizableContentBlockError):
        await sanitize_content([{"type": "some_future_block", "payload": "raw"}], fail)


def test_unsanitizable_content_block_error_does_not_echo_block_type() -> None:
    """M1-14 surface (iii): the exception message must not carry client-controlled
    content (block_type is a client-supplied string, chained via `raise ... from
    exc` into whatever eventually logs the traceback)."""
    secret_type = "SECRET-token-abc123"
    try:
        import asyncio

        async def fail(text: str) -> MockSanitizeResult:
            raise AssertionError("should not be called")

        asyncio.run(sanitize_content([{"type": secret_type}], fail))
    except UnsanitizableContentBlockError as exc:
        assert secret_type not in str(exc)
    else:
        raise AssertionError("expected UnsanitizableContentBlockError")


# ---- K: Major 3 — tool_calls/function_call on a Responses item -------------


async def test_sanitize_responses_item_covers_tool_calls_field() -> None:
    """A Responses `input` item can carry Chat-Completions-shaped tool_calls —
    dropped entirely by field-name-only registry lookup without this coverage."""
    item = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "run", "arguments": json.dumps({"k": "acme"})},
            }
        ],
    }
    new_item, results = await sanitize_responses_item(item, _sanitize_one_redact_acme)
    assert json.loads(new_item["tool_calls"][0]["function"]["arguments"]) == {"k": "[ORG_001]"}
    assert len(results) == 1


def test_collect_responses_item_text_covers_tool_calls_field() -> None:
    item = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "run", "arguments": '{"k":"v"}'}}
        ],
    }
    assert collect_responses_item_text(item) == ["v"]


async def test_sanitize_responses_item_covers_function_call_field() -> None:
    """Legacy message.function_call (not the item-type `function_call`) on a
    Responses item must also be covered."""
    item = {
        "role": "assistant",
        "content": None,
        "function_call": {"name": "run", "arguments": json.dumps({"k": "acme"})},
    }
    new_item, results = await sanitize_responses_item(item, _sanitize_one_redact_acme)
    assert json.loads(new_item["function_call"]["arguments"]) == {"k": "[ORG_001]"}
    assert len(results) == 1


# ---- L: Major 4 — non-string registry field must not be silently dropped ---


async def test_sanitize_responses_item_dict_shaped_input_is_scanned() -> None:
    """custom_tool_call.input off-spec as a dict must still be scanned, not
    silently skipped (the exact defect #1 class, unpatched for non-str)."""
    item = {"type": "custom_tool_call", "call_id": "c1", "input": {"cmd": "acme"}}
    new_item, results = await sanitize_responses_item(item, _sanitize_one_redact_acme)
    assert new_item["input"] == {"cmd": "[ORG_001]"}
    assert len(results) == 1


def test_collect_responses_item_text_dict_shaped_input() -> None:
    item = {"type": "custom_tool_call", "call_id": "c1", "input": {"cmd": "secret"}}
    assert collect_responses_item_text(item) == ["secret"]


async def test_sanitize_responses_item_bare_scalar_registry_field_fails_closed() -> None:
    """A registered text field holding a bare scalar (not str/dict/list/None) is
    a genuinely unrecognized shape — fail closed rather than silently drop it."""
    item = {"type": "custom_tool_call", "call_id": "c1", "input": 12345}
    with pytest.raises(UnsanitizableContentBlockError):
        await sanitize_responses_item(item, _sanitize_one_redact_acme)


# ---- M: "output" full-scan is gated to the item types that carry text ------


async def test_sanitize_responses_item_computer_call_output_not_scanned() -> None:
    """computer_call_output.output is a base64 screenshot — must not be pushed
    through the sanitizer (oversize-policy trip risk on ordinary screenshots)."""

    async def fail(text: str) -> MockSanitizeResult:
        raise AssertionError("computer_call_output.output must not be scanned")

    item = {"type": "computer_call_output", "call_id": "c1", "output": "base64screenshotdata"}
    new_item, results = await sanitize_responses_item(item, fail)
    assert new_item["output"] == "base64screenshotdata"
    assert results == []


def test_collect_responses_item_text_computer_call_output_not_collected() -> None:
    item = {"type": "computer_call_output", "call_id": "c1", "output": "base64screenshotdata"}
    assert collect_responses_item_text(item) == []
