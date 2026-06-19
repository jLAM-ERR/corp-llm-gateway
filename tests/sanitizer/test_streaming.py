import json
from collections.abc import AsyncIterator

import pytest

from corp_llm_gateway.sanitizer import SseStreamDesanitizer, StrategyResult, StreamingDesanitizer


def _mapping(*pairs: tuple[str, str]) -> StrategyResult:
    return StrategyResult(pairs=pairs)


# ---------------------------------------------------------------------------
# Real Anthropic SSE fixture (captured from live litellm Anthropic passthrough)
# ---------------------------------------------------------------------------

# Each entry is one complete SSE event as bytes, matching the wire format.
# Placeholder [EMAIL_001] is split across five text_delta events (indices 3-7).
_MSG_START = (
    b"event: message_start\n"
    b'data: {"type": "message_start", "message": {"id": "msg_01",'
    b' "type": "message", "role": "assistant", "content": [],'
    b' "model": "claude-opus-4-7", "stop_reason": null,'
    b' "usage": {"input_tokens": 18, "output_tokens": 1}}}\n\n'
)
_CB_START = (
    b"event: content_block_start\n"
    b'data: {"type": "content_block_start", "index": 0,'
    b' "content_block": {"type": "text", "text": ""}}\n\n'
)
_MSG_DELTA = (
    b"event: message_delta\n"
    b'data: {"type": "message_delta",'
    b' "delta": {"stop_reason": "end_turn"},'
    b' "usage": {"input_tokens": 18, "output_tokens": 16}}\n\n'
)

_PING = b'event: ping\ndata: {"type": "ping"}\n\n'
_MSG_STOP = b'event: message_stop\ndata: {"type": "message_stop"}\n\n'


def _delta(text: str, index: int = 0) -> bytes:
    return (
        b"event: content_block_delta\n"
        + b'data: {"type": "content_block_delta", "index": '
        + str(index).encode()
        + b', "delta": {"type": "text_delta", "text": '
        + f'"{text}"}}}}\n\n'.encode()
    )


def _cb_start(index: int = 0) -> bytes:
    return (
        b"event: content_block_start\n"
        + b'data: {"type": "content_block_start", "index": '
        + str(index).encode()
        + b', "content_block": {"type": "text", "text": ""}}\n\n'
    )


def _cb_stop(index: int = 0) -> bytes:
    return (
        b"event: content_block_stop\n"
        + b'data: {"type": "content_block_stop", "index": '
        + str(index).encode()
        + b"}\n\n"
    )


ANTHROPIC_SSE_FIXTURE: tuple[bytes, ...] = (
    _MSG_START,
    _CB_START,
    _PING,
    _delta(" ["),
    _delta("EMAIL"),
    _delta("_"),
    _delta("001"),
    _delta("]"),
    _cb_stop(0),
    _MSG_DELTA,
    _MSG_STOP,
)


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


# ===========================================================================
# SseStreamDesanitizer tests
# ===========================================================================


def _collect(sse: SseStreamDesanitizer, events: list[bytes]) -> list[bytes]:
    out: list[bytes] = []
    for ev in events:
        out.extend(sse.feed(ev))  # type: ignore[arg-type]
    out.extend(sse.flush())
    return out


def _data_of(chunk: bytes) -> dict:
    """Parse the ``data:`` line from an SSE bytes event."""
    for line in chunk.decode().splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].lstrip())
    raise AssertionError(f"no data line in {chunk!r}")


# --- fixture loads ----------------------------------------------------------


def test_fixture_chunk_type_is_bytes() -> None:
    """Confirms the captured fixture uses bytes (the real wire type)."""
    for chunk in ANTHROPIC_SSE_FIXTURE:
        assert isinstance(chunk, bytes)


# --- placeholder split across deltas ---------------------------------------


def test_sse_placeholder_split_across_deltas_reassembled() -> None:
    """[EMAIL_001] arrives as 5 separate text_delta events; must be restored."""
    sse = SseStreamDesanitizer(_mapping(("user@example.com", "[EMAIL_001]")))
    out = _collect(sse, list(ANTHROPIC_SSE_FIXTURE))
    text_parts = []
    for chunk in out:
        try:
            obj = _data_of(chunk)
        except AssertionError:
            continue
        if obj.get("type") == "content_block_delta":
            delta = obj.get("delta", {})
            if delta.get("type") == "text_delta":
                text_parts.append(delta["text"])
    full_text = "".join(text_parts)
    assert "user@example.com" in full_text
    assert "[EMAIL_001]" not in full_text


# --- non-placeholder delta passes byte-identical ---------------------------


def test_sse_non_placeholder_delta_text_unchanged() -> None:
    """A content_block_delta with no placeholder must pass its text through unchanged."""
    ev = _delta("hello world")
    sse = SseStreamDesanitizer(_mapping(("alice", "[NAME_001]")))
    out = _collect(sse, [_cb_start(), ev, _cb_stop()])
    delta_chunks = [c for c in out if b"content_block_delta" in c]
    # StreamingDesanitizer may split output across multiple deltas due to holdback
    # buffering, but all text must be present and no placeholder introduced.
    total_text = "".join(_data_of(c)["delta"]["text"] for c in delta_chunks)
    assert total_text == "hello world"
    assert "[NAME_001]" not in total_text


# --- pass-through events are byte-identical --------------------------------


def test_sse_passthrough_events_byte_identical() -> None:
    """ping, message_delta, message_stop, message_start, content_block_start pass unchanged."""
    sse = SseStreamDesanitizer(_mapping(("alice", "[NAME_001]")))
    passthrough_types = {
        "ping", "message_delta", "message_stop",
        "message_start", "content_block_start",
    }
    out = _collect(sse, list(ANTHROPIC_SSE_FIXTURE))
    out_by_type: dict[str, bytes] = {}
    for chunk in out:
        try:
            obj = _data_of(chunk)
        except AssertionError:
            continue
        ev_type = obj.get("type")
        if ev_type in passthrough_types:
            out_by_type[ev_type] = chunk
    # Every passthrough type present in the fixture must come out byte-identical.
    for chunk in ANTHROPIC_SSE_FIXTURE:
        try:
            obj = _data_of(chunk)
        except AssertionError:
            continue
        ev_type = obj.get("type")
        if ev_type in passthrough_types:
            assert out_by_type.get(ev_type) == chunk, f"{ev_type} was altered"


def test_sse_message_delta_usage_unchanged() -> None:
    """message_delta carrying usage must not be altered."""
    sse = SseStreamDesanitizer(_mapping(("alice", "[NAME_001]")))
    out = _collect(sse, list(ANTHROPIC_SSE_FIXTURE))
    for chunk in out:
        try:
            obj = _data_of(chunk)
        except AssertionError:
            continue
        if obj.get("type") == "message_delta":
            assert obj["usage"]["input_tokens"] == 18
            assert obj["usage"]["output_tokens"] == 16


# --- event split across two feed() calls ------------------------------------


def test_sse_event_split_across_feeds() -> None:
    """One SSE event arriving in two byte chunks must be reassembled correctly."""
    ev = _delta("[NAME_001]")
    mid = len(ev) // 2
    part1, part2 = ev[:mid], ev[mid:]
    sse = SseStreamDesanitizer(_mapping(("alice", "[NAME_001]")))
    out = _collect(sse, [_cb_start(), part1, part2, _cb_stop()])
    delta_chunks = [c for c in out if b"content_block_delta" in c]
    text_parts = [_data_of(c)["delta"]["text"] for c in delta_chunks]
    assert "alice" in "".join(text_parts)
    assert "[NAME_001]" not in "".join(text_parts)


# --- \r\n\r\n separators ---------------------------------------------------


def test_sse_crlf_separators_accepted() -> None:
    """Events separated by \\r\\n\\r\\n must be processed correctly."""
    ev = (
        b"event: content_block_delta\r\n"
        b'data: {"type": "content_block_delta", "index": 0,'
        b' "delta": {"type": "text_delta", "text": "[NAME_001]"}}\r\n\r\n'
    )
    start = (
        b"event: content_block_start\r\n"
        b'data: {"type": "content_block_start", "index": 0,'
        b' "content_block": {"type": "text", "text": ""}}\r\n\r\n'
    )
    stop = (
        b"event: content_block_stop\r\n"
        b'data: {"type": "content_block_stop", "index": 0}\r\n\r\n'
    )
    sse = SseStreamDesanitizer(_mapping(("alice", "[NAME_001]")))
    out = _collect(sse, [start, ev, stop])
    delta_chunks = [c for c in out if b"content_block_delta" in c]
    text_parts = [_data_of(c)["delta"]["text"] for c in delta_chunks]
    assert "alice" in "".join(text_parts)


# --- multi-byte UTF-8 char split across byte chunks ------------------------


def test_sse_multibyte_utf8_split_across_chunks_not_corrupted() -> None:
    """A 4-byte emoji split across two byte feed() calls must not corrupt output."""
    emoji = "\U0001f600"  # 4 bytes in UTF-8
    text = "hi " + emoji + " bye"
    ev_bytes = (
        b"event: content_block_delta\n"
        b'data: {"type": "content_block_delta", "index": 0,'
        b' "delta": {"type": "text_delta", "text": "'
        + text.encode("utf-8")
        + b'"}}\n\n'
    )
    # Split at the first byte of the emoji (inside the UTF-8 sequence).
    emoji_start = ev_bytes.index(b"\xf0")
    part1 = ev_bytes[:emoji_start + 1]
    part2 = ev_bytes[emoji_start + 1:]
    sse = SseStreamDesanitizer(_mapping())
    out = _collect(sse, [_cb_start(), part1, part2, _cb_stop()])
    all_text = "".join(
        _data_of(c)["delta"]["text"]
        for c in out
        if b"content_block_delta" in c
    )
    assert emoji in all_text
    assert "hi" in all_text


# --- two text content blocks (indices 0 and 1) -----------------------------


def test_sse_two_text_blocks_no_runtime_error() -> None:
    """Two text content blocks (index 0 and index 1) both desanitized; no crash."""
    mapping = _mapping(("alice", "[N1]"), ("bob", "[N2]"))
    events = [
        _cb_start(0),
        _delta("[N1]", index=0),
        _cb_stop(0),
        _cb_start(1),
        _delta("[N2]", index=1),
        _cb_stop(1),
    ]
    sse = SseStreamDesanitizer(mapping)
    out = _collect(sse, events)
    delta_chunks = [c for c in out if b"content_block_delta" in c]
    texts = [_data_of(c)["delta"]["text"] for c in delta_chunks]
    joined = "".join(texts)
    assert "alice" in joined
    assert "bob" in joined
    assert "[N1]" not in joined
    assert "[N2]" not in joined


# --- stream ends without content_block_stop --------------------------------


def test_sse_flush_emits_held_text_on_truncated_stream() -> None:
    """If stream ends without content_block_stop, flush() emits any held text."""
    ev = _delta("[NAME_001]")
    sse = SseStreamDesanitizer(_mapping(("alice", "[NAME_001]")))
    out: list[bytes] = []
    for chunk in [_cb_start(), ev]:
        out.extend(sse.feed(chunk))  # type: ignore[arg-type]
    # No content_block_stop sent — truncated stream.
    out.extend(sse.flush())
    all_text = ""
    for c in out:
        if b"data:" in c:
            try:
                obj = _data_of(c)
                if obj.get("type") == "content_block_delta":
                    all_text += obj["delta"].get("text", "")
            except (AssertionError, KeyError, json.JSONDecodeError):
                pass
    assert "alice" in all_text


# --- str source type is returned as str ------------------------------------


def test_sse_str_input_returns_str_output() -> None:
    """When feed() receives str, output must also be str."""
    ev_str = _delta("hello").decode()
    start_str = _cb_start().decode()
    stop_str = _cb_stop().decode()
    sse = SseStreamDesanitizer(_mapping(("alice", "[NAME_001]")))
    out: list[str] = []
    for ev in [start_str, ev_str, stop_str]:
        out.extend(sse.feed(ev))  # type: ignore[arg-type]
    out.extend(sse.flush())
    for chunk in out:
        assert isinstance(chunk, str)
