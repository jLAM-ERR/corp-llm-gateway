"""Content block walker for sanitization/desanitization.

Handles both str and list-of-blocks (Anthropic) content shapes,
plus OpenAI multimodal content-parts. Reusable across pre_call and
post_call with configurable rewrite callbacks.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from corp_llm_gateway.sanitizer.orchestrator import SanitizeResult

# Async sanitization function: str → SanitizeResult (runtime decoupled via Any).
SanitizeOne = Callable[[str], Awaitable["SanitizeResult"]]
# Sync desanitization function: placeholder text → original text.
ReverseOne = Callable[[str], str]

_MAX_JSON_DEPTH = 64


class ContentTooDeepError(Exception):
    pass


class UnsanitizableToolArgumentsError(Exception):
    """A tool-call ``arguments`` value has a shape we cannot scan (not str/dict/list).

    Raised instead of silently passing it through — this surface egresses to the
    upstream provider, so an unrecognized shape fails closed."""


async def _sanitize_json(
    value: Any, sanitize_one: SanitizeOne, _depth: int = 0
) -> tuple[Any, list[Any]]:
    """Recursively sanitize string leaves in a JSON-compatible value tree.

    Only string VALUES are rewritten — dict keys (tool-arg names) are preserved.
    Non-str scalars (int/float/bool/None) pass through unchanged.
    """
    if _depth > _MAX_JSON_DEPTH:
        raise ContentTooDeepError(f"input nesting exceeds {_MAX_JSON_DEPTH}")
    if isinstance(value, str):
        result = await sanitize_one(value)
        return result.sanitized_text, [result]
    if isinstance(value, dict):
        new_d: dict[str, Any] = {}
        results: list[Any] = []
        for k, v in value.items():
            nv, r = await _sanitize_json(v, sanitize_one, _depth + 1)
            new_d[k] = nv
            results.extend(r)
        return new_d, results
    if isinstance(value, list):
        new_l: list[Any] = []
        results = []
        for item in value:
            nv, r = await _sanitize_json(item, sanitize_one, _depth + 1)
            new_l.append(nv)
            results.extend(r)
        return new_l, results
    return value, []


def _desanitize_json(value: Any, reverse: ReverseOne, _depth: int = 0) -> Any:
    """Recursively desanitize string leaves in a JSON-compatible value tree.

    Only string VALUES are rewritten — dict keys are preserved.
    Non-str scalars pass through unchanged.
    """
    if _depth > _MAX_JSON_DEPTH:
        return value
    if isinstance(value, str):
        return reverse(value)
    if isinstance(value, dict):
        return {k: _desanitize_json(v, reverse, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_desanitize_json(item, reverse, _depth + 1) for item in value]
    return value


def _collect_json_text(value: Any, _depth: int = 0) -> list[str]:
    """Collect all string leaves from a JSON-compatible value tree."""
    if _depth > _MAX_JSON_DEPTH:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for v in value.values():
            out.extend(_collect_json_text(v, _depth + 1))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_collect_json_text(item, _depth + 1))
        return out
    return []


async def _sanitize_arguments(arguments: str, sanitize_one: SanitizeOne) -> tuple[str, list[Any]]:
    """Sanitize a JSON-encoded OpenAI ``function.arguments`` string.

    Parse → sanitize string leaves → re-serialize (mirrors ``tool_use.input``).
    If the string is not valid JSON, sanitize it whole as one leaf so a raw
    secret can never egress un-scanned (fail-safe)."""
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, ValueError):
        result = await sanitize_one(arguments)
        return result.sanitized_text, [result]
    new_value, results = await _sanitize_json(parsed, sanitize_one)
    return json.dumps(new_value, ensure_ascii=False), results


def _desanitize_arguments(arguments: str, reverse: ReverseOne) -> str:
    """Reverse placeholders inside a JSON-encoded ``function.arguments`` string.

    Parse → desanitize string leaves → re-serialize; ``json.dumps`` re-escapes
    so an original containing quotes/backslashes stays valid JSON. Falls back to
    a raw string reverse when the arguments are not valid JSON."""
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, ValueError):
        return reverse(arguments)
    return json.dumps(_desanitize_json(parsed, reverse), ensure_ascii=False)


def _collect_arguments_text(arguments: str) -> list[str]:
    """Collect the string leaves of a JSON-encoded ``function.arguments`` string.

    Mirrors ``_sanitize_arguments`` so the Stage-0/Stage-5 pre-scan sees exactly
    what will be sanitized (whole string when it is not valid JSON)."""
    try:
        parsed = json.loads(arguments)
    except (json.JSONDecodeError, ValueError):
        return [arguments]
    return _collect_json_text(parsed)


async def _sanitize_tool_arguments(
    arguments: Any, sanitize_one: SanitizeOne
) -> tuple[Any, list[Any]]:
    """Sanitize a tool-call ``arguments`` value of any shape.

    The OpenAI spec says ``arguments`` is a JSON-encoded string, but some clients
    send an already-parsed dict/list. Handle str via the arguments-string path and
    dict/list via the same recursion as ``tool_use.input``. ``None``/absent is no
    data; any other shape (bare scalar) is unrecognized and fails closed."""
    if isinstance(arguments, str):
        return await _sanitize_arguments(arguments, sanitize_one)
    if isinstance(arguments, (dict, list)):
        return await _sanitize_json(arguments, sanitize_one)
    if arguments is None:
        return arguments, []
    raise UnsanitizableToolArgumentsError(type(arguments).__name__)


def _desanitize_tool_arguments(arguments: Any, reverse: ReverseOne) -> Any:
    """Reverse placeholders in a tool-call ``arguments`` value of any shape."""
    if isinstance(arguments, str):
        return _desanitize_arguments(arguments, reverse)
    if isinstance(arguments, (dict, list)):
        return _desanitize_json(arguments, reverse)
    return arguments


def _collect_tool_arguments_text(arguments: Any) -> list[str]:
    """Collect the string leaves of a tool-call ``arguments`` value of any shape.

    Mirrors ``_sanitize_tool_arguments`` so the Stage-0/Stage-5 pre-scan sees
    exactly what will be sanitized."""
    if isinstance(arguments, str):
        return _collect_arguments_text(arguments)
    if isinstance(arguments, (dict, list)):
        return _collect_json_text(arguments)
    return []


def message_has_tool_calls(message: dict[str, Any]) -> bool:
    """True when a chat/Responses item carries sanitizable tool data."""
    if isinstance(message.get("tool_calls"), list) or isinstance(
        message.get("function_call"), dict
    ):
        return True
    item_type = message.get("type")
    if item_type == "function_call" and "arguments" in message:
        return True
    return item_type in {"function_call_output", "custom_tool_call_output"} and (
        "output" in message
    )


async def sanitize_tool_calls(
    message: dict[str, Any], sanitize_one: SanitizeOne
) -> tuple[dict[str, Any], list[Any]]:
    """Sanitize OpenAI message-level ``tool_calls[].function.arguments`` and legacy
    ``function_call.arguments``. Content is handled by ``sanitize_content`` — this
    only rewrites the tool-call argument spans. Returns (new_message, results)."""
    results: list[Any] = []
    new_message = dict(message)
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        new_calls: list[Any] = []
        for call in tool_calls:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict) and "arguments" in fn:
                new_args, r = await _sanitize_tool_arguments(fn["arguments"], sanitize_one)
                new_calls.append({**call, "function": {**fn, "arguments": new_args}})
                results.extend(r)
            else:
                new_calls.append(call)
        new_message["tool_calls"] = new_calls
    fc = message.get("function_call")
    if isinstance(fc, dict) and "arguments" in fc:
        new_args, r = await _sanitize_tool_arguments(fc["arguments"], sanitize_one)
        new_message["function_call"] = {**fc, "arguments": new_args}
        results.extend(r)

    # Responses API represents calls and their outputs as top-level input
    # items instead of message-level ``tool_calls``.
    item_type = message.get("type")
    if item_type == "function_call" and "arguments" in message:
        new_args, r = await _sanitize_tool_arguments(message["arguments"], sanitize_one)
        new_message["arguments"] = new_args
        results.extend(r)
    elif item_type in {"function_call_output", "custom_tool_call_output"} and ("output" in message):
        new_output, r = await _sanitize_json(message["output"], sanitize_one)
        new_message["output"] = new_output
        results.extend(r)
    return new_message, results


async def sanitize_message(
    message: dict[str, Any], sanitize_one: SanitizeOne
) -> tuple[dict[str, Any], list[Any]]:
    """Sanitize a full chat message: content blocks PLUS OpenAI tool-call arguments.

    Empty/absent content is skipped (so a tool-call-only assistant message is
    still processed for its arguments)."""
    results: list[Any] = []
    new_message = dict(message)
    content = message.get("content")
    if content is not None and content != "":
        new_content, content_results = await sanitize_content(content, sanitize_one)
        new_message["content"] = new_content
        results.extend(content_results)
    new_message, tc_results = await sanitize_tool_calls(new_message, sanitize_one)
    results.extend(tc_results)
    return new_message, results


def desanitize_tool_calls(message: dict[str, Any], reverse: ReverseOne) -> dict[str, Any]:
    """Reverse placeholders in a response message's ``tool_calls``/``function_call`` args."""
    new_message = dict(message)
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        new_calls: list[Any] = []
        for call in tool_calls:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict) and "arguments" in fn:
                new_args = _desanitize_tool_arguments(fn["arguments"], reverse)
                new_calls.append({**call, "function": {**fn, "arguments": new_args}})
            else:
                new_calls.append(call)
        new_message["tool_calls"] = new_calls
    fc = message.get("function_call")
    if isinstance(fc, dict) and "arguments" in fc:
        new_message["function_call"] = {
            **fc,
            "arguments": _desanitize_tool_arguments(fc["arguments"], reverse),
        }
    item_type = message.get("type")
    if item_type == "function_call" and "arguments" in message:
        new_message["arguments"] = _desanitize_tool_arguments(message["arguments"], reverse)
    elif item_type in {"function_call_output", "custom_tool_call_output"} and ("output" in message):
        new_message["output"] = _desanitize_json(message["output"], reverse)
    return new_message


def collect_tool_call_text(message: dict[str, Any]) -> list[str]:
    """Collect sanitizable text from a message's ``tool_calls``/``function_call`` args."""
    out: list[str] = []
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict) and "arguments" in fn:
                out.extend(_collect_tool_arguments_text(fn["arguments"]))
    fc = message.get("function_call")
    if isinstance(fc, dict) and "arguments" in fc:
        out.extend(_collect_tool_arguments_text(fc["arguments"]))
    item_type = message.get("type")
    if item_type == "function_call" and "arguments" in message:
        out.extend(_collect_tool_arguments_text(message["arguments"]))
    elif item_type in {"function_call_output", "custom_tool_call_output"} and ("output" in message):
        out.extend(_collect_json_text(message["output"]))
    return out


async def _sanitize_block(
    block: dict[str, Any],
    sanitize_one: SanitizeOne,
) -> tuple[dict[str, Any], list[Any]]:
    """Apply sanitize_one to a single content dict block.

    Shared by the list-item path and the bare-dict path so the rules
    live in exactly one place.
    """
    block_type = block.get("type")
    if block_type in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str):
        result = await sanitize_one(block["text"])
        return {**block, "text": result.sanitized_text}, [result]
    if block_type == "tool_result" and "content" in block:
        new_sub, sub_results = await sanitize_content(block["content"], sanitize_one)
        return {**block, "content": new_sub}, sub_results
    if block_type == "tool_use" and "input" in block:
        new_input, sub_results = await _sanitize_json(block["input"], sanitize_one)
        return {**block, "input": new_input}, sub_results
    if block_type == "document":
        new_block = dict(block)
        results: list[Any] = []
        for fld in ("title", "context"):
            v = new_block.get(fld)
            if isinstance(v, str) and v:
                r = await sanitize_one(v)
                new_block[fld] = r.sanitized_text
                results.append(r)
        src = new_block.get("source")
        if isinstance(src, dict):
            stype = src.get("type")
            if stype == "text" and isinstance(src.get("data"), str):
                r = await sanitize_one(src["data"])
                new_block["source"] = {**src, "data": r.sanitized_text}
                results.append(r)
            elif stype == "content" and "content" in src:
                new_sub, sub = await sanitize_content(src["content"], sanitize_one)
                new_block["source"] = {**src, "content": new_sub}
                results.extend(sub)
            # base64 / url sources: binary or out-of-scope → leave untouched
        return new_block, results
    # SECURITY: thinking/redacted_thinking blocks are intentionally passed through
    # unchanged. Anthropic SIGNS thinking blocks and rejects modified ones on
    # multi-turn replay — rewriting them would break conversations. The model only
    # ever sees placeholders anyway, so no originals are present to leak.
    # SECURITY: egress sanitization covers tool_use.input (string leaves, non-streaming).
    # Streaming tool_use desanitization (input_json_delta) is handled in streaming.py
    # via SseStreamDesanitizer with JSON-string-escaping of originals.
    # Image, image_url, unknown → pass through unchanged.
    return block, []


def _desanitize_block(block: dict[str, Any], reverse: ReverseOne) -> dict[str, Any]:
    """Apply reverse to a single content dict block.

    Shared by the list-item path and the bare-dict path.
    """
    block_type = block.get("type")
    if block_type in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str):
        return {**block, "text": reverse(block["text"])}
    if block_type == "tool_result" and "content" in block:
        return {**block, "content": desanitize_content(block["content"], reverse)}
    if block_type == "tool_use" and "input" in block:
        return {**block, "input": _desanitize_json(block["input"], reverse)}
    if block_type == "document":
        new_block = dict(block)
        for fld in ("title", "context"):
            v = new_block.get(fld)
            if isinstance(v, str) and v:
                new_block[fld] = reverse(v)
        src = new_block.get("source")
        if isinstance(src, dict):
            stype = src.get("type")
            if stype == "text" and isinstance(src.get("data"), str):
                new_block["source"] = {**src, "data": reverse(src["data"])}
            elif stype == "content" and "content" in src:
                new_block["source"] = {
                    **src,
                    "content": desanitize_content(src["content"], reverse),
                }
        return new_block
    return block


async def sanitize_content(
    content: Any,
    sanitize_one: SanitizeOne,
) -> tuple[Any, list[Any]]:
    """Sanitize content that may be str, list of blocks, a bare block dict, or other.

    Walks the content tree, applying sanitize_one to each text segment,
    and returns (new_content, [results, ...]) where results are what
    sanitize_one returned.

    Handles:
    - str → sanitize directly, return (sanitized_str, [result])
    - list → per item: dict block via _sanitize_block; non-dict → pass through
    - dict → treated as a single block via _sanitize_block (prevents bare-dict leaks)
    - anything else (None, int, …) → pass through unchanged, return empty results

    Builds new dicts/lists (no in-place mutation).
    """
    if isinstance(content, str):
        result = await sanitize_one(content)
        return result.sanitized_text, [result]

    if isinstance(content, list):
        new_list: list[Any] = []
        results: list[Any] = []
        for item in content:
            if isinstance(item, dict):
                new_item, item_results = await _sanitize_block(item, sanitize_one)
                results.extend(item_results)
                new_list.append(new_item)
            else:
                new_list.append(item)
        return new_list, results

    if isinstance(content, dict):
        return await _sanitize_block(content, sanitize_one)

    # Genuinely unknown non-dict/non-str/non-list → pass through unchanged.
    return content, []


def collect_text(content: Any) -> list[str]:
    """Collect every sanitizable text string from a content value
    (str | list[block] | tool_result-nested | tool_use.input | bare block dict), read-only.
    Mirrors sanitize_content's traversal so the pre-scan sees exactly what
    will be sanitized."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        out: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            block_type = item.get("type")
            if block_type in {"text", "input_text", "output_text"} and isinstance(
                item.get("text"), str
            ):
                out.append(item["text"])
            elif block_type == "tool_result" and "content" in item:
                out.extend(collect_text(item["content"]))
            elif block_type == "tool_use" and "input" in item:
                out.extend(_collect_json_text(item["input"]))
            elif block_type == "document":
                for fld in ("title", "context"):
                    v = item.get(fld)
                    if isinstance(v, str) and v:
                        out.append(v)
                src = item.get("source")
                if isinstance(src, dict):
                    stype = src.get("type")
                    if stype == "text" and isinstance(src.get("data"), str):
                        out.append(src["data"])
                    elif stype == "content" and "content" in src:
                        out.extend(collect_text(src["content"]))
        return out
    if isinstance(content, dict):
        block_type = content.get("type")
        if block_type in {"text", "input_text", "output_text"} and isinstance(
            content.get("text"), str
        ):
            return [content["text"]]
        if block_type == "tool_result" and "content" in content:
            return collect_text(content["content"])
        # SECURITY: egress sanitization covers tool_use.input (string leaves, non-streaming).
        # Streaming tool_use desanitization (input_json_delta) is handled in streaming.py.
        if block_type == "tool_use" and "input" in content:
            return _collect_json_text(content["input"])
        if block_type == "document":
            document_text: list[str] = []
            for fld in ("title", "context"):
                v = content.get(fld)
                if isinstance(v, str) and v:
                    document_text.append(v)
            src = content.get("source")
            if isinstance(src, dict):
                stype = src.get("type")
                if stype == "text" and isinstance(src.get("data"), str):
                    document_text.append(src["data"])
                elif stype == "content" and "content" in src:
                    document_text.extend(collect_text(src["content"]))
            return document_text
        return []
    return []


def desanitize_content(
    content: Any,
    reverse: ReverseOne,
) -> Any:
    """Desanitize content that may be str, list of blocks, a bare block dict, or other.

    Walks the content tree, applying reverse to each text segment,
    and returns new_content with placeholders replaced by originals.

    Handles the same shapes as sanitize_content but synchronously.
    Builds new dicts/lists (no in-place mutation).
    """
    if isinstance(content, str):
        return reverse(content)

    if isinstance(content, list):
        new_list: list[Any] = []
        for item in content:
            if isinstance(item, dict):
                new_list.append(_desanitize_block(item, reverse))
            else:
                new_list.append(item)
        return new_list

    if isinstance(content, dict):
        return _desanitize_block(content, reverse)

    # Genuinely unknown non-dict/non-str/non-list → pass through unchanged.
    return content


_RESPONSES_TEXT_FIELDS = frozenset(
    {
        "arguments",
        "content",
        "delta",
        "output",
        "reasoning",
        "refusal",
        "summary",
        "text",
        "transcript",
    }
)
_RESPONSES_OPAQUE_FIELDS = frozenset({"encrypted_content"})


def desanitize_responses_payload(
    value: Any,
    reverse: ReverseOne,
    *,
    _field: str | None = None,
    _depth: int = 0,
) -> Any:
    """Reverse placeholders in OpenAI Responses API output shapes.

    Responses objects carry user-visible text in nested ``output``/``content``
    items and tool arguments rather than Chat Completions ``choices``. Structural
    identifiers and signed ``encrypted_content`` are deliberately left untouched.
    """
    if _depth > _MAX_JSON_DEPTH:
        return value
    if _field in _RESPONSES_OPAQUE_FIELDS:
        return value
    if isinstance(value, str):
        if _field == "arguments":
            return _desanitize_arguments(value, reverse)
        return reverse(value) if _field in _RESPONSES_TEXT_FIELDS else value
    if isinstance(value, list):
        return [
            desanitize_responses_payload(item, reverse, _field=_field, _depth=_depth + 1)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: desanitize_responses_payload(
                item,
                reverse,
                _field=str(key),
                _depth=_depth + 1,
            )
            for key, item in value.items()
        }
    return value
