"""Unit tests for content block walker (task 1)."""

from __future__ import annotations

from dataclasses import dataclass

from corp_llm_gateway.sanitizer.content_blocks import (
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
    """Non-text blocks (image, tool_use, document) pass through byte-identical."""

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
