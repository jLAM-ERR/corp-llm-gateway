"""Content block walker for sanitization/desanitization.

Handles both str and list-of-blocks (Anthropic) content shapes,
plus OpenAI multimodal content-parts. Reusable across pre_call and
post_call with configurable rewrite callbacks.
"""

from __future__ import annotations

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


async def _sanitize_block(
    block: dict[str, Any],
    sanitize_one: SanitizeOne,
) -> tuple[dict[str, Any], list[Any]]:
    """Apply sanitize_one to a single content dict block.

    Shared by the list-item path and the bare-dict path so the rules
    live in exactly one place.
    """
    block_type = block.get("type")
    if block_type == "text" and isinstance(block.get("text"), str):
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
    if block_type == "text" and isinstance(block.get("text"), str):
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
            if block_type == "text" and isinstance(item.get("text"), str):
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
        if block_type == "text" and isinstance(content.get("text"), str):
            return [content["text"]]
        if block_type == "tool_result" and "content" in content:
            return collect_text(content["content"])
        # SECURITY: egress sanitization covers tool_use.input (string leaves, non-streaming).
        # Streaming tool_use desanitization (input_json_delta) is handled in streaming.py.
        if block_type == "tool_use" and "input" in content:
            return _collect_json_text(content["input"])
        if block_type == "document":
            out: list[str] = []
            for fld in ("title", "context"):
                v = content.get(fld)
                if isinstance(v, str) and v:
                    out.append(v)
            src = content.get("source")
            if isinstance(src, dict):
                stype = src.get("type")
                if stype == "text" and isinstance(src.get("data"), str):
                    out.append(src["data"])
                elif stype == "content" and "content" in src:
                    out.extend(collect_text(src["content"]))
            return out
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
