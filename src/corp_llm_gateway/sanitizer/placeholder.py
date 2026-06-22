import re
from collections.abc import Iterable


def sort_placeholders_by_descending_length(placeholders: Iterable[str]) -> list[str]:
    """Return placeholders sorted by descending length, stable on ties.

    Substitution must replace longer placeholders first, otherwise a
    short placeholder (e.g. `[NAME]`) can shadow a longer one
    (e.g. `[NAME_2]`) and corrupt de-sanitization.

    Lift from the data-sanitizer plugin's `desanitize.py:18`.
    """
    return sorted(placeholders, key=lambda s: (-len(s), s))


def apply_pairs(text: str, pairs: Iterable[tuple[str, str]]) -> str:
    """Forward substitution: replace each ``original`` with its ``placeholder``.

    Longer originals are substituted first so a shorter original that is a
    substring of a longer one cannot partially corrupt it. This is the forward
    counterpart to :func:`sort_placeholders_by_descending_length` (reverse
    path) and mirrors ``orchestrator._apply_pairs``.
    """
    for original, placeholder in sorted(pairs, key=lambda p: -len(p[0])):
        text = text.replace(original, placeholder)
    return text


_PLACEHOLDER_LABEL_RE = re.compile(r"^\[(?P<family>.+)_(?P<index>\d+)\]$")


def placeholder_family(label: str) -> str | None:
    """FAMILY of a [FAMILY_NNN] placeholder ('EMAIL' from '[EMAIL_001]'),
    or None if not in that form. A category name only — never original
    text, so it is safe for audit histograms. Keep the pattern in sync
    with placeholder_allocator._LABEL_RE."""
    m = _PLACEHOLDER_LABEL_RE.match(label)
    return m.group("family") if m else None
