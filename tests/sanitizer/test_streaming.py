from collections.abc import AsyncIterator

import pytest

from corp_llm_gateway.sanitizer import StreamingDesanitizer, StrategyResult


def _mapping(*pairs: tuple[str, str]) -> StrategyResult:
    return StrategyResult(pairs=pairs)


# Sync feed/flush behavior --------------------------------------------------


def test_feed_complete_placeholder_in_one_chunk() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    out1 = d.feed("hello [NAME_001] world")
    out2 = d.flush()
    assert out1 + out2 == "hello alice world"


def test_feed_placeholder_split_across_two_chunks() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    out1 = d.feed("hello [NAME")
    out2 = d.feed("_001] world")
    out3 = d.flush()
    assert out1 + out2 + out3 == "hello alice world"


def test_feed_placeholder_split_across_three_chunks() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    pieces = [d.feed(c) for c in ["hello [NA", "ME_", "001] bye"]]
    pieces.append(d.flush())
    assert "".join(pieces) == "hello alice bye"


def test_feed_split_one_char_at_a_time() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    pieces = [d.feed(c) for c in "hello [NAME_001] bye"]
    pieces.append(d.flush())
    assert "".join(pieces) == "hello alice bye"


def test_feed_two_placeholders_back_to_back() -> None:
    d = StreamingDesanitizer(
        _mapping(("alice", "[NAME_1]"), ("bob", "[NAME_2]"))
    )
    out = d.feed("[NAME_1][NAME_2]")
    out += d.flush()
    assert out == "alicebob"


def test_feed_no_placeholders_passthrough() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    pieces = [d.feed(c) for c in ["foo ", "bar ", "baz"]]
    pieces.append(d.flush())
    assert "".join(pieces) == "foo bar baz"


def test_feed_empty_chunks_are_safe() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    pieces = [d.feed(c) for c in ["", "[NAME_001]", ""]]
    pieces.append(d.flush())
    assert "".join(pieces) == "alice"


def test_flush_returns_remaining_buffer() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    early = d.feed("hi")
    tail = d.flush()
    assert early + tail == "hi"


def test_flush_idempotent() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    d.feed("hi")
    d.flush()
    assert d.flush() == ""


def test_feed_after_flush_raises() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    d.flush()
    with pytest.raises(RuntimeError, match="after flush"):
        d.feed("more")


def test_empty_mapping_passthrough() -> None:
    d = StreamingDesanitizer(_mapping())
    out = d.feed("hello world")
    out += d.flush()
    assert out == "hello world"


def test_length_descending_replacement() -> None:
    """A longer placeholder must replace before its prefix counterpart."""
    d = StreamingDesanitizer(
        _mapping(("alice cooper", "[NAME_LONG]"), ("alice", "[NAME]"))
    )
    out = d.feed("[NAME_LONG] vs [NAME]")
    out += d.flush()
    assert out == "alice cooper vs alice"


def test_placeholder_at_end_of_last_chunk() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    out = d.feed("intro [NAME_001]")
    out += d.flush()
    assert out == "intro alice"


def test_partial_placeholder_at_stream_end_is_emitted_verbatim() -> None:
    """If stream ends mid-placeholder, the partial bytes flush as-is."""
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    out = d.feed("hello [NAME")
    out += d.flush()
    assert out == "hello [NAME"


# Streaming async iterator interface ----------------------------------------


async def _async_iter(items: list[str]) -> AsyncIterator[str]:
    for item in items:
        yield item


async def test_stream_async_iterator() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    chunks = [c async for c in d.stream(_async_iter(["hello [NAME", "_001] bye"]))]
    assert "".join(chunks) == "hello alice bye"


async def test_stream_empty_iterator() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    chunks = [c async for c in d.stream(_async_iter([]))]
    assert chunks == []


async def test_stream_single_char_chunks() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    src = list("intro [NAME_001] outro")
    chunks = [c async for c in d.stream(_async_iter(src))]
    assert "".join(chunks) == "intro alice outro"


async def test_stream_no_placeholders() -> None:
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_001]")))
    chunks = [c async for c in d.stream(_async_iter(["foo", " bar", " baz"]))]
    assert "".join(chunks) == "foo bar baz"


async def test_stream_multiple_placeholders_with_split_boundary() -> None:
    d = StreamingDesanitizer(
        _mapping(("alice", "[NAME_1]"), ("bob", "[NAME_2]"))
    )
    chunks = [
        c async for c in d.stream(_async_iter(["hi [NAM", "E_1] and [NAME", "_2] done"]))
    ]
    assert "".join(chunks) == "hi alice and bob done"
