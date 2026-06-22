"""Adversarial cases for RequestPlaceholderAllocator.

These guard the *class* of bug behind project_placeholder_collision_cross_segment:
independent per-segment placeholder numbering producing colliding tokens. They
probe the nastier orderings (a minted label later clashing with a corp-LLM
label, multi-way collisions, repeated originals) rather than the happy path.
"""

from corp_llm_gateway.sanitizer.placeholder_allocator import (
    RequestPlaceholderAllocator,
)


def test_minted_label_does_not_collide_with_later_corp_label() -> None:
    # A claims [EMAIL_001]; B collides -> minted [EMAIL_002]; then the corp-LLM
    # itself hands back [EMAIL_002] for a THIRD original C. The mint scan must
    # also skip the previously-minted token, not just the corp-assigned ones.
    a = RequestPlaceholderAllocator()
    assert a.remap((("A", "[EMAIL_001]"),)) == (("A", "[EMAIL_001]"),)
    assert a.remap((("B", "[EMAIL_001]"),)) == (("B", "[EMAIL_002]"),)
    assert a.remap((("C", "[EMAIL_002]"),)) == (("C", "[EMAIL_003]"),)


def test_three_way_collision_all_split() -> None:
    a = RequestPlaceholderAllocator()
    out = a.remap((("A", "[E_001]"), ("B", "[E_001]"), ("C", "[E_001]")))
    assert out == (("A", "[E_001]"), ("B", "[E_002]"), ("C", "[E_003]"))
    # Bijection: three distinct originals -> three distinct tokens.
    assert len({ph for _, ph in out}) == 3


def test_repeated_original_within_one_segment_reuses_token() -> None:
    a = RequestPlaceholderAllocator()
    out = a.remap((("A@x", "[EMAIL_001]"), ("A@x", "[EMAIL_001]")))
    assert out == (("A@x", "[EMAIL_001]"), ("A@x", "[EMAIL_001]"))


def test_same_original_different_corp_labels_across_segments_canonicalizes() -> None:
    # A appears in seg1 as [EMAIL_001] and in seg2 the corp-LLM numbers it
    # [EMAIL_007] (because seg2 had earlier emails). It must canonicalize back
    # to the token A already owns, not introduce a second alias for A.
    a = RequestPlaceholderAllocator()
    a.remap((("A", "[EMAIL_001]"),))
    assert a.remap((("Z", "[EMAIL_001]"), ("A", "[EMAIL_007]"))) == (
        ("Z", "[EMAIL_002]"),
        ("A", "[EMAIL_001]"),
    )


def test_long_collision_chain_stays_bijective() -> None:
    a = RequestPlaceholderAllocator()
    seen: set[str] = set()
    for i in range(50):
        ((_, ph),) = a.remap(((f"orig-{i}", "[EMAIL_001]"),))
        assert ph not in seen  # never reuse a token for a different original
        seen.add(ph)
    assert len(seen) == 50


def test_distinct_originals_never_share_after_arbitrary_mix() -> None:
    a = RequestPlaceholderAllocator()
    pairs = (
        ("a@x", "[EMAIL_001]"),
        ("b@x", "[EMAIL_001]"),
        ("KEY1", "[API_KEY_001]"),
        ("a@x", "[EMAIL_001]"),  # repeat of a@x -> must reuse
        ("KEY2", "[API_KEY_001]"),
    )
    out = a.remap(pairs)
    # a@x maps to one stable token in both its appearances.
    a_tokens = {ph for orig, ph in out if orig == "a@x"}
    assert len(a_tokens) == 1
    # Every distinct original has a distinct token (bijection).
    by_orig = dict(out)
    assert len(set(by_orig.values())) == len(by_orig)
