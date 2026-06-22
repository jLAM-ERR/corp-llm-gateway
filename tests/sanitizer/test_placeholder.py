from corp_llm_gateway.sanitizer.placeholder import (
    apply_pairs,
    find_placeholder_literals,
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
