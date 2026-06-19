import re

from corp_llm_gateway.rules.loader import RulesParseError
from corp_llm_gateway.rules.models import Rule, Rules

_RULE_RE = re.compile(
    r"""^
    \s*-\s*                       # bullet
    (?:`(?P<orig_q>[^`]+)`|(?P<orig>[^\s→][^→]*?))
    \s*→\s*
    (?:`(?P<rep_q>[^`]+)`|(?P<rep>[^\s].*?))
    \s*$""",
    re.VERBOSE,
)


def parse(text: str) -> Rules:
    rules: list[Rule] = []
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.rstrip()
        stripped = line.lstrip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("<!--"):
            continue
        if "->" in stripped and "→" not in stripped:
            raise RulesParseError(f"line {line_no}: use em-dash separator → (U+2192), not ascii ->")
        m = _RULE_RE.match(line)
        if not m:
            raise RulesParseError(f"line {line_no}: not a valid rule: {line!r}")
        original = (m.group("orig_q") or m.group("orig") or "").strip()
        replacement = (m.group("rep_q") or m.group("rep") or "").strip()
        if not original or not replacement:
            raise RulesParseError(f"line {line_no}: empty original or replacement")
        rules.append(Rule(pattern=original, replacement=replacement))
    return Rules(rules=tuple(rules))
