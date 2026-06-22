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
    # SECURITY (known gap, not yet handled): `tool_use` blocks carry an `input`
    # dict that is passed through UN-sanitized. In a multi-turn flow a client may
    # replay a restored original inside a tool-call arg, which would then egress
    # un-redacted. Sanitizing tool_use.input is a separate follow-up task. See
    # project_tool_use_input_unsanitized in session memory.
    # Image, image_url, tool_use, document, unknown → pass through unchanged.
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
    (str | list[block] | tool_result-nested | bare block dict), read-only.
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
        return out
    if isinstance(content, dict):
        block_type = content.get("type")
        if block_type == "text" and isinstance(content.get("text"), str):
            return [content["text"]]
        if block_type == "tool_result" and "content" in content:
            return collect_text(content["content"])
        # SECURITY (known gap, not yet handled): `tool_use` blocks carry an `input`
        # dict that is passed through UN-sanitized. In a multi-turn flow a client may
        # replay a restored original inside a tool-call arg, which would then egress
        # un-redacted. Sanitizing tool_use.input is a separate follow-up task. See
        # project_tool_use_input_unsanitized in session memory.
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
