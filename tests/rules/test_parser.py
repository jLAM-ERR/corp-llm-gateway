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


def test_parse_rejects_ascii_arrow() -> None:
    with pytest.raises(RulesParseError, match="em-dash"):
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
