from __future__ import annotations

import codecs
import json
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable

from corp_llm_gateway.sanitizer.placeholder import sort_placeholders_by_descending_length
from corp_llm_gateway.sanitizer.strategies import StrategyResult

# Regex that matches a complete SSE event boundary (two consecutive newlines).
_SSE_BOUNDARY = re.compile(r"\r\n\r\n|\n\n|\r\r")


class StreamingDesanitizer:
    """Stateful de-sanitizer for SSE streaming chunks.

    Plan ref: M1-8. The corp-LLM proxy streams response chunks; placeholders
    may span chunk boundaries (e.g. `[NAME` in one chunk, `_001]` in the
    next). We buffer the last `max_placeholder_length - 1` characters so any
    in-flight placeholder of up to `max_placeholder_length` is fully visible
    when its closing bytes arrive.

    Replacement order is length-descending (M1-9) so a longer placeholder
    can't be shadowed by a shorter prefix-match one.
    """

    def __init__(self, mapping: StrategyResult, escape: Callable[[str], str] | None = None) -> None:
        self._by_placeholder: dict[str, str] = {
            placeholder: (escape(original) if escape else original)
            for original, placeholder in mapping.pairs
        }
        self._sorted_placeholders: tuple[str, ...] = tuple(
            sort_placeholders_by_descending_length(self._by_placeholder)
        )
        self._max_len = max((len(p) for p in self._by_placeholder), default=0)
        self._buffer = ""
        self._flushed = False

    def feed(self, chunk: str) -> str:
        if self._flushed:
            raise RuntimeError("StreamingDesanitizer.feed called after flush")
        self._buffer += chunk
        self._buffer = self._replace_all(self._buffer)

        if self._max_len <= 1:
            safe = self._buffer
            self._buffer = ""
            return safe

        hold = self._max_len - 1
        if len(self._buffer) <= hold:
            return ""
        safe = self._buffer[:-hold]
        self._buffer = self._buffer[-hold:]
        return safe

    def flush(self) -> str:
        if self._flushed:
            return ""
        self._flushed = True
        remaining = self._replace_all(self._buffer)
        self._buffer = ""
        return remaining

    async def stream(self, chunks: AsyncIterable[str]) -> AsyncIterator[str]:
        async for chunk in chunks:
            out = self.feed(chunk)
            if out:
                yield out
        tail = self.flush()
        if tail:
            yield tail

    def _replace_all(self, text: str) -> str:
        for placeholder in self._sorted_placeholders:
            text = text.replace(placeholder, self._by_placeholder[placeholder])
        return text


class OpenAiToolCallDesanitizer:
    """Per-tool-call desanitizer for streamed OpenAI ``tool_calls[].function.arguments``.

    OpenAI streams a tool call's ``arguments`` as JSON-text fragments across
    chunks, keyed by ``tool_calls[].index``; a placeholder may straddle two
    fragments. One :class:`StreamingDesanitizer` per index reassembles it, and
    originals are JSON-string-escaped so the reversed value stays valid inside
    the raw arguments string (mirrors the Anthropic ``input_json_delta`` path).
    """

    def __init__(self, mapping: StrategyResult) -> None:
        self._mapping = mapping
        self._by_index: dict[int, StreamingDesanitizer] = {}

    def feed(self, index: int, fragment: str) -> str:
        ds = self._by_index.get(index)
        if ds is None:
            ds = StreamingDesanitizer(self._mapping, escape=_json_string_escape)
            self._by_index[index] = ds
        return ds.feed(fragment)

    def flush(self) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        for idx, ds in self._by_index.items():
            tail = ds.flush()
            if tail:
                out.append((idx, tail))
        self._by_index.clear()
        return out


class SseStreamDesanitizer:
    """De-sanitize an Anthropic or OpenAI SSE byte stream.

    Each ``feed`` call accepts one or more raw SSE bytes/str chunks (as
    produced by litellm's Anthropic passthrough).  The class accumulates
    a byte buffer, splits on SSE event boundaries (``\\n\\n`` /
    ``\\r\\n\\r\\n`` / ``\\r\\r``), and rewrites only model text fields:

    - Anthropic ``content_block_delta`` with ``delta.type == "text_delta"``
    - OpenAI ``choices[N].delta.content``

    Every other event (``message_start``, ``ping``, ``message_delta``,
    ``message_stop``, ``content_block_start``, ``content_block_stop``,
    ``error``, ``[DONE]``) passes through byte-identical.

    Placeholder assembly across delta boundaries uses one
    ``StreamingDesanitizer`` per Anthropic text content block (flushed at
    ``content_block_stop``), preserving the M1-9 length-descending rule.
    """

    def __init__(self, mapping: StrategyResult) -> None:
        self._mapping = mapping
        # True when the source stream emits bytes; False for str.
        self._source_is_bytes: bool | None = None
        # Incremental UTF-8 decoder shared across all feeds.
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        # Raw byte accumulator; always bytes internally.
        self._buf: bytes = b""
        # Per-block (index → desanitizer) for Anthropic text/tool_use content blocks.
        self._block_desanitizers: dict[int, StreamingDesanitizer] = {}
        # Tracks block type ("text" or "tool_use") per index for correct flush delta type.
        self._block_types: dict[int, str] = {}
        # Single desanitizer for OpenAI streams (one content stream total).
        self._openai_desanitizer: StreamingDesanitizer | None = None
        # OpenAI tool_calls arguments stream, keyed by tool_calls[].index.
        self._openai_tool_calls = OpenAiToolCallDesanitizer(mapping)
        # Legacy OpenAI function_call arguments stream (singular, no index).
        self._openai_function_call: StreamingDesanitizer | None = None

    # ------------------------------------------------------------------ public

    def feed(self, chunk: bytes | str) -> list[bytes | str]:
        if self._source_is_bytes is None:
            self._source_is_bytes = isinstance(chunk, bytes)
        raw = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
        decoded = self._utf8.decode(raw, final=False)
        self._buf += decoded.encode("utf-8")
        return self._drain()

    def flush(self) -> list[bytes | str]:
        # Decode any remaining bytes held by the incremental decoder.
        tail_decoded = self._utf8.decode(b"", final=True)
        self._buf += tail_decoded.encode("utf-8")
        out: list[bytes | str] = []
        # Emit any complete events accumulated so far.
        out.extend(self._drain())
        # Flush any still-active block desanitizers (defensive: truncated stream
        # with no content_block_stop, or placeholders held in their buffer).
        for idx, ds in list(self._block_desanitizers.items()):
            tail = ds.flush()
            if tail:
                btype = self._block_types.get(idx, "text")
                if btype == "tool_use":
                    ev = _make_input_json_delta_event(idx, tail)
                else:
                    ev = _make_text_delta_event(idx, tail)
                out.append(self._encode(ev + "\n\n"))
            del self._block_desanitizers[idx]
        self._block_types.clear()
        if self._openai_desanitizer is not None:
            oi_tail = self._openai_desanitizer.flush()
            if oi_tail:
                out.append(self._encode(_make_openai_delta_event(oi_tail)))
            self._openai_desanitizer = None
        if self._openai_function_call is not None:
            fc_tail = self._openai_function_call.flush()
            if fc_tail:
                out.append(self._encode(_make_openai_function_call_delta_event(fc_tail)))
            self._openai_function_call = None
        for tc_idx, tc_tail in self._openai_tool_calls.flush():
            out.append(self._encode(_make_openai_tool_call_delta_event(tc_idx, tc_tail)))
        # Emit whatever partial (incomplete) SSE event remains in the buffer.
        if self._buf:
            partial = self._buf
            self._buf = b""
            out.append(self._encode(partial.decode("utf-8", errors="replace")))
        return out

    # ----------------------------------------------------------------- private

    def _drain(self) -> list[bytes | str]:
        """Split accumulated buffer into complete SSE events and process each."""
        out: list[bytes | str] = []
        while True:
            text = self._buf.decode("utf-8", errors="replace")
            m = _SSE_BOUNDARY.search(text)
            if m is None:
                break
            event_text = text[: m.end()]
            self._buf = text[m.end() :].encode("utf-8")
            out.extend(self._process_event(event_text))
        return out

    def _process_event(self, event_text: str) -> list[bytes | str]:
        """Rewrite model-text fields in one complete SSE event; pass others unchanged."""
        data_str = _extract_data_line(event_text)
        if data_str is None:
            # No data line at all (e.g. bare ping line) — pass through.
            return [self._encode(event_text)]
        if data_str == "[DONE]":
            # End-of-stream sentinel (OpenAI) — flush OpenAI desanitizer first.
            result: list[bytes | str] = []
            if self._openai_desanitizer is not None:
                oi_tail = self._openai_desanitizer.flush()
                if oi_tail:
                    result.append(self._encode(_make_openai_delta_event(oi_tail)))
                self._openai_desanitizer = None
            if self._openai_function_call is not None:
                fc_tail = self._openai_function_call.flush()
                if fc_tail:
                    result.append(self._encode(_make_openai_function_call_delta_event(fc_tail)))
                self._openai_function_call = None
            for tc_idx, tc_tail in self._openai_tool_calls.flush():
                result.append(self._encode(_make_openai_tool_call_delta_event(tc_idx, tc_tail)))
            result.append(self._encode(event_text))
            return result
        try:
            obj = json.loads(data_str)
        except json.JSONDecodeError:
            return [self._encode(event_text)]

        ev_type = obj.get("type") if isinstance(obj, dict) else None

        # Anthropic: content_block_start — fresh desanitizer for text and tool_use blocks.
        if ev_type == "content_block_start":
            cb = obj.get("content_block") or {}
            if isinstance(cb, dict):
                idx = int(obj.get("index", 0))
                if cb.get("type") == "text":
                    self._block_desanitizers[idx] = StreamingDesanitizer(self._mapping)
                    self._block_types[idx] = "text"
                elif cb.get("type") == "tool_use":
                    # JSON-escape originals so substitution inside partial_json is valid JSON.
                    self._block_desanitizers[idx] = StreamingDesanitizer(
                        self._mapping, escape=_json_string_escape
                    )
                    self._block_types[idx] = "tool_use"
                # thinking / redacted_thinking / other blocks: no desanitizer (pass through).
            return [self._encode(event_text)]

        # Anthropic: content_block_delta — rewrite text_delta and input_json_delta.
        if ev_type == "content_block_delta":
            delta = obj.get("delta") or {}
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                idx = int(obj.get("index", 0))
                text_in = delta.get("text", "")
                if not isinstance(text_in, str):
                    return [self._encode(event_text)]
                ds = self._block_desanitizers.get(idx)
                if ds is None:
                    # Desanitizer not started (e.g. non-text block) — pass through.
                    return [self._encode(event_text)]
                rewritten = ds.feed(text_in)
                if not rewritten:
                    # Held back — emit nothing for this event.
                    return []
                new_obj = dict(obj)
                new_obj["delta"] = {**delta, "text": rewritten}
                boundary = _boundary_from(event_text)
                return [self._encode(_rebuild_event(event_text, new_obj, boundary))]
            if isinstance(delta, dict) and delta.get("type") == "input_json_delta":
                idx = int(obj.get("index", 0))
                pj = delta.get("partial_json", "")
                if not isinstance(pj, str):
                    return [self._encode(event_text)]
                ds = self._block_desanitizers.get(idx)
                if ds is None:
                    return [self._encode(event_text)]
                rewritten = ds.feed(pj)
                if not rewritten:
                    return []
                new_obj = dict(obj)
                new_obj["delta"] = {**delta, "partial_json": rewritten}
                boundary = _boundary_from(event_text)
                return [self._encode(_rebuild_event(event_text, new_obj, boundary))]

        # Anthropic: content_block_stop — flush that block's desanitizer tail first.
        if ev_type == "content_block_stop":
            idx = int(obj.get("index", 0))
            result = []
            ds = self._block_desanitizers.pop(idx, None)
            btype = self._block_types.pop(idx, "text")
            if ds is not None:
                tail = ds.flush()
                if tail:
                    tail_ev = (
                        _make_input_json_delta_event(idx, tail)
                        if btype == "tool_use"
                        else _make_text_delta_event(idx, tail)
                    )
                    boundary = _boundary_from(event_text)
                    result.append(self._encode(tail_ev + boundary))
            result.append(self._encode(event_text))
            return result

        # OpenAI: choices[N].delta — desanitize content, tool_calls, and legacy
        # function_call in the SAME delta. A held-back/empty content must NOT drop a
        # tool_call/function_call riding in the same event (id/name/args would be lost).
        # n>1 choices: only choices[0].delta is rewritten (parity with the content path).
        if isinstance(obj, dict) and "choices" in obj:
            choices = obj.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                first_choice = choices[0]
                delta = first_choice.get("delta")
                if isinstance(delta, dict) and (
                    isinstance(delta.get("content"), str)
                    or isinstance(delta.get("tool_calls"), list)
                    or isinstance(delta.get("function_call"), dict)
                ):
                    new_delta = dict(delta)
                    content_present = isinstance(delta.get("content"), str)
                    tool_or_fn = False

                    if content_present:
                        if self._openai_desanitizer is None:
                            self._openai_desanitizer = StreamingDesanitizer(self._mapping)
                        new_delta["content"] = self._openai_desanitizer.feed(delta["content"])

                    if isinstance(delta.get("tool_calls"), list):
                        new_calls: list[object] = []
                        for tc in delta["tool_calls"]:
                            fn = tc.get("function") if isinstance(tc, dict) else None
                            if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                                idx = coerce_tool_index(tc.get("index", 0))
                                if idx is None:
                                    new_calls.append(tc)
                                    continue
                                rewritten = self._openai_tool_calls.feed(idx, fn["arguments"])
                                new_calls.append({**tc, "function": {**fn, "arguments": rewritten}})
                            else:
                                new_calls.append(tc)
                        new_delta["tool_calls"] = new_calls
                        tool_or_fn = True

                    fc = delta.get("function_call")
                    if isinstance(fc, dict) and isinstance(fc.get("arguments"), str):
                        if self._openai_function_call is None:
                            self._openai_function_call = StreamingDesanitizer(
                                self._mapping, escape=_json_string_escape
                            )
                        new_delta["function_call"] = {
                            **fc,
                            "arguments": self._openai_function_call.feed(fc["arguments"]),
                        }
                        tool_or_fn = True

                    # Drop only a content-ONLY delta whose content is fully held back.
                    if content_present and not tool_or_fn and not new_delta.get("content"):
                        return []
                    new_first = {**first_choice, "delta": new_delta}
                    new_choices = [new_first, *list(choices[1:])]
                    new_obj = {**obj, "choices": new_choices}
                    boundary = _boundary_from(event_text)
                    return [self._encode(_rebuild_event(event_text, new_obj, boundary))]

        # Everything else (message_start, ping, message_delta, message_stop,
        # error, non-text blocks) — byte-identical pass-through.
        return [self._encode(event_text)]

    def _encode(self, text: str | bytes) -> bytes | str:
        """Re-emit in the same type (bytes vs str) as the source stream."""
        if isinstance(text, bytes):
            # Already bytes; return as-is if source is bytes, else decode.
            return text if self._source_is_bytes else text.decode("utf-8", errors="replace")
        # text is str
        if self._source_is_bytes:
            return text.encode("utf-8")
        return text


# ---------------------------------------------------------------------------
# SSE helpers (module-private)
# ---------------------------------------------------------------------------


def coerce_tool_index(value: object) -> int | None:
    """Coerce a streamed tool_call ``index`` to int, or None when unusable.

    A null/garbage index must not crash the whole response stream — the caller
    skips that fragment instead of aborting."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _extract_data_line(event_text: str) -> str | None:
    """Return the payload from the first ``data:`` line, or None."""
    for line in event_text.splitlines():
        if line.startswith("data:"):
            return line[5:].lstrip(" ")
    return None


def _boundary_from(event_text: str) -> str:
    """Return the trailing boundary (two newlines) used in the event."""
    m = _SSE_BOUNDARY.search(event_text)
    return m.group(0) if m else "\n\n"


def _rebuild_event(original_event: str, new_obj: dict, boundary: str) -> str:
    """Reconstruct an SSE event preserving any ``event:`` line, rewriting ``data:``."""
    lines = original_event.rstrip("\r\n").splitlines()
    out_lines: list[str] = []
    for line in lines:
        if line.startswith("data:"):
            out_lines.append("data: " + json.dumps(new_obj, ensure_ascii=False))
        else:
            out_lines.append(line)
    sep = "\r\n" if "\r\n" in boundary else "\n"
    return sep.join(out_lines) + boundary


def _json_string_escape(s: str) -> str:
    """Escape a string for safe substitution INSIDE a JSON string literal
    (e.g. inside an input_json_delta fragment). json.dumps wraps + escapes;
    strip the surrounding quotes."""
    return json.dumps(s)[1:-1]


def _make_text_delta_event(index: int, text: str) -> str:
    """Build a bare ``content_block_delta`` SSE event string (no trailing boundary)."""
    obj = {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "text_delta", "text": text},
    }
    return "event: content_block_delta\ndata: " + json.dumps(obj, ensure_ascii=False)


def _make_input_json_delta_event(index: int, partial_json: str) -> str:
    """Build a bare ``content_block_delta`` SSE event for input_json_delta (no boundary)."""
    obj = {
        "type": "content_block_delta",
        "index": index,
        "delta": {"type": "input_json_delta", "partial_json": partial_json},
    }
    return "event: content_block_delta\ndata: " + json.dumps(obj, ensure_ascii=False)


def _make_openai_delta_event(content: str) -> str:
    """Build a complete OpenAI-format SSE event for a choices delta tail."""
    obj = {"choices": [{"delta": {"content": content}}]}
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _make_openai_tool_call_delta_event(index: int, arguments: str) -> str:
    """Build a complete OpenAI-format SSE event for a tool_calls arguments tail."""
    obj = {
        "choices": [
            {"delta": {"tool_calls": [{"index": index, "function": {"arguments": arguments}}]}}
        ]
    }
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _make_openai_function_call_delta_event(arguments: str) -> str:
    """Build a complete OpenAI-format SSE event for a legacy function_call args tail."""
    obj = {"choices": [{"delta": {"function_call": {"arguments": arguments}}}]}
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
