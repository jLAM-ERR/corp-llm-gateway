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
# split_segments — F5: fence-delimiter spans are covered (100% tiling)
# ---------------------------------------------------------------------------

_FENCE = "```"


def test_fence_lang_tag_span_is_covered_by_a_segment() -> None:
    """F5 repro: PII in the opening-fence lang-tag position must land in a segment.

    Pre-fix the ```<lang> opener + ``` closer were in NO segment, so the local
    detectors never scanned them and PII there egressed unredacted.
    """
    email = "alice.smith@corp.lan"
    text = f"{_FENCE}{email}\nsome_code = 1\n{_FENCE}"
    segs = split_segments(text)
    assert any(email in s.text for s in segs), "lang-tag PII is in no segment (F5 leak)"


def test_closing_fence_delimiter_is_covered_by_a_segment() -> None:
    text = f"{_FENCE}python\nx = 1\n{_FENCE}"
    segs = split_segments(text)
    # The trailing ``` must belong to a segment, not fall into an uncovered gap.
    assert any(s.end == len(text) and s.text.endswith(_FENCE) for s in segs)


@pytest.mark.asyncio
async def test_local_pass_detects_pii_in_fence_lang_tag() -> None:
    """F5 behavioral repro: the local detection pass now sees lang-tag PII."""
    from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector
    from corp_llm_gateway.sanitizer.local_pass import LocalDetectionPass

    email = "alice.smith@corp.lan"
    text = f"{_FENCE}{email}\nsome_code = 1\n{_FENCE}"
    findings = await LocalDetectionPass([RegexChecksumDetector()]).findings(text)
    hit = next((f for f in findings if f.text == email), None)
    assert hit is not None, "lang-tag email not detected (F5 leak)"
    # The finding offset is absolute into the original text.
    assert text[hit.start : hit.end] == email


# A corpus that exercises fenced / no-lang / empty-body / multiple / nested-
# comment-marker / unterminated / comment-only fence shapes.
_TILING_CORPUS = [
    "",
    "plain prose only, no fence",
    f"before\n{_FENCE}python\nx = 1\n{_FENCE}\nafter",
    f"{_FENCE}alice.smith@corp.lan\ncode\n{_FENCE}",
    f"{_FENCE}\nno lang tag\n{_FENCE}",
    f"{_FENCE}\n{_FENCE}",
    f"p1\n{_FENCE}python\nc1\n{_FENCE}\np2\n{_FENCE}js\nc2\n{_FENCE}\np3",
    f"{_FENCE}c\nint x = 1; // y /* z */ done\n{_FENCE}",
    f"{_FENCE}python\nunterminated fence with alice@corp.lan",
    "prose /* stray */ no fence # hash",
    f"{_FENCE}py\n# lead comment\ncode\n{_FENCE}",
    f"lead\n{_FENCE}\n/* only comment */{_FENCE}\ntail",
]


@pytest.mark.parametrize("text", _TILING_CORPUS)
def test_split_segments_tiles_input_contiguously(text: str) -> None:
    """F5 coverage invariant: sorted spans cover [0, len(text)) with NO gap AND
    NO overlap. A bare sum(len)==len can be satisfied by an overlap compensating
    a gap; the contiguous tiling is the property that actually closes F5."""
    segs = split_segments(text)
    spans = sorted((s.start, s.end) for s in segs)
    cursor = 0
    for start, end in spans:
        assert start == cursor, f"gap or overlap at {cursor} (next span starts {start}): {text!r}"
        assert end > start, f"empty/inverted span ({start},{end}): {text!r}"
        cursor = end
    assert cursor == len(text), f"tail gap: covered {cursor} of {len(text)}: {text!r}"
    # Redundant with the tiling but pins the pure-slice contract too.
    assert sum(s.end - s.start for s in segs) == len(text)
    for s in segs:
        assert s.text == text[s.start : s.end]


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
