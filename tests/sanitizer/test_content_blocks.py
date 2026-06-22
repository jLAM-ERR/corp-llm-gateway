"""Unit tests for content block walker (task 1)."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from corp_llm_gateway.sanitizer.content_blocks import (
    ContentTooDeepError,
    _collect_json_text,
    _sanitize_json,
    collect_text,
    desanitize_content,
    sanitize_content,
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
