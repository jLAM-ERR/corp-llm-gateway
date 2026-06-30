"""Tests for sanitizer.segmenter — DP-5.

Covers: split_segments (fence split, comment classification, prose) and
split_identifier (camel/Pascal/snake/kebab/SCREAMING — exact sub-tokens +
offsets).
"""

from __future__ import annotations

import pytest

from corp_llm_gateway.sanitizer.segmenter import (
    SegmentKind,
    split_identifier,
    split_segments,
)

# ---------------------------------------------------------------------------
# split_segments — prose
# ---------------------------------------------------------------------------


def test_pure_prose_returns_single_prose_segment() -> None:
    text = "Hello world, how are you?"
    segs = split_segments(text)
    assert len(segs) == 1
    assert segs[0].kind == SegmentKind.PROSE
    assert segs[0].text == text
    assert segs[0].start == 0
    assert segs[0].end == len(text)


def test_empty_text_returns_empty_list() -> None:
    assert split_segments("") == []


def test_offsets_are_absolute_slices_of_original() -> None:
    text = "prose\n```python\ncode\n```\nmore prose"
    segs = split_segments(text)
    for seg in segs:
        assert seg.text == text[seg.start : seg.end], f"offset mismatch for {seg!r}"


# ---------------------------------------------------------------------------
# split_segments — fenced code blocks
# ---------------------------------------------------------------------------


def test_fenced_code_block_produces_code_segment() -> None:
    text = "before\n```python\nx = 1\n```\nafter"
    segs = split_segments(text)
    kinds = {s.kind for s in segs}
    assert SegmentKind.CODE in kinds
    assert SegmentKind.PROSE in kinds


def test_prose_before_and_after_fence() -> None:
    text = "intro\n```python\nx = 1\n```\noutro"
    segs = split_segments(text)
    prose = [s for s in segs if s.kind == SegmentKind.PROSE]
    assert any("intro" in s.text for s in prose)
    assert any("outro" in s.text for s in prose)


def test_multiple_code_blocks() -> None:
    text = "p1\n```python\nc1\n```\np2\n```js\nc2\n```\np3"
    segs = split_segments(text)
    code = [s for s in segs if s.kind == SegmentKind.CODE]
    assert len(code) == 2
    assert any("c1" in s.text for s in code)
    assert any("c2" in s.text for s in code)


def test_no_language_tag_fence_works() -> None:
    text = "before\n```\ncode here\n```\nafter"
    segs = split_segments(text)
    code = [s for s in segs if s.kind == SegmentKind.CODE]
    assert any("code here" in s.text for s in code)


# ---------------------------------------------------------------------------
# split_segments — comment classification inside code blocks
# ---------------------------------------------------------------------------


def test_line_comment_hash_classified_as_comment() -> None:
    text = "```python\nx = 1  # a comment\ny = 2\n```"
    segs = split_segments(text)
    comment_segs = [s for s in segs if s.kind == SegmentKind.COMMENT]
    assert any("comment" in s.text for s in comment_segs)


def test_line_comment_double_slash_classified_as_comment() -> None:
    text = "```c\nint x = 1; // c comment\n```"
    segs = split_segments(text)
    comment_segs = [s for s in segs if s.kind == SegmentKind.COMMENT]
    assert any("c comment" in s.text for s in comment_segs)


def test_block_comment_classified_as_comment() -> None:
    text = "```c\n/* block comment */\nint x = 1;\n```"
    segs = split_segments(text)
    comment_segs = [s for s in segs if s.kind == SegmentKind.COMMENT]
    assert any("block comment" in s.text for s in comment_segs)


def test_comment_and_code_offsets_are_slices() -> None:
    text = "```python\nx = 1  # inline\ny = 2\n```"
    segs = split_segments(text)
    for seg in segs:
        assert seg.text == text[seg.start : seg.end]


# ---------------------------------------------------------------------------
# split_identifier — camel / Pascal / snake / kebab / SCREAMING
# ---------------------------------------------------------------------------


def test_pascal_case_companynameabc_service() -> None:
    tokens = split_identifier("CompanynameabcService")
    assert ("Companynameabc", 0, 14) in tokens
    assert ("Service", 14, 21) in tokens


def test_pascal_case_betadirect_client() -> None:
    tokens = split_identifier("BetadirectClient")
    assert ("Betadirect", 0, 10) in tokens
    assert ("Client", 10, 16) in tokens


def test_pascal_case_kdir_service() -> None:
    tokens = split_identifier("KdirService")
    assert ("Kdir", 0, 4) in tokens
    assert ("Service", 4, 11) in tokens


def test_pascal_case_user_account_service() -> None:
    tokens = split_identifier("UserAccountService")
    texts = [t for t, _, _ in tokens]
    assert "User" in texts
    assert "Account" in texts
    assert "Service" in texts


def test_camel_case_split() -> None:
    tokens = split_identifier("myVarName")
    texts = [t for t, _, _ in tokens]
    assert "my" in texts
    assert "Var" in texts
    assert "Name" in texts


def test_snake_case_split() -> None:
    tokens = split_identifier("snake_case_name")
    texts = [t for t, _, _ in tokens]
    assert "snake" in texts
    assert "case" in texts
    assert "name" in texts


def test_kebab_case_split() -> None:
    tokens = split_identifier("kebab-case-name")
    texts = [t for t, _, _ in tokens]
    assert "kebab" in texts
    assert "case" in texts
    assert "name" in texts


def test_screaming_snake_split() -> None:
    tokens = split_identifier("SCREAMING_SNAKE")
    texts = [t for t, _, _ in tokens]
    assert "SCREAMING" in texts
    assert "SNAKE" in texts


def test_consecutive_caps_abc_test() -> None:
    tokens = split_identifier("ABCTest")
    texts = [t for t, _, _ in tokens]
    assert "ABC" in texts
    assert "Test" in texts


def test_get_http_response() -> None:
    tokens = split_identifier("getHTTPResponse")
    texts = [t for t, _, _ in tokens]
    assert "get" in texts
    assert "HTTP" in texts
    assert "Response" in texts


def test_offsets_relative_to_name() -> None:
    """Every sub-token text must equal name[start:end]."""
    for name in [
        "CompanynameabcService",
        "BetadirectClient",
        "KdirService",
        "UserAccountService",
        "SCREAMING_SNAKE",
        "myVarName",
        "snake_case",
        "ABCTest",
    ]:
        tokens = split_identifier(name)
        for token_text, start, end in tokens:
            assert name[start:end] == token_text, f"{name}: {token_text!r} at [{start}:{end}]"


def test_empty_identifier_returns_empty() -> None:
    assert split_identifier("") == []


def test_single_lowercase_word() -> None:
    tokens = split_identifier("hello")
    assert len(tokens) == 1
    assert tokens[0] == ("hello", 0, 5)


def test_single_uppercase_word() -> None:
    tokens = split_identifier("CONSTANT")
    assert len(tokens) == 1
    assert tokens[0][0] == "CONSTANT"


@pytest.mark.parametrize(
    "name,expected_texts",
    [
        ("CompanynameabcService", {"Companynameabc", "Service"}),
        ("BetadirectClient", {"Betadirect", "Client"}),
        ("KdirService", {"Kdir", "Service"}),
        ("snake_case_id", {"snake", "case", "id"}),
        ("SCREAMING_SNAKE", {"SCREAMING", "SNAKE"}),
    ],
)
def test_parametrized_split(name: str, expected_texts: set[str]) -> None:
    texts = {t for t, _, _ in split_identifier(name)}
    assert expected_texts.issubset(texts), f"{name}: got {texts}, want superset of {expected_texts}"
