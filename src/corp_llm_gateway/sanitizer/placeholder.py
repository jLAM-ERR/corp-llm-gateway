import re
from collections.abc import Iterable

_PLACEHOLDER_FIND_RE = re.compile(r"\[[A-Z][A-Z0-9_]*_\d{3,}\]")
_UNWRAPPED_PLACEHOLDER_FIND_RE = re.compile(r"(?<!\[)\b[A-Z][A-Z0-9_]*_[A-Z0-9_]+\b(?!\])")
_RESPONSE_ALIAS_RE = re.compile(r"^\[(?P<alias>[A-Z][A-Z0-9_]*_[A-Z0-9_]+)\]$")


def find_placeholder_literals(text: str) -> list[str]:
    """All [FAMILY_NNN]-format substrings literally present in `text`
    (e.g. a user who typed '[EMAIL_001]' in their prompt). Used to forbid
    a real redaction from reusing a token the user typed verbatim."""
    return _PLACEHOLDER_FIND_RE.findall(text)


def find_unwrapped_placeholder_literals(text: str) -> list[str]:
    """Bare placeholder-like tokens already present in request input.

    Models commonly remove square brackets from generated placeholders when
    placing them inside code identifiers or file names. Existing bare tokens
    are tracked so reverse aliases never rewrite a user-supplied literal.
    """
    return _UNWRAPPED_PLACEHOLDER_FIND_RE.findall(text)


def add_unwrapped_response_aliases(
    pairs: Iterable[tuple[str, str]],
    *,
    forbidden: Iterable[str] = (),
) -> tuple[tuple[str, str], ...]:
    """Add response-only aliases for bracket-stripped placeholders.

    ``[LOCATION_007]`` is not a valid identifier, so a model may emit
    ``LOCATION_007`` in a class or file name. The alias lets the reverse pass
    restore that mutation while preserving explicit mappings and any bare
    token that was already present in the request.
    """
    original_pairs = tuple(pairs)
    blocked = set(forbidden)
    claimed = {placeholder: original for original, placeholder in original_pairs}
    expanded = list(original_pairs)

    for original, placeholder in original_pairs:
        match = _RESPONSE_ALIAS_RE.fullmatch(placeholder)
        if match is None:
            continue
        alias = match.group("alias")
        if alias == original or alias in blocked or alias in claimed:
            continue
        expanded.append((original, alias))
        claimed[alias] = original

    return tuple(expanded)


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
