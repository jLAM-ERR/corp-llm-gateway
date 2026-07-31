import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from corp_llm_gateway.sanitizer import (
    ResponsesStreamDesanitizer,
    SseStreamDesanitizer,
    StrategyResult,
    StreamingDesanitizer,
)


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
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_1]"), ("bob", "[NAME_2]")))
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
    d = StreamingDesanitizer(_mapping(("alice cooper", "[NAME_LONG]"), ("alice", "[NAME]")))
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


# Defect #6: bare-alias boundary anchor, hold-back window sizing ------------


def test_bare_alias_split_across_chunks_does_not_corrupt_containing_identifier() -> None:
    """`MY_` and the alias arrive in separate feeds; the underscore boundary
    context must survive the split so the anchor still blocks the match."""
    d = StreamingDesanitizer(
        _mapping(("SecretPlace", "[LOCATION_007]"), ("SecretPlace", "LOCATION_007"))
    )
    out = d.feed("const MY_")
    out += d.feed("LOCATION_007 = 1;")
    out += d.flush()
    assert out == "const MY_LOCATION_007 = 1;"


def test_bare_alias_boundary_char_survives_hold_back_flush_across_chunk_split() -> None:
    """Pin that the hold-back window (`_max_len - 1`, sized off the longest
    BRACKETED placeholder — 13 chars for `[LOCATION_007]`) actually retains
    the one boundary character (`_`) the anchor needs, even once padding is
    long enough to force an intermediate flush before the alias arrives."""
    d = StreamingDesanitizer(
        _mapping(("SecretPlace", "[LOCATION_007]"), ("SecretPlace", "LOCATION_007"))
    )
    filler = "x" * 40
    out = d.feed(f"const {filler}MY_")
    out += d.feed("LOCATION_007 = 1;")
    out += d.flush()
    assert out == f"const {filler}MY_LOCATION_007 = 1;"


def test_bare_alias_replaced_across_chunk_split_at_legitimate_word_boundary() -> None:
    """Positive control for the hold-back test above: a genuine stand-alone
    bare alias, split across the same padded chunk boundary, must still be
    restored — the anchor fix must not be over-conservative."""
    d = StreamingDesanitizer(
        _mapping(("SecretPlace", "[LOCATION_007]"), ("SecretPlace", "LOCATION_007"))
    )
    filler = "x" * 40
    out = d.feed(f"const {filler} see ")
    out += d.feed("LOCATION_007 done")
    out += d.flush()
    assert out == f"const {filler} see SecretPlace done"


def test_bare_no_bracket_sibling_placeholder_at_max_len_not_corrupted_in_one_feed() -> None:
    """MAJOR 5 fallout: a bare (non-bracketed) placeholder with NO bracketed
    sibling (an operator replace.md replacement, e.g. `PARTNER-A`) can be
    exactly `max_len` chars long — the model-coined-alias mechanism always has
    a 2-char-longer `[FAMILY_NNN]` sibling padding the hold-back window, but a
    rule replacement has no such sibling. Fed whole in ONE feed() call
    (immediately followed by flush(), i.e. genuinely nothing else coming),
    the trailing occurrence's sentinel-deferral (MAJOR 3) must not let the
    fixed-size hold-back window chop it mid-placeholder."""
    d = StreamingDesanitizer(_mapping(("acme", "PARTNER-A")))
    out = d.feed("PARTNER-A and PARTNER-A and PARTNER-A")
    out += d.flush()
    assert out == "acme and acme and acme"


def test_bare_single_char_placeholder_deferred_at_buffer_tail_still_restores() -> None:
    """A bare placeholder of length <= 1 hit the "nothing to hold back" fast
    path unconditionally, draining and clearing the buffer even when the
    trailing occurrence was deferred (not yet confirmed complete) — the
    deferred, unreplaced placeholder was then emitted as-is and never
    retried, instead of surviving into flush() where it correctly restores."""
    d = StreamingDesanitizer(_mapping(("Ivanov", "X")))
    out = d.feed("hello X")
    out += d.flush()
    assert out == "hello Ivanov"


def test_bare_alias_boundary_defeated_by_chunk_split_end_of_buffer() -> None:
    """MAJOR 3: the exact review repro. Unary (whole text at once) correctly
    leaves `NAME_001Suffix` untouched (the anchor's trailing lookahead sees
    `S`, an identifier char, right after the alias). Split across a chunk
    boundary so the alias's real end coincides with the CURRENT buffer's end,
    end-of-buffer must NOT be treated as "definitely nothing follows" — a
    later chunk can still extend it past the boundary."""
    d = StreamingDesanitizer(_mapping(("Alice Smith", "[NAME_001]"), ("Alice Smith", "NAME_001")))
    out = d.feed("class NAME_001")
    out += d.feed("Suffix: pass")
    out += d.flush()
    assert out == "class NAME_001Suffix: pass"


def test_bare_alias_boundary_respected_when_split_lands_on_real_word_boundary() -> None:
    """Companion positive control: the same split point, but the next chunk
    genuinely starts with a non-identifier character — the alias must still
    be restored (the fix must not become over-conservative and never fire)."""
    d = StreamingDesanitizer(_mapping(("Alice Smith", "[NAME_001]"), ("Alice Smith", "NAME_001")))
    out = d.feed("class NAME_001")
    out += d.feed(": pass")
    out += d.flush()
    assert out == "class Alice Smith: pass"


def test_bare_alias_ending_in_hold_back_sentinel_char_not_corrupted_across_chunks() -> None:
    """A rule replacement (bare, no bracketed sibling) can legitimately end in
    any character, including one a hold-back scheme might otherwise reserve
    as a synthetic marker. A partial occurrence split across a chunk boundary
    must never be treated as complete just because the split itself happens
    to produce that trailing character."""
    d = StreamingDesanitizer(_mapping(("ProjectPhoenix", "matrix")))
    out = d.feed("the matri")
    out += d.feed("cal build of matrix")
    out += d.flush()
    assert out == "the matrical build of ProjectPhoenix"


def test_bare_alias_ending_in_hold_back_sentinel_char_restored_in_one_feed() -> None:
    """Positive control for the same trailing character: a genuine, complete
    occurrence fed and flushed with nothing else pending must still restore."""
    d = StreamingDesanitizer(_mapping(("ProjectPhoenix", "matrix")))
    out = d.feed("see matrix here")
    out += d.flush()
    assert out == "see ProjectPhoenix here"


# ---- Round-4 CRITICAL 3: the LEADING boundary is defeated by truncation ----


def test_bare_alias_leading_boundary_survives_buffer_truncation_across_chunks() -> None:
    """Exact review repro. One-shot correctly leaves `XNAME_001` untouched (the
    anchor's leading lookbehind sees `X`, an identifier char, right before the
    alias). Split so the `X` gets flushed out of the buffer (truncated away)
    before the alias itself is re-scanned in a later feed() — losing that
    context must not make the anchor wrongly accept the match."""
    d = StreamingDesanitizer(_mapping(("John Smith", "[NAME_001]"), ("John Smith", "NAME_001")))
    out = d.feed("class XNAME_001 x")
    out += d.feed(" rest")
    out += d.flush()
    assert out == "class XNAME_001 x rest"


def test_bare_alias_leading_boundary_still_restores_at_real_word_boundary_after_truncation() -> (
    None
):
    """Positive control: same truncation-inducing split, but the character
    preceding the alias is a genuine non-identifier boundary — the fix must
    not become over-conservative and block a legitimate restoration."""
    d = StreamingDesanitizer(_mapping(("John Smith", "[NAME_001]"), ("John Smith", "NAME_001")))
    out = d.feed("class NAME_001 x")
    out += d.feed(" rest")
    out += d.flush()
    assert out == "class John Smith x rest"


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
    d = StreamingDesanitizer(_mapping(("alice", "[NAME_1]"), ("bob", "[NAME_2]")))
    chunks = [c async for c in d.stream(_async_iter(["hi [NAM", "E_1] and [NAME", "_2] done"]))]
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
        "ping",
        "message_delta",
        "message_stop",
        "message_start",
        "content_block_start",
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
    stop = b'event: content_block_stop\r\ndata: {"type": "content_block_stop", "index": 0}\r\n\r\n'
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
        b' "delta": {"type": "text_delta", "text": "' + text.encode("utf-8") + b'"}}\n\n'
    )
    # Split at the first byte of the emoji (inside the UTF-8 sequence).
    emoji_start = ev_bytes.index(b"\xf0")
    part1 = ev_bytes[: emoji_start + 1]
    part2 = ev_bytes[emoji_start + 1 :]
    sse = SseStreamDesanitizer(_mapping())
    out = _collect(sse, [_cb_start(), part1, part2, _cb_stop()])
    all_text = "".join(_data_of(c)["delta"]["text"] for c in out if b"content_block_delta" in c)
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


# --- Minor: ResponsesStreamDesanitizer must not bypass validation on failure -


class _FakeResponsesEvent:
    """Duck-typed stand-in for a litellm-yielded Pydantic Responses event."""

    def __init__(self, type_: str, **fields: Any) -> None:
        self.type = type_
        self._fields = fields
        for k, v in fields.items():
            setattr(self, k, v)

    def model_dump(self, mode: str = "python", exclude_none: bool = False) -> dict[str, Any]:
        return {"type": self.type, **self._fields}

    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> "_FakeResponsesEvent":
        fields = {k: v for k, v in payload.items() if k != "type"}
        return cls(payload["type"], **fields)

    def model_copy(self, *, update: dict[str, Any], deep: bool = True) -> "_FakeResponsesEvent":
        fields = {**self._fields, **{k: v for k, v in update.items() if k != "type"}}
        return _FakeResponsesEvent(update.get("type", self.type), **fields)


class _FakeResponsesEventValidateFails(_FakeResponsesEvent):
    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> "_FakeResponsesEventValidateFails":
        raise ValueError("simulated reconstruction failure")

    def model_copy(
        self, *, update: dict[str, Any], deep: bool = True
    ) -> "_FakeResponsesEventValidateFails":
        raise AssertionError(
            "must not fall through to an unvalidated model_copy after model_validate fails"
        )


class _FakeResponsesEventValidateFailsWithPayloadInMessage(_FakeResponsesEvent):
    """Stands in for a real pydantic ValidationError, whose message embeds
    the offending `input_value` — i.e. the payload that failed to validate,
    which at this call site is the already-DESANITIZED (original-bearing)
    event dict."""

    @classmethod
    def model_validate(
        cls, payload: dict[str, Any]
    ) -> "_FakeResponsesEventValidateFailsWithPayloadInMessage":
        raise ValueError(f"1 validation error for Foo\n  input_value={payload!r}")


def test_responses_stream_event_reconstruct_failure_does_not_bypass_validation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A model_validate failure on a typed Responses stream event must not
    silently degrade to an unvalidated model_copy — the exact class of
    degradation litellm_hook.py's _apply_reverse_to_response deliberately
    refuses (log + return the untouched original instead)."""
    import logging

    d = ResponsesStreamDesanitizer(_mapping(("alice", "[NAME_001]")))
    event = _FakeResponsesEventValidateFails(
        "response.completed", output=[{"content": "hi [NAME_001]"}]
    )

    with caplog.at_level(logging.WARNING):
        out = d.feed(event)

    assert out == [event]
    assert "reconstruct_failed" in caplog.text.lower()


def test_responses_stream_event_reconstruct_failure_log_has_no_original(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """M1-14: the reconstruct-failure log must never carry the original —
    the payload at this point is already desanitized, so a real pydantic
    ValidationError (which embeds the offending `input_value` in its own
    message) must not be logged with its exception details."""
    import logging

    d = ResponsesStreamDesanitizer(_mapping(("Ivanov", "[NAME_001]")))
    event = _FakeResponsesEventValidateFailsWithPayloadInMessage(
        "response.completed", output=[{"content": "hi [NAME_001]"}]
    )

    with caplog.at_level(logging.WARNING):
        out = d.feed(event)

    assert out == [event]
    assert "Ivanov" not in caplog.text


def test_responses_stream_event_reconstruct_success_still_restores() -> None:
    """Positive control: when model_validate succeeds, the event is restored
    and desanitized normally (the failure path must not become the norm)."""

    d = ResponsesStreamDesanitizer(_mapping(("alice", "[NAME_001]")))
    event = _FakeResponsesEvent("response.completed", output=[{"content": "hi [NAME_001]"}])

    out = d.feed(event)

    assert len(out) == 1
    assert out[0].output[0]["content"] == "hi alice"


class _FakeResponsesEventWithHiddenParams(_FakeResponsesEvent):
    """model_dump() never includes pydantic PrivateAttr fields (mirrors the
    real litellm.types.llms.openai.ResponseCompletedEvent._hidden_params) —
    model_validate() on the dump builds a BRAND NEW instance whose private
    attrs reset to this constructor default, losing whatever real value
    litellm attached to the ORIGINAL instance at runtime."""

    def __init__(self, type_: str, **fields: Any) -> None:
        super().__init__(type_, **fields)
        self._hidden_params: dict[str, Any] = {}


def test_responses_stream_event_reconstruct_success_restores_hidden_params() -> None:
    """Round-4 IMPORTANT 8: _restore_responses_event's successful
    model_validate() path dropped _hidden_params — the same defect I9 fixed
    at the unary _apply_reverse_to_response site, left at this sibling."""
    d = ResponsesStreamDesanitizer(_mapping(("alice", "[NAME_001]")))
    event = _FakeResponsesEventWithHiddenParams(
        "response.completed", output=[{"content": "hi [NAME_001]"}]
    )
    event._hidden_params = {"response_cost": 0.05}

    out = d.feed(event)

    assert len(out) == 1
    assert out[0].output[0]["content"] == "hi alice"
    assert out[0]._hidden_params == {"response_cost": 0.05}


# --- Minor: synthetic tail event must not replay a stale sequence_number ----


def test_responses_stream_synthetic_tail_event_uses_latest_sequence_number() -> None:
    """_event_metadata used to snapshot id fields (incl. sequence_number) only
    from the FIRST delta of a stream key — every later delta for that same
    key advances sequence_number, so a synthetic tail event built from the
    stale snapshot replayed an already-sent value instead of the latest one."""
    d = ResponsesStreamDesanitizer(_mapping(("alice", "[NAME_001]")))

    assert (
        d.feed(
            {
                "type": "response.output_text.delta",
                "item_id": "item_1",
                "sequence_number": 100,
                "delta": "x",
            }
        )
        == []
    )
    assert (
        d.feed(
            {
                "type": "response.output_text.delta",
                "item_id": "item_1",
                "sequence_number": 101,
                "delta": "y",
            }
        )
        == []
    )

    tail_events = d.flush()
    assert len(tail_events) == 1
    tail = json.loads(tail_events[0])
    assert tail["delta"] == "xy"
    assert tail["sequence_number"] == 101
