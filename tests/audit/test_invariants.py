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
