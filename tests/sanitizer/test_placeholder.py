from corp_llm_gateway.sanitizer.placeholder import (
    apply_pairs,
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


def test_sort_placeholders_descending_length_stable() -> None:
    assert sort_placeholders_by_descending_length(["[A]", "[AAA]", "[AA]"]) == [
        "[AAA]",
        "[AA]",
        "[A]",
    ]
