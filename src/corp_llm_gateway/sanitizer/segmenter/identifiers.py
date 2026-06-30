"""Sub-token splitter for code identifiers.

Handles camelCase / PascalCase / snake_case / kebab-case / SCREAMING_SNAKE.
Pure stdlib. Seam: replace with tree-sitter tokenization if lexer-level
accuracy is needed for identifiers embedded in complex syntax.
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(
    r"[A-Z]{2,}(?=[A-Z][a-z]|\d|[_\-]|$)"  # consecutive caps before a transition
    r"|[A-Z][a-z\d]*"  # PascalCase / camelCase word
    r"|[a-z\d]+"  # lowercase run (snake / kebab)
)


def split_identifier(name: str) -> list[tuple[str, int, int]]:
    """Split a code identifier into sub-tokens with (start, end) offsets relative to name.

    Skips underscore and hyphen delimiters; they produce no sub-tokens.
    """
    return [(m.group(), m.start(), m.end()) for m in _TOKEN_RE.finditer(name)]
