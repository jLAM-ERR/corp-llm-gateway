"""Client identity preambles that must reach the provider byte-identical.

Claude Code emits its identity line as its OWN leading ``system`` block and
recognises it again by EXACT string equality (``Set.has`` over a fixed
three-element set) when it re-partitions the blocks for prompt caching —
verified with ``strings`` on the installed binary
(``~/.local/share/claude/versions/2.1.221``, claude 2.1.221). Anthropic's
OAuth-authenticated ``/v1/messages`` route keys on that same block to accept a
``sk-ant-oat`` subscription token, so one redacted character inside it stops the
request being a Claude Code request.

The strings are fixed client constants, never user content, so exempting them
from rewriting cannot leak an original (M1-14). The exemption is claimed by
``litellm_hook._sanitize_prompt_field`` for the leading ``system`` block ONLY —
never for ``instructions``, user messages, ``tool_result`` or ``document``
leaves, which are sanitized normally even when they hold an identity literal.
The exempt block stays fully visible to the Stage-0 / Stage-5 scans, which read
content through ``collect_text`` rather than through the sanitize path.
"""

from __future__ import annotations

from typing import Any

CLAUDE_CODE_IDENTITY_PREAMBLES: frozenset[str] = frozenset(
    {
        "You are Claude Code, Anthropic's official CLI for Claude.",
        "You are Claude Code, Anthropic's official CLI for Claude, "
        "running within the Claude Agent SDK.",
        "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
    }
)

# The one block Claude Code emits AHEAD of its identity block. litellm keeps it
# on the first-party Anthropic route (`_filter_billing_headers_from_system` runs
# only for providers that reject it), so it can still be sitting in front of the
# identity block when the gateway sees the payload.
_BILLING_HEADER_PREFIX = "x-anthropic-billing-header:"


def is_identity_preamble(text: str) -> bool:
    """True when ``text`` is BYTE-EXACTLY a client identity preamble.

    No whitespace tolerance: upstream matches the literal by exact equality, so a
    padded value is not the thing being protected, and tolerating padding would
    let unbounded whitespace ride into an unsanitized, size-unchecked leaf.
    """
    return text in CLAUDE_CODE_IDENTITY_PREAMBLES


def leading_identity_block_index(system: Any) -> int | None:
    """Index of the identity block at the HEAD of a ``system`` block list, else None.

    Only ``x-anthropic-billing-header:`` blocks may precede it. A later
    occurrence — after any ordinary prompt block — is not the client's identity
    block and gets sanitized like any other leaf.
    """
    if not isinstance(system, list):
        return None
    for index, block in enumerate(system):
        if not isinstance(block, dict) or block.get("type") != "text":
            return None
        text = block.get("text")
        if not isinstance(text, str):
            return None
        if is_identity_preamble(text):
            return index
        if not text.startswith(_BILLING_HEADER_PREFIX):
            return None
    return None
