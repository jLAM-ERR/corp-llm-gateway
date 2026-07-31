"""Recursive NEVER-fields gate (F10): a NEVER key nested inside a dict/list
must be caught, not only top-level keys."""

import pytest

from corp_llm_gateway.audit import NeverFieldPresentError, assert_no_never_fields


def test_clean_nested_record_passes() -> None:
    assert_no_never_fields(
        {
            "request_id": "r1",
            "meta": {"team_id": "t1", "counts": {"EMAIL": 2}},
            "items": [{"label": "NAME"}, {"label": "EMAIL"}],
        }
    )


def test_never_key_nested_in_dict_is_caught() -> None:
    with pytest.raises(NeverFieldPresentError, match="mapping"):
        assert_no_never_fields({"request_id": "r1", "debug": {"mapping": {"a": "b"}}})


def test_never_key_nested_in_list_is_caught() -> None:
    with pytest.raises(NeverFieldPresentError, match="pairs"):
        assert_no_never_fields({"request_id": "r1", "events": [{"ok": 1}, {"pairs": []}]})


def test_deeply_nested_never_key_is_caught() -> None:
    with pytest.raises(NeverFieldPresentError, match="original_content"):
        assert_no_never_fields({"a": {"b": [{"c": {"original_content": "secret"}}]}})


def test_nested_header_style_never_key_is_caught() -> None:
    with pytest.raises(NeverFieldPresentError, match="X-Corp-Auth"):
        assert_no_never_fields({"headers": {"X-Corp-Auth": "tok"}})


def test_top_level_never_key_still_caught() -> None:
    with pytest.raises(NeverFieldPresentError):
        assert_no_never_fields({"mapping": []})


def test_string_value_is_not_walked_as_chars() -> None:
    # A string value that happens to contain a NEVER word is content, not a key.
    assert_no_never_fields({"note": "the mapping was applied"})


def test_extra_headers_field_is_never_allowed() -> None:
    with pytest.raises(NeverFieldPresentError, match="extra_headers"):
        assert_no_never_fields(
            {"request_id": "r1", "extra_headers": {"ChatGPT-Account-Id": "acct-1"}}
        )


def test_api_key_field_is_never_allowed_at_top_level() -> None:
    # The Codex auth bridge writes the developer's ChatGPT OAuth bearer to
    # data["api_key"] (litellm_hook.py) — a new raw-credential surface.
    with pytest.raises(NeverFieldPresentError, match="api_key"):
        assert_no_never_fields({"request_id": "r1", "api_key": "sk-oauth-secret"})


def test_api_key_field_nested_under_benign_field_is_caught() -> None:
    with pytest.raises(NeverFieldPresentError, match="api_key"):
        assert_no_never_fields({"request_id": "r1", "debug": {"api_key": "sk-oauth-secret"}})


def test_finding_label_counts_detector_labels_pass_the_gate() -> None:
    """Regression pin: finding_label_counts maps a detector LABEL (API_KEY, JWT,
    PASSWORD, ...) to a count — those keys are data, not schema field names, and
    must not be matched against NEVER_FIELDS."""
    assert_no_never_fields({"request_id": "r1", "finding_label_counts": {"API_KEY": 3, "JWT": 1}})


def test_never_field_nested_as_value_inside_finding_label_counts_is_still_caught() -> None:
    """finding_label_counts' declared type is dict[str, int], so this shape can't
    occur in practice — the test pins the gate's VALUE walk, not the schema: a
    NEVER key nested one level deeper must still be caught, proving the
    data-keyed-field carve-out doesn't blind the walk to its subtree."""
    with pytest.raises(NeverFieldPresentError, match="authorization"):
        assert_no_never_fields(
            {
                "request_id": "r1",
                "finding_label_counts": {"API_KEY": {"authorization": "Bearer x"}},
            }
        )


def test_never_field_nested_under_a_list_inside_data_keyed_field_is_still_caught() -> None:
    """Minor: `_walk_values_only`'s list branch used to recurse into ITSELF
    instead of the normal `_walk`, so a NEVER key nested under a list inside a
    DATA_KEYED_FIELDS value was exempted from key matching at EVERY depth
    below that list, not just the data-keyed field's own immediate key layer
    (its docstring's promise)."""
    with pytest.raises(NeverFieldPresentError, match="mapping"):
        assert_no_never_fields(
            {
                "request_id": "r1",
                "finding_label_counts": [{"mapping": "leaked-original"}],
            }
        )
