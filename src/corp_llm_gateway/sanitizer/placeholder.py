import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

_PLACEHOLDER_FIND_RE = re.compile(r"\[[A-Z][A-Z0-9_]*_\d{3,}\]")
_UNWRAPPED_PLACEHOLDER_FIND_RE = re.compile(r"(?<!\[)\b[A-Z][A-Z0-9_]*_[A-Z0-9_]+\b(?!\])")
_RESPONSE_ALIAS_RE = re.compile(r"^\[(?P<alias>[A-Z][A-Z0-9_]*_[A-Z0-9_]+)\]$")


class StaleSpanError(ValueError):
    """A pre-selected replacement span is invalid or no longer matches the
    source text (e.g. a stale Cache-A/allocator remap).

    Fails closed (M4 fail-policy matrix): `litellm_hook.py` maps this to a
    stable `error_code` (via the `error_code` class attribute) plus an audit
    record and `gateway_failure{component}` metric, instead of letting a bare
    exception escape as a generic 500. Subclasses ValueError so existing
    `except ValueError` / `pytest.raises(ValueError)` call sites keep working.
    """

    error_code = "E_SPAN_INVALID"


@dataclass(frozen=True)
class AppliedSpan:
    """One original-text range selected for forward substitution."""

    start: int
    end: int
    original: str


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


def build_reverse_substituter(pairs: Iterable[tuple[str, str]]) -> Callable[[str], str]:
    """Build the shared reverse (model-output → original) substitution function.

    Applies longest-placeholder-first (M1-9). A bracketed placeholder
    (``[FAMILY_001]``) is replaced with a plain substring match — the
    brackets are not identifier characters, so they already delimit a
    boundary. A bracket-less response alias (``FAMILY_001``, added by
    :func:`add_unwrapped_response_aliases` for models that strip brackets
    when coining identifiers, or an operator ``replace.md`` rule replacement
    with no bracketed sibling) is replaced only at an identifier boundary
    (``(?<![A-Za-z0-9_])alias(?![A-Za-z0-9_])``), so a model-coined
    identifier that merely CONTAINS the alias (``MY_PROJECT_001``) is left
    untouched instead of corrupted by an unbounded ``str.replace``.

    The returned callable accepts an optional ``final`` keyword (default
    ``True``) for streaming callers with an in-flight, not-yet-complete
    buffer: a bare-alias match ending exactly at the buffer's current end is
    indistinguishable from one a later chunk could still extend past the
    boundary (the trailing lookahead is zero-width, so "nothing here yet"
    satisfies it the same as "definitely nothing here") — pass
    ``final=False`` to leave such a trailing match unreplaced instead of
    finalizing it early; the caller's hold-back window keeps the unreplaced
    text buffered until real trailing context (or the final flush) confirms
    it one way or the other. No synthetic character is ever inserted into
    the text being matched, so a replacement/placeholder value can safely
    contain any character, including one a padding-based scheme might
    otherwise have reserved as a marker.

    When two different originals share one placeholder — an operator
    ``replace.md`` rule replacement configured many-to-one, which
    ``RequestPlaceholderAllocator`` deliberately exempts from its bijection —
    the reverse map keeps the FIRST original to claim that placeholder,
    matching the allocator's own ``_by_placeholder.setdefault`` semantics.
    Restoring such a collapsed placeholder is inherently lossy (only one of
    the two originals can ever come back for a GIVEN static reverse map); this
    at least makes the two maps agree instead of picking different originals.

    Every reverse site (unary response, Anthropic/OpenAI SSE streaming,
    Responses SSE streaming) must build its reverse function through this
    helper so the boundary rule is enforced uniformly.

    The returned callable also accepts ``protected_prefix`` (default ``0``):
    the number of leading characters of ``text`` that must never be consumed
    by a bare-alias match, even though they're still visible to its LEADING
    lookbehind. A streaming caller that truncates its buffer needs one real
    character of true left context to correctly resolve the boundary check
    for a bare alias sitting at the buffer's new start (see
    ``streaming.StreamingDesanitizer._replace_all``); prepending that
    character makes it visible to the lookbehind without letting a match
    retroactively consume text that was already emitted to the client.
    Bracketed placeholders need no such guard — an exact ``[FAMILY_NNN]``
    substring is always caught (or definitively absent) using only the text
    present in a single buffer BEFORE any truncation, so it can never survive
    un-replaced only to combine with later text at the truncation point.
    """
    by_placeholder: dict[str, str] = {}
    for original, placeholder in pairs:
        by_placeholder.setdefault(placeholder, original)
    entries: list[tuple[str, str, re.Pattern[str] | None]] = []
    for placeholder in sort_placeholders_by_descending_length(by_placeholder):
        replacement = by_placeholder[placeholder]
        if placeholder.startswith("["):
            entries.append((placeholder, replacement, None))
        else:
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(placeholder)}(?![A-Za-z0-9_])")
            entries.append((placeholder, replacement, pattern))

    def _reverse(text: str, *, final: bool = True, protected_prefix: int = 0) -> str:
        for placeholder, replacement, pattern in entries:
            if pattern is None:
                text = text.replace(placeholder, replacement)
                continue
            if final and protected_prefix == 0:
                text = pattern.sub(lambda _m, r=replacement: r, text)
                continue
            # Not the final flush, or a protected prefix is in play: a match
            # whose end lands exactly at the buffer's current end can't yet be
            # told apart from one a later chunk could still extend past the
            # boundary — defer it (leave it unmatched here) instead of
            # finalizing. A match starting inside `protected_prefix` is text
            # already emitted to the client — never replace it, regardless of
            # `final`.
            pieces: list[str] = []
            cursor = 0
            end = len(text)
            for m in pattern.finditer(text):
                if m.start() < protected_prefix:
                    continue
                if not final and m.end() == end:
                    break
                pieces.append(text[cursor : m.start()])
                pieces.append(replacement)
                cursor = m.end()
            pieces.append(text[cursor:])
            text = "".join(pieces)
        return text

    return _reverse


def apply_pairs(text: str, pairs: Iterable[tuple[str, str]]) -> str:
    """Forward substitution: replace each ``original`` with its ``placeholder``.

    Longer originals are substituted first so a shorter original that is a
    substring of a longer one cannot partially corrupt it. This is the forward
    counterpart to :func:`sort_placeholders_by_descending_length` (reverse
    path). Span-aware orchestrators use :func:`apply_spans`; this remains the
    fallback for legacy/custom sanitizers without span metadata.
    """
    for original, placeholder in sorted(pairs, key=lambda p: -len(p[0])):
        text = text.replace(original, placeholder)
    return text


def apply_spans(
    text: str,
    spans: Iterable[AppliedSpan],
    pairs: Iterable[tuple[str, str]],
) -> str:
    """Apply a preselected, non-overlapping replacement plan in one pass.

    Replacements are read from *pairs* by original so request-level placeholder
    canonicalization can reuse the exact spans without rescanning or chaining.
    """
    by_original: dict[str, str] = {}
    for original, replacement in pairs:
        by_original.setdefault(original, replacement)

    ordered = sorted(spans, key=lambda span: (span.start, span.end))
    out: list[str] = []
    cursor = 0
    for span in ordered:
        if span.start < cursor or span.start < 0 or span.end <= span.start or span.end > len(text):
            raise StaleSpanError(
                f"invalid or overlapping applied span: start={span.start} end={span.end}"
            )
        if text[span.start : span.end] != span.original:
            raise StaleSpanError(
                f"applied span does not match source text: start={span.start} end={span.end}"
            )
        selected_replacement = by_original.get(span.original)
        if selected_replacement is None:
            raise StaleSpanError(
                f"missing replacement for applied span: start={span.start} end={span.end}"
            )
        out.append(text[cursor : span.start])
        out.append(selected_replacement)
        cursor = span.end
    out.append(text[cursor:])
    return "".join(out)


_PLACEHOLDER_LABEL_RE = re.compile(r"^\[(?P<family>.+)_(?P<index>\d+)\]$")


def placeholder_family(label: str) -> str | None:
    """FAMILY of a [FAMILY_NNN] placeholder ('EMAIL' from '[EMAIL_001]'),
    or None if not in that form. A category name only — never original
    text, so it is safe for audit histograms. Keep the pattern in sync
    with placeholder_allocator._LABEL_RE."""
    m = _PLACEHOLDER_LABEL_RE.match(label)
    return m.group("family") if m else None
