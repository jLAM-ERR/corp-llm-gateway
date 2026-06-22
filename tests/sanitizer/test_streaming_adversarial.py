"""Adversarial / edge-case tests for SseStreamDesanitizer.

Covers framing integrity, malformed inputs, non-text delta types, block
edge-cases, wire-format variants, ensure_ascii, usage pass-through, and
empty/no-op paths not exercised by the happy-path suite.
"""

from __future__ import annotations

import json

import pytest

from corp_llm_gateway.sanitizer import SseStreamDesanitizer, StrategyResult
from tests.sanitizer.test_streaming import (
    _MSG_DELTA,
    _MSG_START,
    _MSG_STOP,
    _PING,
    ANTHROPIC_SSE_FIXTURE,
    _cb_start,
    _cb_stop,
    _delta,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mapping(*pairs: tuple[str, str]) -> StrategyResult:
    return StrategyResult(pairs=pairs)


def _collect_bytes(sse: SseStreamDesanitizer, events: list[bytes]) -> list[bytes]:
    out: list[bytes] = []
    for ev in events:
        out.extend(sse.feed(ev))  # type: ignore[arg-type]
    out.extend(sse.flush())
    return out


def _collect_str(sse: SseStreamDesanitizer, events: list[str]) -> list[str]:
    out: list[str] = []
    for ev in events:
        out.extend(sse.feed(ev))  # type: ignore[arg-type]
    out.extend(sse.flush())
    return out


def _data_obj(chunk: bytes) -> dict | None:
    """Return parsed JSON from the data: line, or None if absent / not JSON."""
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        if line.startswith("data:"):
            payload = line[5:].lstrip()
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return None
    return None


# ---------------------------------------------------------------------------
# 1. Framing integrity invariant
# ---------------------------------------------------------------------------


def test_framing_integrity_every_data_line_is_valid_json() -> None:
    """Every emitted data: line in a realistic split-placeholder stream parses as JSON."""
    sse = SseStreamDesanitizer(_mapping(("user@example.com", "[EMAIL_001]")))
    out = _collect_bytes(sse, list(ANTHROPIC_SSE_FIXTURE))
    for chunk in out:
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].lstrip()
            if payload == "[DONE]":
                continue
            try:
                json.loads(payload)
            except json.JSONDecodeError as exc:
                pytest.fail(f"data: line is not valid JSON: {payload!r} — {exc}")


def test_framing_integrity_original_reconstructed_after_split() -> None:
    """Text reconstruction: joined delta.text values contain the original, not the placeholder."""
    sse = SseStreamDesanitizer(_mapping(("user@example.com", "[EMAIL_001]")))
    out = _collect_bytes(sse, list(ANTHROPIC_SSE_FIXTURE))
    text_parts: list[str] = []
    for chunk in out:
        obj = _data_obj(chunk)
        if obj and obj.get("type") == "content_block_delta":
            delta = obj.get("delta", {})
            if delta.get("type") == "text_delta":
                text_parts.append(delta["text"])
    full = "".join(text_parts)
    assert "user@example.com" in full, f"original not restored: {full!r}"
    assert "[EMAIL_001]" not in full, f"placeholder leaked: {full!r}"


# ---------------------------------------------------------------------------
# 2. Malformed / non-JSON data: lines
# ---------------------------------------------------------------------------


def test_malformed_data_line_passes_through_unchanged() -> None:
    """An event with a non-JSON data: line must not raise; it passes through byte-identical."""
    bad_event = b"event: content_block_delta\ndata: NOT JSON AT ALL\n\n"
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [bad_event])
    assert bad_event in out


def test_done_sentinel_passes_through_unchanged() -> None:
    """data: [DONE] (OpenAI end-of-stream sentinel) must pass through without raising."""
    done_event = b"data: [DONE]\n\n"
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [done_event])
    assert done_event in out


def test_done_sentinel_after_openai_content_flushes_desanitizer() -> None:
    """[DONE] after OpenAI content deltas must trigger OpenAI desanitizer flush
    and wrap the tail in a valid choices SSE event, not emit raw bytes.

    This is a regression guard for the bug where the hold-back tail emitted
    by StreamingDesanitizer.flush() is passed directly to self._encode()
    instead of being wrapped in a data:{choices:[{delta:{content:...}}]} event.
    """
    openai_delta = b'data: {"choices": [{"delta": {"content": "[N1]"}}]}\n\n'
    done_event = b"data: [DONE]\n\n"
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [openai_delta, done_event])
    # Every non-[DONE] data: line must be valid JSON with a choices envelope.
    for chunk in out:
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].lstrip()
            if payload == "[DONE]":
                continue
            obj = json.loads(payload)  # must not raise
            assert "choices" in obj, (
                f"tail emitted as raw bytes without choices envelope: {chunk!r}"
            )
    # [DONE] must still be present.
    combined = b"".join(out).decode("utf-8")
    assert "[DONE]" in combined
    # The placeholder must be fully restored across all choices chunks.
    text_parts = []
    for chunk in out:
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if line.startswith("data:"):
                payload = line[5:].lstrip()
                if payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                    if "choices" in obj:
                        delta = obj["choices"][0].get("delta", {})
                        if isinstance(delta.get("content"), str):
                            text_parts.append(delta["content"])
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
    assert "alice" in "".join(text_parts), f"original not restored: {''.join(text_parts)!r}"


def test_empty_data_line_passes_through() -> None:
    """An event with data: <empty> must not raise and passes through."""
    empty_event = b"data: \n\n"
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    # Must not raise.
    out = _collect_bytes(sse, [empty_event])
    assert len(out) >= 1


def test_event_with_no_data_line_passes_through() -> None:
    """A bare event line with no data: line (e.g. ping style) passes through."""
    bare_event = b"event: ping\n\n"
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [bare_event])
    assert bare_event in out


# ---------------------------------------------------------------------------
# 3. Non-text delta types (tool_use / thinking) pass through unchanged
# ---------------------------------------------------------------------------


def _input_json_delta_event(index: int = 0, partial_json: str = '{"k":') -> bytes:
    obj = {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "input_json_delta", "partial_json": partial_json},
    }
    return b"event: content_block_delta\ndata: " + json.dumps(obj).encode() + b"\n\n"


def _thinking_delta_event(index: int = 0, thinking: str = "let me think") -> bytes:
    obj = {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "thinking_delta", "thinking": thinking},
    }
    return b"event: content_block_delta\ndata: " + json.dumps(obj).encode() + b"\n\n"


def _tool_use_block_start(index: int = 1) -> bytes:
    obj = {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "tool_use", "id": "tu1", "name": "bash", "input": {}},
    }
    return b"event: content_block_start\ndata: " + json.dumps(obj).encode() + b"\n\n"


def test_input_json_delta_passes_through_byte_identical() -> None:
    """input_json_delta must never be fed to the text desanitizer; byte-identical out."""
    ev = _input_json_delta_event(partial_json='{"name": "alice"}')
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [ev])
    # Should pass through unchanged — alice in partial_json must NOT be replaced.
    combined = b"".join(out)
    obj = _data_obj(combined)
    if obj and obj.get("type") == "content_block_delta":
        delta = obj.get("delta", {})
        assert delta.get("type") == "input_json_delta"
        assert "alice" in delta.get("partial_json", "")
    else:
        # Entire original event must appear verbatim in output.
        assert ev in out or ev in combined


def test_thinking_delta_passes_through_byte_identical() -> None:
    """thinking_delta must not be rewritten by the desanitizer."""
    ev = _thinking_delta_event(thinking="alice is the user")
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [ev])
    combined = b"".join(out).decode("utf-8")
    assert "alice is the user" in combined
    assert "[N1]" not in combined


def test_input_json_delta_after_tool_use_block_start_unchanged() -> None:
    """A tool_use content_block_start followed by input_json_delta: no desanitizer for it."""
    events = [
        _tool_use_block_start(1),
        _input_json_delta_event(1, partial_json='{"key": "value"}'),
    ]
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, events)
    combined = b"".join(out).decode("utf-8")
    # The partial_json must be intact.
    assert '"key": "value"' in combined or "key" in combined


def test_non_text_content_block_start_no_desanitizer_created() -> None:
    """content_block_start for a tool_use block must NOT create a StreamingDesanitizer.

    If one is incorrectly created, a subsequent content_block_stop will
    flush it — that must not crash even if it does create one.
    """
    events = [
        _tool_use_block_start(1),
        _input_json_delta_event(1, partial_json="{}"),
        _cb_stop(1),
    ]
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    # Must not raise.
    out = _collect_bytes(sse, events)
    assert len(out) >= 1


# ---------------------------------------------------------------------------
# 4. Block edge cases
# ---------------------------------------------------------------------------


def test_placeholder_held_until_block_stop_then_flushed() -> None:
    """The desanitizer tail is flushed BEFORE content_block_stop arrives.

    When a placeholder is entirely held in the StreamingDesanitizer's
    internal buffer (because the whole [N1] fit within the hold window),
    content_block_stop must trigger a flush that emits a delta before the stop.
    """
    mapping = _mapping(("alice", "[NAME_001]"))
    ev = _delta("[NAME_001]", index=0)
    sse = SseStreamDesanitizer(mapping)
    out = _collect_bytes(sse, [_cb_start(0), ev, _cb_stop(0)])

    # Find the stop event position.
    stop_pos = next((i for i, c in enumerate(out) if b"content_block_stop" in c), None)
    # There must be at least one content_block_delta emitted before stop.
    delta_before_stop = any(b"content_block_delta" in out[i] for i in range(stop_pos or 0))
    # Reconstruct all delta text (before and at/after stop).
    all_delta_text = "".join(
        _data_obj(c)["delta"]["text"]
        for c in out
        if b"content_block_delta" in c
        and _data_obj(c) is not None
        and _data_obj(c).get("type") == "content_block_delta"
    )
    assert "alice" in all_delta_text, f"placeholder not restored; out={out!r}"
    assert "[NAME_001]" not in all_delta_text
    assert delta_before_stop, "tail flush delta must arrive before content_block_stop"


def test_content_block_delta_with_no_preceding_start_passes_through() -> None:
    """A content_block_delta with no preceding content_block_start must not crash.

    The desanitizer will have no StreamingDesanitizer for that index and
    must pass the event through unchanged (the ds-is-None guard).
    """
    ev = _delta("[N1]", index=99)
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    # No _cb_start(99) sent — must not raise.
    out = _collect_bytes(sse, [ev])
    # Event passes through unchanged.
    assert any(b"content_block_delta" in c for c in out)


def test_two_text_blocks_each_placeholder_restored_independently() -> None:
    """Two text blocks (index 0 and index 1) each restore their own placeholder."""
    mapping = _mapping(("alice", "[N1]"), ("bob", "[N2]"))
    events = [
        _cb_start(0),
        _delta("[N", 0),
        _delta("1]", 0),
        _cb_stop(0),
        _cb_start(1),
        _delta("[N", 1),
        _delta("2]", 1),
        _cb_stop(1),
    ]
    sse = SseStreamDesanitizer(mapping)
    out = _collect_bytes(sse, events)
    delta_texts = [
        _data_obj(c)["delta"]["text"]
        for c in out
        if b"content_block_delta" in c
        and _data_obj(c) is not None
        and _data_obj(c).get("type") == "content_block_delta"
    ]
    joined = "".join(delta_texts)
    assert "alice" in joined
    assert "bob" in joined
    assert "[N1]" not in joined
    assert "[N2]" not in joined


def test_two_text_blocks_no_runtime_error_on_second_block() -> None:
    """Opening a second content block after the first must not raise RuntimeError."""
    mapping = _mapping(("x", "[X]"), ("y", "[Y]"))
    events = [
        _cb_start(0),
        _delta("[X]", 0),
        _cb_stop(0),
        _cb_start(1),
        _delta("[Y]", 1),
        _cb_stop(1),
    ]
    sse = SseStreamDesanitizer(mapping)
    out = _collect_bytes(sse, events)  # must not raise RuntimeError
    assert len(out) >= 1


# ---------------------------------------------------------------------------
# 5. Wire-form variants
# ---------------------------------------------------------------------------


def test_str_input_returns_str_output() -> None:
    """When feed() receives str, every returned chunk is also str."""
    start = _cb_start().decode()
    ev = _delta("[N1]").decode()
    stop = _cb_stop().decode()
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_str(sse, [start, ev, stop])
    for chunk in out:
        assert isinstance(chunk, str), f"expected str, got {type(chunk)}: {chunk!r}"


def test_str_input_placeholder_restored() -> None:
    """str-mode feed also restores placeholders."""
    start = _cb_start().decode()
    ev = _delta("[N1]").decode()
    stop = _cb_stop().decode()
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_str(sse, [start, ev, stop])
    text = "".join(
        json.loads(line[5:].lstrip())["delta"]["text"]
        for chunk in out
        for line in chunk.splitlines()
        if line.startswith("data:")
        and "content_block_delta" in chunk
        and json.loads(line[5:].lstrip()).get("type") == "content_block_delta"
    )
    assert "alice" in text


def test_crlf_event_separators_accepted_no_exception() -> None:
    """\\r\\n\\r\\n event boundaries must be handled without raising.

    The StreamingDesanitizer hold-back may split "alice" across two
    content_block_delta events; assert on the RECONSTRUCTED text, not
    the raw bytes blob.
    """
    ev = (
        b"event: content_block_delta\r\n"
        b'data: {"type": "content_block_delta", "index": 0,'
        b' "delta": {"type": "text_delta", "text": "[N1]"}}\r\n\r\n'
    )
    start = (
        b"event: content_block_start\r\n"
        b'data: {"type": "content_block_start", "index": 0,'
        b' "content_block": {"type": "text", "text": ""}}\r\n\r\n'
    )
    stop = b'event: content_block_stop\r\ndata: {"type": "content_block_stop", "index": 0}\r\n\r\n'
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [start, ev, stop])
    # Reconstruct text from all content_block_delta chunks.
    text_parts = [
        _data_obj(c)["delta"]["text"]
        for c in out
        if b"content_block_delta" in c
        and _data_obj(c) is not None
        and _data_obj(c).get("type") == "content_block_delta"
    ]
    restored = "".join(text_parts)
    assert "alice" in restored, f"placeholder not restored: {restored!r}"
    assert "[N1]" not in restored


def test_sse_event_split_across_two_feeds_correct_output() -> None:
    """One SSE event split at its midpoint across two feed() calls still desanitizes."""
    ev = _delta("[N1]", index=0)
    mid = len(ev) // 2
    part1, part2 = ev[:mid], ev[mid:]
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [_cb_start(), part1, part2, _cb_stop()])
    delta_chunks = [c for c in out if b"content_block_delta" in c]
    texts = [
        _data_obj(c)["delta"]["text"]
        for c in delta_chunks
        if _data_obj(c) and _data_obj(c).get("type") == "content_block_delta"
    ]
    assert "alice" in "".join(texts)
    assert "[N1]" not in "".join(texts)


def test_sse_event_split_at_first_byte_across_feeds() -> None:
    """Event split at byte 1 (extreme early split) is still handled.

    The StreamingDesanitizer hold-back may spread 'hello' across two
    content_block_delta events; reconstruct from deltas, not raw bytes.
    """
    ev = _delta("hello")
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [_cb_start(), ev[:1], ev[1:], _cb_stop()])
    text_parts = [
        _data_obj(c)["delta"]["text"]
        for c in out
        if b"content_block_delta" in c
        and _data_obj(c) is not None
        and _data_obj(c).get("type") == "content_block_delta"
    ]
    assert "hello" in "".join(text_parts)


def test_multibyte_cyrillic_original_not_corrupted() -> None:
    """A multi-byte Cyrillic original split across two byte chunks must not corrupt."""
    cyrillic = "Привет"  # 6 chars, each 2 bytes in UTF-8
    ev_bytes = (
        b"event: content_block_delta\n"
        b'data: {"type": "content_block_delta", "index": 0,'
        b' "delta": {"type": "text_delta", "text": "' + cyrillic.encode("utf-8") + b'"}}\n\n'
    )
    # Split in the middle of the UTF-8 bytes.
    split = len(ev_bytes) // 2
    part1, part2 = ev_bytes[:split], ev_bytes[split:]
    sse = SseStreamDesanitizer(_mapping())
    out = _collect_bytes(sse, [_cb_start(), part1, part2, _cb_stop()])
    combined = b"".join(out).decode("utf-8")
    assert cyrillic in combined


# ---------------------------------------------------------------------------
# 6. ensure_ascii=False — non-ASCII originals are not escaped
# ---------------------------------------------------------------------------


def test_non_ascii_original_emitted_as_utf8_not_escaped() -> None:
    """A Cyrillic original is emitted as raw UTF-8 characters, not \\uXXXX escapes."""
    original = "Алиса"
    placeholder = "[NAME_001]"
    ev = _delta(placeholder, index=0)
    sse = SseStreamDesanitizer(_mapping((original, placeholder)))
    out = _collect_bytes(sse, [_cb_start(), ev, _cb_stop()])
    for chunk in out:
        if b"content_block_delta" not in chunk:
            continue
        # The raw UTF-8 bytes of the Cyrillic name must appear in the chunk.
        assert original.encode("utf-8") in chunk, f"Cyrillic original was JSON-escaped: {chunk!r}"
        # Confirm no \\u escape for the first Cyrillic char (U+0410).
        assert b"\\u0410" not in chunk


def test_non_ascii_original_data_line_parses_as_json() -> None:
    """After non-ASCII substitution, the data: line is still valid JSON."""
    original = "Алиса"
    placeholder = "[NAME_001]"
    ev = _delta(placeholder, index=0)
    sse = SseStreamDesanitizer(_mapping((original, placeholder)))
    out = _collect_bytes(sse, [_cb_start(), ev, _cb_stop()])
    for chunk in out:
        for line in chunk.decode("utf-8").splitlines():
            if line.startswith("data:"):
                payload = line[5:].lstrip()
                if payload == "[DONE]":
                    continue
                parsed = json.loads(payload)  # must not raise
                if parsed.get("type") == "content_block_delta":
                    assert parsed["delta"]["text"] == original


# ---------------------------------------------------------------------------
# 7. usage untouched — message_delta with usage passes byte-identical
# ---------------------------------------------------------------------------


def test_message_delta_usage_passes_byte_identical() -> None:
    """message_delta with usage block must pass through byte-identical."""
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [_MSG_DELTA])
    assert _MSG_DELTA in out


def test_message_delta_with_custom_usage_byte_identical() -> None:
    """A message_delta with custom token counts is not modified."""
    usage_event = (
        b"event: message_delta\n"
        b'data: {"type": "message_delta", "delta": {"stop_reason": "max_tokens"},'
        b' "usage": {"input_tokens": 99, "output_tokens": 42}}\n\n'
    )
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [usage_event])
    assert usage_event in out


# ---------------------------------------------------------------------------
# 8. Empty / no-op paths
# ---------------------------------------------------------------------------


def test_flush_with_nothing_fed_returns_empty_list() -> None:
    """flush() on a fresh SseStreamDesanitizer with no input must return []."""
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    result = sse.flush()
    assert result == []


def test_empty_mapping_bytes_pass_through_byte_identical() -> None:
    """Empty mapping: all bytes events must pass through unchanged."""
    sse = SseStreamDesanitizer(_mapping())
    events = [_MSG_START, _cb_start(), _delta("hello world"), _cb_stop(), _MSG_STOP]
    out = _collect_bytes(sse, events)
    for ev in events:
        assert ev in out, f"event not found byte-identical in output: {ev!r}"


def test_flush_called_twice_does_not_raise() -> None:
    """Calling flush() twice must not raise; second call is idempotent."""
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    _collect_bytes(sse, [_cb_start(), _delta("[N1]"), _cb_stop()])
    first = sse.flush()
    second = sse.flush()
    # both flush() calls return lists; the second is empty (idempotent)
    assert isinstance(first, list)
    assert isinstance(second, list)


def test_feed_after_complete_stream_is_safe() -> None:
    """Feeding more events after a complete stream (including flush) must not raise."""
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    _collect_bytes(sse, [_cb_start(), _delta("hello"), _cb_stop(), _MSG_STOP])
    # flush the whole stream
    sse.flush()
    # feeding more must not raise (though results may be odd)
    extra = sse.feed(_PING)
    assert isinstance(extra, list)


def test_empty_bytes_feed_returns_empty_list() -> None:
    """Feeding b'' must not crash and returns []."""
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    result = sse.feed(b"")
    assert result == []


def test_empty_str_feed_returns_empty_list() -> None:
    """Feeding '' (empty str) must not crash and returns []."""
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    result = sse.feed("")
    assert result == []


def test_ping_passes_through_byte_identical() -> None:
    """ping event must always pass through byte-identical."""
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [_PING])
    assert _PING in out


def test_message_stop_passes_through_byte_identical() -> None:
    """message_stop must pass through byte-identical."""
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [_MSG_STOP])
    assert _MSG_STOP in out


# ---------------------------------------------------------------------------
# 9. Framing: emitted chunks are individually parseable
# ---------------------------------------------------------------------------


def test_each_emitted_bytes_chunk_is_self_contained_sse() -> None:
    """Each emitted bytes chunk that contains a data: line must individually parse as JSON.

    Guards against multi-event chunks where JSON from one event bleeds into another.
    """
    sse = SseStreamDesanitizer(_mapping(("user@corp.com", "[EMAIL_001]")))
    events = [
        _MSG_START,
        _cb_start(0),
        _delta(" ["),
        _delta("EMAIL"),
        _delta("_"),
        _delta("001"),
        _delta("]"),
        _cb_stop(0),
        _MSG_STOP,
    ]
    out = _collect_bytes(sse, events)
    for chunk in out:
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if line.startswith("data:"):
                payload = line[5:].lstrip()
                if payload == "[DONE]":
                    continue
                try:
                    json.loads(payload)
                except json.JSONDecodeError as exc:
                    pytest.fail(f"chunk data: line is not valid JSON: {payload!r} — {exc}")


# ---------------------------------------------------------------------------
# 10. OpenAI SSE path (choices[].delta.content)
# ---------------------------------------------------------------------------


def test_openai_sse_bytes_placeholder_not_emitted_in_choices_chunks() -> None:
    """OpenAI-format SSE bytes: the placeholder [N1] must not appear in any
    choices delta content field.  The full restoration is gated by the
    [DONE]-flush bug (see test_done_sentinel_after_openai_content_flushes_desanitizer).
    """

    def _openai_delta(content: str) -> bytes:
        obj = {"choices": [{"delta": {"content": content}}]}
        return b"data: " + json.dumps(obj).encode() + b"\n\n"

    events = [
        _openai_delta("[N"),
        _openai_delta("1]"),
        b"data: [DONE]\n\n",
    ]
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, events)
    for chunk in out:
        obj = _data_obj(chunk)
        if obj and "choices" in obj:
            choices = obj.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                if isinstance(delta.get("content"), str):
                    assert "[N1]" not in delta["content"], (
                        f"placeholder leaked into choices chunk: {chunk!r}"
                    )
    # [DONE] must still be present.
    assert any(b"[DONE]" in c for c in out)


def test_openai_sse_non_text_choice_passes_through() -> None:
    """An OpenAI chunk with no delta.content (e.g. finish reason) passes through unchanged."""
    finish_event = b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [finish_event])
    assert finish_event in out


# ---------------------------------------------------------------------------
# 11. Anthropic end-of-stream flush tail is valid SSE (no content_block_stop)
# ---------------------------------------------------------------------------


def test_anthropic_truncated_flush_tail_emitted_as_valid_content_block_delta() -> None:
    """Held tail emitted by flush() on a truncated Anthropic stream is a valid SSE event.

    When a placeholder is held in the block desanitizer's buffer and the stream
    ends without content_block_stop, flush() must emit the tail wrapped in a
    proper content_block_delta SSE event (data: JSON), not as raw text.
    """
    ev = _delta("[NAME_001]")
    sse = SseStreamDesanitizer(_mapping(("alice", "[NAME_001]")))
    out: list[bytes] = []
    for chunk in [_cb_start(), ev]:
        out.extend(sse.feed(chunk))  # type: ignore[arg-type]
    # No content_block_stop — truncated stream.
    out.extend(sse.flush())

    # Every data: line in the output must be valid JSON.
    for chunk in out:
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].lstrip()
            if payload == "[DONE]":
                continue
            try:
                json.loads(payload)
            except json.JSONDecodeError as exc:
                pytest.fail(f"flush() tail emitted as raw non-JSON data: {payload!r} — {exc}")

    # The original must be present in the delta text fields.
    all_text = ""
    for chunk in out:
        obj = _data_obj(chunk)
        if obj and obj.get("type") == "content_block_delta":
            delta = obj.get("delta", {})
            if delta.get("type") == "text_delta":
                all_text += delta.get("text", "")
    assert "alice" in all_text, f"original not in flush tail: {all_text!r}"


def test_openai_truncated_flush_tail_emitted_as_valid_choices_event() -> None:
    """Held tail from OpenAI desanitizer emitted by flush() (no [DONE]) is valid SSE.

    When the stream ends without [DONE] and the OpenAI desanitizer has a
    held-back tail, flush() must wrap it in a choices SSE event, not raw text.
    """

    def _openai_delta(content: str) -> bytes:
        obj = {"choices": [{"delta": {"content": content}}]}
        return b"data: " + json.dumps(obj).encode() + b"\n\n"

    # Feed a split placeholder; no [DONE] — truncated OpenAI stream.
    sse = SseStreamDesanitizer(_mapping(("alice", "[NAME_001]")))
    out: list[bytes] = []
    for chunk in [_openai_delta("[NAME"), _openai_delta("_001]")]:
        out.extend(sse.feed(chunk))  # type: ignore[arg-type]
    out.extend(sse.flush())

    for chunk in out:
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].lstrip()
            if payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError as exc:
                pytest.fail(f"flush() OpenAI tail emitted as raw non-JSON: {payload!r} — {exc}")
            assert "choices" in obj, f"flush() tail has no choices envelope: {chunk!r}"

    all_text = "".join(
        chunk.decode("utf-8", errors="replace") for chunk in out if b"choices" in chunk
    )
    assert "alice" in all_text


# ---------------------------------------------------------------------------
# 12. Streaming tool_use input_json_delta desanitization
# ---------------------------------------------------------------------------


def _tool_use_start(index: int = 1, name: str = "bash") -> bytes:
    obj = {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "tool_use", "id": "tu1", "name": name, "input": {}},
    }
    return b"event: content_block_start\ndata: " + json.dumps(obj).encode() + b"\n\n"


def _input_json_delta(index: int = 1, partial_json: str = "") -> bytes:
    obj = {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "input_json_delta", "partial_json": partial_json},
    }
    return b"event: content_block_delta\ndata: " + json.dumps(obj).encode() + b"\n\n"


def _thinking_block_start(index: int = 0) -> bytes:
    obj = {
        "type": "content_block_start",
        "index": index,
        "content_block": {"type": "thinking", "thinking": ""},
    }
    return b"event: content_block_start\ndata: " + json.dumps(obj).encode() + b"\n\n"


def _thinking_delta(index: int = 0, thinking: str = "") -> bytes:
    obj = {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "thinking_delta", "thinking": thinking},
    }
    return b"event: content_block_delta\ndata: " + json.dumps(obj).encode() + b"\n\n"


def _signature_delta(index: int = 0, signature: str = "sig") -> bytes:
    obj = {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "signature_delta", "signature": signature},
    }
    return b"event: content_block_delta\ndata: " + json.dumps(obj).encode() + b"\n\n"


def _collect_partial_jsons(out: list[bytes]) -> list[str]:
    """Collect all partial_json values from input_json_delta chunks in output."""
    result = []
    for chunk in out:
        obj = _data_obj(chunk)
        if obj and obj.get("type") == "content_block_delta":
            delta = obj.get("delta", {})
            if delta.get("type") == "input_json_delta":
                pj = delta.get("partial_json", "")
                if pj:
                    result.append(pj)
    return result


def test_tool_use_input_json_delta_placeholder_split_across_two_deltas() -> None:
    """[EMAIL_001] split across two input_json_delta fragments is desanitized; output valid JSON."""
    mapping = _mapping(("a@b.com", "[EMAIL_001]"))
    events = [
        _tool_use_start(1),
        _input_json_delta(1, '{"to": "[EMA'),
        _input_json_delta(1, 'IL_001]"}'),
        _cb_stop(1),
    ]
    sse = SseStreamDesanitizer(mapping)
    out = _collect_bytes(sse, events)
    parts = _collect_partial_jsons(out)
    reassembled = "".join(parts)
    assert "[EMAIL_001]" not in reassembled, f"placeholder leaked: {reassembled!r}"
    assert "a@b.com" in reassembled, f"original not restored: {reassembled!r}"
    parsed = json.loads(reassembled)
    assert parsed == {"to": "a@b.com"}


def test_tool_use_input_json_delta_json_escape_correctness() -> None:
    """An original with a double-quote is JSON-string-escaped so partial_json stays valid JSON."""
    mapping = _mapping(('a"b', "[NAME_001]"))
    events = [
        _tool_use_start(1),
        _input_json_delta(1, '{"x":"[NAME_001]"}'),
        _cb_stop(1),
    ]
    sse = SseStreamDesanitizer(mapping)
    out = _collect_bytes(sse, events)
    parts = _collect_partial_jsons(out)
    reassembled = "".join(parts)
    assert "[NAME_001]" not in reassembled, f"placeholder leaked: {reassembled!r}"
    parsed = json.loads(reassembled)
    assert parsed == {"x": 'a"b'}, f"unexpected parse result: {parsed!r}"


def test_tool_use_input_json_delta_no_placeholders_passes_through() -> None:
    """A tool_use block with no placeholders in input_json_delta passes through unchanged."""
    mapping = _mapping(("alice", "[NAME_001]"))
    partial = '{"cmd": "ls -la"}'
    events = [
        _tool_use_start(1),
        _input_json_delta(1, partial),
        _cb_stop(1),
    ]
    sse = SseStreamDesanitizer(mapping)
    out = _collect_bytes(sse, events)
    parts = _collect_partial_jsons(out)
    reassembled = "".join(parts)
    assert reassembled == partial, f"unexpected rewrite: {reassembled!r}"


def test_thinking_delta_with_placeholder_passes_through_verbatim() -> None:
    """A thinking_delta containing a placeholder is NOT rewritten (thinking blocks are signed)."""
    placeholder = "[EMAIL_001]"
    mapping = _mapping(("a@b.com", placeholder))
    ev = _thinking_delta(0, f"the user email is {placeholder}")
    sse = SseStreamDesanitizer(mapping)
    out = _collect_bytes(sse, [_thinking_block_start(0), ev, _cb_stop(0)])
    combined = b"".join(out).decode("utf-8")
    assert placeholder in combined, f"placeholder was rewritten in thinking_delta: {combined!r}"
    assert "a@b.com" not in combined, f"original leaked into thinking_delta: {combined!r}"


def test_signature_delta_passes_through_byte_identical() -> None:
    """signature_delta (part of thinking block signing) must pass through byte-identical."""
    sig_ev = _signature_delta(0, "SomeSig==")
    sse = SseStreamDesanitizer(_mapping(("alice", "[N1]")))
    out = _collect_bytes(sse, [_thinking_block_start(0), sig_ev, _cb_stop(0)])
    assert sig_ev in out, "signature_delta was not passed through unchanged"


def test_mixed_text_and_tool_use_blocks_both_desanitize() -> None:
    """Text block (index 0) and tool_use block (index 1) both desanitize in one message."""
    mapping = _mapping(("alice", "[NAME_001]"), ("a@b.com", "[EMAIL_001]"))
    events = [
        _cb_start(0),
        _delta("[NAME_001]", 0),
        _cb_stop(0),
        _tool_use_start(1),
        _input_json_delta(1, '{"to":"[EMAIL_001]"}'),
        _cb_stop(1),
    ]
    sse = SseStreamDesanitizer(mapping)
    out = _collect_bytes(sse, events)

    # Collect text delta text
    text_parts = []
    json_parts = []
    for chunk in out:
        obj = _data_obj(chunk)
        if obj and obj.get("type") == "content_block_delta":
            delta = obj.get("delta", {})
            if delta.get("type") == "text_delta":
                text_parts.append(delta.get("text", ""))
            elif delta.get("type") == "input_json_delta":
                pj = delta.get("partial_json", "")
                if pj:
                    json_parts.append(pj)

    text_joined = "".join(text_parts)
    json_joined = "".join(json_parts)

    assert "alice" in text_joined, f"text block not desanitized: {text_joined!r}"
    assert "[NAME_001]" not in text_joined
    assert "a@b.com" in json_joined, f"tool_use block not desanitized: {json_joined!r}"
    assert "[EMAIL_001]" not in json_joined
    # tool_use partial_json must also be valid JSON
    parsed = json.loads(json_joined)
    assert parsed == {"to": "a@b.com"}
