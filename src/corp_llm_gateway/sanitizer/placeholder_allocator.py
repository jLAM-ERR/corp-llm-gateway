"""Per-request placeholder allocation (cross-segment collision fix).

A single Claude Code request is sanitized one text segment at a time — every
message content block AND the top-level ``system`` field is its own
``SanitizationOrchestrator.sanitize`` call (see ``litellm_hook`` pre_call and
``sanitizer/content_blocks``). The corp-LLM numbers placeholders from
``[LABEL_001]`` independently for each call, so two DIFFERENT originals living
in two different segments collide on the same token (e.g. the git-identity
email in ``system`` and a customer email in a user message both become
``[EMAIL_001]``).

That collision breaks de-sanitization: the reverse map
(``StreamingDesanitizer._by_placeholder``) is keyed by placeholder, so a
duplicated token can only ever restore ONE of the originals — the other is
silently lost, and on egress the upstream model sees a single token standing
for two different people.

``RequestPlaceholderAllocator`` remaps each segment's pairs to a
request-canonical placeholder, guaranteeing within one request that the same
original always maps to one placeholder (reused across segments) and that
different originals never share a placeholder (a fresh label in the same family
is minted on collision). See project_placeholder_collision_cross_segment in
session memory.
"""

from __future__ import annotations

import re

# A standard placeholder is ``[FAMILY_NNN]`` where FAMILY may itself contain
# underscores (e.g. ``API_KEY``); the trailing ``_<digits>`` is the index.
_LABEL_RE = re.compile(r"^\[(?P<family>.+)_(?P<index>\d+)\]$")
_FALLBACK_FAMILY = "REDACTED"


class RequestPlaceholderAllocator:
    """Enforce an original<->placeholder bijection across a request's segments.

    One instance per request. Not thread-safe; pre_call processes a request's
    segments sequentially.
    """

    def __init__(self) -> None:
        self._by_original: dict[str, str] = {}
        self._by_placeholder: dict[str, str] = {}

    def remap(self, pairs: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        """Return request-canonical ``(original, placeholder)`` pairs.

        Input order is preserved. Each call updates allocator state so later
        segments observe placeholders already claimed by earlier ones.
        """
        return tuple(
            (original, self._canonical(original, placeholder)) for original, placeholder in pairs
        )

    def _canonical(self, original: str, placeholder: str) -> str:
        existing = self._by_original.get(original)
        if existing is not None:
            # Same original seen before (possibly in another segment): reuse its
            # placeholder so the model sees one consistent token for it.
            return existing
        chosen = placeholder if placeholder not in self._by_placeholder else self._mint(placeholder)
        self._by_original[original] = chosen
        self._by_placeholder[chosen] = original
        return chosen

    def _mint(self, placeholder: str) -> str:
        """Allocate a fresh unused placeholder in the same label family."""
        match = _LABEL_RE.match(placeholder)
        family = match.group("family") if match else _FALLBACK_FAMILY
        index = 1
        while True:
            candidate = f"[{family}_{index:03d}]"
            if candidate not in self._by_placeholder:
                return candidate
            index += 1
