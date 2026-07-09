import pytest

from corp_llm_gateway.rules import Rule, Rules, RulesParseError, parse


def test_parse_empty_text() -> None:
    assert parse("") == Rules(rules=())


def test_parse_blank_lines() -> None:
    assert parse("\n\n   \n") == Rules(rules=())


def test_parse_comments() -> None:
    text = "# header\n<!-- xml comment -->\n   # indented\n"
    assert parse(text) == Rules(rules=())


def test_parse_simple_rule() -> None:
    rules = parse("- alice → [NAME_001]")
    assert rules == Rules(rules=(Rule("alice", "[NAME_001]"),))


def test_parse_quoted_rule() -> None:
    rules = parse("- `alice` → `[NAME_001]`")
    assert rules == Rules(rules=(Rule("alice", "[NAME_001]"),))


def test_parse_mixed_quoting() -> None:
    rules = parse("- `secret-acl` → REDACTED")
    assert rules == Rules(rules=(Rule("secret-acl", "REDACTED"),))


def test_parse_multiple_rules() -> None:
    text = "- alice → [N1]\n# comment\n- bob → [N2]\n"
    assert parse(text) == Rules(rules=(Rule("alice", "[N1]"), Rule("bob", "[N2]")))


def test_parse_rejects_bare_ascii_arrow() -> None:
    with pytest.raises(RulesParseError, match="not ascii"):
        parse("- alice -> [NAME_001]")


def test_parse_rejects_malformed_with_line_number() -> None:
    text = "- alice → [N1]\n!!! garbage"
    with pytest.raises(RulesParseError, match="line 2"):
        parse(text)


def test_parse_rejects_empty_replacement() -> None:
    with pytest.raises(RulesParseError):
        parse("- alice → ")


def test_parse_rejects_empty_original() -> None:
    with pytest.raises(RulesParseError):
        parse("- → [NAME]")


def test_parse_strips_whitespace() -> None:
    assert parse("  -   alice   →   [N1]  ") == Rules(rules=(Rule("alice", "[N1]"),))


# ── equals separator (new canonical) — arrow above stays valid for back-compat ────


def test_parse_simple_rule_equals() -> None:
    assert parse("- alice = [NAME_001]") == Rules(rules=(Rule("alice", "[NAME_001]"),))


def test_parse_quoted_rule_equals() -> None:
    assert parse("- `alice` = `[NAME_001]`") == Rules(rules=(Rule("alice", "[NAME_001]"),))


def test_parse_mixed_quoting_equals() -> None:
    assert parse("- `secret-acl` = REDACTED") == Rules(rules=(Rule("secret-acl", "REDACTED"),))


def test_parse_strips_whitespace_equals() -> None:
    assert parse("  -   alice   =   [N1]  ") == Rules(rules=(Rule("alice", "[N1]"),))


def test_parse_equals_inside_quoted_value() -> None:
    # '=' inside backticks is content; the closing backtick ends the field, not the '='.
    assert parse("- `k=v` = `[SECRET]`") == Rules(rules=(Rule("k=v", "[SECRET]"),))


def test_parse_bare_colon_value_stays_bare() -> None:
    # Only '=' / '→' are separators, so a colon is legal in a bare (unquoted) original.
    assert parse("- db:5432 = [HOST]") == Rules(rules=(Rule("db:5432", "[HOST]"),))


def test_parse_both_separators_in_one_file() -> None:
    text = "- alice = [N1]\n- bob → [N2]\n"
    assert parse(text) == Rules(rules=(Rule("alice", "[N1]"), Rule("bob", "[N2]")))


def test_parse_bare_equals_value_mis_splits() -> None:
    # A bare original containing '=' splits at the first '=' — authors must quote it.
    # Pinning the documented limitation, not endorsing it.
    assert parse("- k=v = X") == Rules(rules=(Rule("k", "v = X"),))


def test_parse_ascii_arrow_with_quoted_equals_value_raises() -> None:
    # '->' typo whose value contains '=' must NOT silently mis-parse: the leading-backtick
    # exclusion makes the whole rule fail rather than matching a bogus bare original.
    with pytest.raises(RulesParseError):
        parse("- `a=b` -> `[X]`")
