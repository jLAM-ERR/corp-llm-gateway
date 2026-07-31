from corp_llm_gateway.sanitizer.placeholder_allocator import (
    RequestPlaceholderAllocator,
)


def test_no_collision_pairs_unchanged() -> None:
    a = RequestPlaceholderAllocator()
    pairs = (("a@x.com", "[EMAIL_001]"), ("KEY1", "[API_KEY_001]"))
    assert a.remap(pairs) == pairs


def test_same_original_reuses_placeholder_across_segments() -> None:
    a = RequestPlaceholderAllocator()
    seg1 = a.remap((("a@x.com", "[EMAIL_001]"),))
    seg2 = a.remap((("a@x.com", "[EMAIL_001]"),))  # same original, later segment
    assert seg1 == (("a@x.com", "[EMAIL_001]"),)
    assert seg2 == (("a@x.com", "[EMAIL_001]"),)  # reused, not split


def test_different_originals_same_token_are_split() -> None:
    a = RequestPlaceholderAllocator()
    seg1 = a.remap((("a@x.com", "[EMAIL_001]"),))  # claims [EMAIL_001]
    seg2 = a.remap((("b@y.com", "[EMAIL_001]"),))  # collision -> minted
    assert seg1 == (("a@x.com", "[EMAIL_001]"),)
    assert seg2 == (("b@y.com", "[EMAIL_002]"),)


def test_collision_mint_preserves_label_family() -> None:
    a = RequestPlaceholderAllocator()
    a.remap((("KEY_A", "[API_KEY_001]"),))
    assert a.remap((("KEY_B", "[API_KEY_001]"),)) == (("KEY_B", "[API_KEY_002]"),)


def test_mint_skips_already_taken_indices() -> None:
    a = RequestPlaceholderAllocator()
    # [EMAIL_001] and [EMAIL_002] legitimately assigned to A and B.
    a.remap((("A@x", "[EMAIL_001]"), ("B@x", "[EMAIL_002]")))
    # C collides on [EMAIL_001]; next free in family is [EMAIL_003].
    assert a.remap((("C@x", "[EMAIL_001]"),)) == (("C@x", "[EMAIL_003]"),)


def test_independent_families_number_independently() -> None:
    a = RequestPlaceholderAllocator()
    a.remap((("A@x", "[EMAIL_001]"), ("KEY1", "[API_KEY_001]")))
    assert a.remap((("B@x", "[EMAIL_001]"), ("KEY2", "[API_KEY_001]"))) == (
        ("B@x", "[EMAIL_002]"),
        ("KEY2", "[API_KEY_002]"),
    )


def test_within_segment_distinct_tokens_untouched() -> None:
    a = RequestPlaceholderAllocator()
    pairs = (("a@x", "[EMAIL_001]"), ("b@x", "[EMAIL_002]"))
    assert a.remap(pairs) == pairs


def test_nonstandard_placeholder_collision_uses_fallback_family() -> None:
    a = RequestPlaceholderAllocator()
    a.remap((("A", "REDACTED"),))  # claims the bare token
    assert a.remap((("B", "REDACTED"),)) == (("B", "[REDACTED_001]"),)


def test_empty_pairs() -> None:
    assert RequestPlaceholderAllocator().remap(()) == ()


# ---- MAJOR 5: case-insensitive rule collisions must not mint new tokens ----


def test_rule_originals_sharing_a_configured_replacement_collapse_to_one_token() -> None:
    """Exact review repro: rule `Acme = PARTNER-A` matched case-insensitively
    against `acme`/`ACME`/`Acme` yields three DIFFERENT originals with the
    IDENTICAL configured replacement — an operator-configured replacement is
    intentionally many-to-one and must not compete for a fresh label the way
    two distinct detector findings sharing a placeholder would."""
    a = RequestPlaceholderAllocator()
    pairs = (
        ("acme", "PARTNER-A"),
        ("ACME", "PARTNER-A"),
        ("Acme", "PARTNER-A"),
    )
    out = a.remap(pairs, exempt_from_bijection=frozenset({"acme", "ACME", "Acme"}))
    assert out == (
        ("acme", "PARTNER-A"),
        ("ACME", "PARTNER-A"),
        ("Acme", "PARTNER-A"),
    )


def test_rule_exemption_does_not_apply_across_segments_to_non_rule_originals() -> None:
    """A detector/oracle finding must still mint a fresh token when it
    collides with an ALREADY-claimed rule token — only the rule's OWN
    case-variant occurrences are exempt, not unrelated originals."""
    a = RequestPlaceholderAllocator()
    a.remap((("Acme", "PARTNER-A"),), exempt_from_bijection=frozenset({"Acme"}))
    out = a.remap((("Bob Newco", "PARTNER-A"),), exempt_from_bijection=frozenset({"Acme"}))
    assert out == (("Bob Newco", "[REDACTED_001]"),)


def test_two_different_rules_sharing_one_configured_replacement_also_collapse() -> None:
    """Two DIFFERENT rules configured (deliberately or not) with the identical
    replacement text behave the same as release's plain str.replace did —
    both are operator-configured, so both are exempt."""
    a = RequestPlaceholderAllocator()
    a.remap((("Acme", "PARTNER-A"),), exempt_from_bijection=frozenset({"Acme", "Foobar"}))
    out = a.remap((("Foobar", "PARTNER-A"),), exempt_from_bijection=frozenset({"Acme", "Foobar"}))
    assert out == (("Foobar", "PARTNER-A"),)


def test_rule_exemption_never_bypasses_the_forbidden_literal_guard() -> None:
    """SECURITY: a placeholder the user typed literally must never be reused
    for a real redaction, even when the colliding original is rule-exempt —
    otherwise the redaction token becomes indistinguishable from the user's
    own literal text (the exact hazard `forbid()` exists to prevent)."""
    a = RequestPlaceholderAllocator()
    a.forbid(["PARTNER-A"])
    out = a.remap((("Acme", "PARTNER-A"),), exempt_from_bijection=frozenset({"Acme"}))
    assert out == (("Acme", "[REDACTED_001]"),)
