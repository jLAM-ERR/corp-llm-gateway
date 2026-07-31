import pytest

from corp_llm_gateway.sanitizer.placeholder import (
    AppliedSpan,
    add_unwrapped_response_aliases,
    apply_pairs,
    apply_spans,
    build_reverse_substituter,
    find_placeholder_literals,
    find_unwrapped_placeholder_literals,
    sort_placeholders_by_descending_length,
)


def test_apply_pairs_basic() -> None:
    assert apply_pairs("hello alice", [("alice", "[NAME_001]")]) == "hello [NAME_001]"


def test_apply_pairs_longer_original_first_avoids_substring_corruption() -> None:
    # "john" is a substring of "john.doe@x.com": the longer original must be
    # replaced first, or the email is corrupted by the name substitution.
    text = "john.doe@x.com is john"
    pairs = [("john", "[NAME_001]"), ("john.doe@x.com", "[EMAIL_001]")]
    assert apply_pairs(text, pairs) == "[EMAIL_001] is [NAME_001]"


def test_apply_pairs_empty_is_identity() -> None:
    assert apply_pairs("untouched", []) == "untouched"


def test_apply_pairs_replaces_all_occurrences() -> None:
    assert apply_pairs("a@x and a@x", [("a@x", "[E_001]")]) == "[E_001] and [E_001]"


def test_apply_spans_uses_selected_occurrences_without_chaining() -> None:
    text = "Alice Smith met Alice"
    spans = (
        AppliedSpan(0, 11, "Alice Smith"),
        AppliedSpan(16, 21, "Alice"),
    )
    pairs = (
        ("Alice Smith", "AliceAlias"),
        ("Alice", "[PERSON_002]"),
    )

    assert apply_spans(text, spans, pairs) == "AliceAlias met [PERSON_002]"


def test_apply_spans_rejects_stale_source_range() -> None:
    with pytest.raises(ValueError, match="does not match source text") as exc_info:
        apply_spans("Alice", (AppliedSpan(0, 5, "Bob"),), (("Bob", "[PERSON_001]"),))
    assert "Bob" not in str(exc_info.value)


def test_sort_placeholders_descending_length_stable() -> None:
    assert sort_placeholders_by_descending_length(["[A]", "[AAA]", "[AA]"]) == [
        "[AAA]",
        "[AA]",
        "[A]",
    ]


def test_find_placeholder_literals_matches_real_tokens() -> None:
    assert find_placeholder_literals("send to [EMAIL_001] ok") == ["[EMAIL_001]"]
    assert find_placeholder_literals("key [API_KEY_001] here") == ["[API_KEY_001]"]


def test_find_placeholder_literals_rejects_code_identifiers() -> None:
    assert find_placeholder_literals("[my_var_1]") == []
    assert find_placeholder_literals("[item_3]") == []
    assert find_placeholder_literals("[MAX_SIZE_3]") == []


def test_find_placeholder_literals_empty_string() -> None:
    assert find_placeholder_literals("") == []


def test_find_placeholder_literals_multiple_real_tokens() -> None:
    text = "[EMAIL_001] and [API_KEY_002] in text"
    result = find_placeholder_literals(text)
    assert "[EMAIL_001]" in result
    assert "[API_KEY_002]" in result
    assert len(result) == 2


def test_find_unwrapped_placeholder_literals_ignores_bracketed_tokens() -> None:
    text = "LOCATION_007 [LOCATION_008] PARTNER_CODENAME and ordinary words"
    assert find_unwrapped_placeholder_literals(text) == [
        "LOCATION_007",
        "PARTNER_CODENAME",
    ]


def test_add_unwrapped_response_aliases_for_code_identifiers() -> None:
    pairs = add_unwrapped_response_aliases([("KdirCorpCalculatorService", "[LOCATION_007]")])
    assert pairs == (
        ("KdirCorpCalculatorService", "[LOCATION_007]"),
        ("KdirCorpCalculatorService", "LOCATION_007"),
    )


def test_add_unwrapped_response_aliases_preserves_input_literal() -> None:
    pairs = add_unwrapped_response_aliases(
        [("KdirCorpCalculatorService", "[LOCATION_007]")],
        forbidden={"LOCATION_007"},
    )
    assert pairs == (("KdirCorpCalculatorService", "[LOCATION_007]"),)


def test_add_unwrapped_response_aliases_does_not_override_explicit_mapping() -> None:
    pairs = add_unwrapped_response_aliases(
        [
            ("KdirCorpCalculatorService", "[LOCATION_007]"),
            ("literal", "LOCATION_007"),
        ]
    )
    assert pairs == (
        ("KdirCorpCalculatorService", "[LOCATION_007]"),
        ("literal", "LOCATION_007"),
    )


# --- build_reverse_substituter (defect #6: boundary-anchored bare aliases) --


def test_build_reverse_substituter_bracketed_placeholder_is_plain_replace() -> None:
    reverse = build_reverse_substituter([("alice", "[NAME_001]")])
    assert reverse("hello [NAME_001]") == "hello alice"


def test_build_reverse_substituter_bare_alias_replaces_at_word_boundary() -> None:
    reverse = build_reverse_substituter(
        [("SecretPlace", "[LOCATION_007]"), ("SecretPlace", "LOCATION_007")]
    )
    assert reverse("see LOCATION_007 here") == "see SecretPlace here"


def test_build_reverse_substituter_bare_alias_does_not_corrupt_containing_identifier() -> None:
    """Defect #6(i): MY_PROJECT_001 merely CONTAINS the alias PROJECT_001 —
    an unbounded str.replace corrupts it into MY_Zephyr Ledger. The stand-alone
    occurrence right after it is a legitimate bracket-stripped alias and must
    still be restored."""
    reverse = build_reverse_substituter(
        [("Zephyr Ledger", "[PROJECT_001]"), ("Zephyr Ledger", "PROJECT_001")]
    )
    text = "const MY_PROJECT_001 = 1; // see PROJECT_001"
    assert reverse(text) == "const MY_PROJECT_001 = 1; // see Zephyr Ledger"


def test_build_reverse_substituter_bare_alias_relocation_repro() -> None:
    reverse = build_reverse_substituter(
        [("SecretPlace", "[LOCATION_007]"), ("SecretPlace", "LOCATION_007")]
    )
    assert reverse("RELOCATION_0071") == "RELOCATION_0071"
    assert reverse("PICKUP_LOCATION_007X") == "PICKUP_LOCATION_007X"


def test_sort_placeholders_descending_length_with_aliases_present() -> None:
    """Invariant 5 (M1-9) still holds once bracketless response aliases are
    mixed in with their bracketed placeholders."""
    assert sort_placeholders_by_descending_length(["X_001", "[X_0011]", "[X_001]", "X_0011"]) == [
        "[X_0011]",
        "[X_001]",
        "X_0011",
        "X_001",
    ]


def test_build_reverse_substituter_many_to_one_collision_prefers_first_original() -> None:
    """When two different originals share one placeholder (an operator rule
    replacement configured many-to-one, exempted from the allocator's
    bijection), the reverse map must agree with the allocator's own
    first-claim semantics (`RequestPlaceholderAllocator._by_placeholder.
    setdefault` keeps the first original) — otherwise the SAME placeholder
    restores to different text depending on iteration order, corrupting one
    of the two forward occurrences."""
    reverse = build_reverse_substituter([("Acme", "[COMPANY]"), ("Globex", "[COMPANY]")])
    assert reverse("[COMPANY] acquired [COMPANY]") == "Acme acquired Acme"


def test_build_reverse_substituter_length_descending_with_aliases_present() -> None:
    reverse = build_reverse_substituter(
        [
            ("long-original", "[X_0011]"),
            ("long-original", "X_0011"),
            ("short-original", "[X_001]"),
            ("short-original", "X_001"),
        ]
    )
    assert reverse("[X_0011] X_0011 [X_001] X_001") == (
        "long-original long-original short-original short-original"
    )
