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
from rewriting cannot leak an original (M1-14). They stay fully visible to the
Stage-0 / Stage-5 scans, which read content through ``collect_text`` rather than
through the sanitize path.
"""

from __future__ import annotations

CLAUDE_CODE_IDENTITY_PREAMBLES: frozenset[str] = frozenset(
    {
        "You are Claude Code, Anthropic's official CLI for Claude.",
        "You are Claude Code, Anthropic's official CLI for Claude, "
        "running within the Claude Agent SDK.",
        "You are a Claude agent, built on Anthropic's Claude Agent SDK.",
    }
)


def is_identity_preamble(text: str) -> bool:
    """True when ``text`` is nothing but a client identity preamble.

    Surrounding whitespace is tolerated because it carries no user content; any
    other extra character means the leaf is a normal prompt that happens to
    start with the preamble, and it is sanitized as usual.
    """
    return text.strip() in CLAUDE_CODE_IDENTITY_PREAMBLES
