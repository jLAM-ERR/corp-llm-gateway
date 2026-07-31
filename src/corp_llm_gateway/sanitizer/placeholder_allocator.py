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

from collections.abc import Iterable

from corp_llm_gateway.sanitizer.placeholder import placeholder_family

_FALLBACK_FAMILY = "REDACTED"


class RequestPlaceholderAllocator:
    """Enforce an original<->placeholder bijection across a request's segments.

    One instance per request. Not thread-safe; pre_call processes a request's
    segments sequentially.
    """

    def __init__(self) -> None:
        self._by_original: dict[str, str] = {}
        self._by_placeholder: dict[str, str] = {}
        self._forbidden: set[str] = set()
        self._family_next: dict[str, int] = {}

    def forbid(self, labels: Iterable[str]) -> None:
        """Mark labels that must never be assigned (they appear literally in
        the request input). SECURITY: prevents a real redaction's token from
        being indistinguishable from a user-typed literal on the reverse pass;
        today conversation_id == request_id so a collision stays within one
        request, but this would become a cross-context leak if conversation_id
        widens (see docs/conversation-id.md)."""
        self._forbidden.update(labels)

    def _is_taken(self, label: str) -> bool:
        return label in self._by_placeholder or label in self._forbidden

    def remap(
        self,
        pairs: tuple[tuple[str, str], ...],
        *,
        exempt_from_bijection: frozenset[str] = frozenset(),
    ) -> tuple[tuple[str, str], ...]:
        """Return request-canonical ``(original, placeholder)`` pairs.

        Input order is preserved. Each call updates allocator state so later
        segments observe placeholders already claimed by earlier ones.

        *exempt_from_bijection*: originals (matched substrings) that came from
        an operator-configured ``replace.md`` rule rather than a detector/
        oracle finding. Case-insensitive rule matching means the SAME rule can
        match several differently-cased originals (``Acme``/``acme``/``ACME``)
        that all carry the identical CONFIGURED replacement text — that is a
        deliberate many-to-one mapping, not a collision between two different
        real-world originals, so it must not compete for a freshly minted
        label the way two distinct detector findings sharing a placeholder
        would (see MAJOR 5).
        """
        return tuple(
            (original, self._canonical(original, placeholder, original in exempt_from_bijection))
            for original, placeholder in pairs
        )

    def _canonical(self, original: str, placeholder: str, exempt: bool = False) -> str:
        existing = self._by_original.get(original)
        if existing is not None:
            # Same original seen before (possibly in another segment): reuse its
            # placeholder so the model sees one consistent token for it.
            return existing
        taken = self._is_taken(placeholder)
        if taken and exempt and placeholder not in self._forbidden:
            # Another rule occurrence (same rule, a different case variant, or
            # a different rule sharing the same configured replacement text)
            # already claimed this exact token — reuse it verbatim rather than
            # minting a fresh, operator-unconfigured one. The `_forbidden`
            # check is NOT skipped: a placeholder the user typed literally
            # must still never be reused for a real redaction, exempt or not.
            chosen = placeholder
        elif taken:
            chosen = self._mint(placeholder)
        else:
            chosen = placeholder
        self._by_original[original] = chosen
        self._by_placeholder.setdefault(chosen, original)
        return chosen

    def _mint(self, placeholder: str) -> str:
        """Allocate a fresh unused placeholder in the same label family."""
        family = placeholder_family(placeholder) or _FALLBACK_FAMILY
        index = self._family_next.get(family, 1)
        while self._is_taken(f"[{family}_{index:03d}]"):
            index += 1
        self._family_next[family] = index + 1
        return f"[{family}_{index:03d}]"
