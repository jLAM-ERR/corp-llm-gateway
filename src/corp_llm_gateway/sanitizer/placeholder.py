from collections.abc import Iterable


def sort_placeholders_by_descending_length(placeholders: Iterable[str]) -> list[str]:
    """Return placeholders sorted by descending length, stable on ties.

    Substitution must replace longer placeholders first, otherwise a
    short placeholder (e.g. `[NAME]`) can shadow a longer one
    (e.g. `[NAME_2]`) and corrupt de-sanitization.

    Lift from the data-sanitizer plugin's `desanitize.py:18`.
    """
    return sorted(placeholders, key=lambda s: (-len(s), s))
