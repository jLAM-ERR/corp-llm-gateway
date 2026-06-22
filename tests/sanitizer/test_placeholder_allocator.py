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
