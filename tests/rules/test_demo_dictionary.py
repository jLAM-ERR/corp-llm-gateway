from pathlib import Path

from corp_llm_gateway.rules import parse


def test_demo_dictionary_matches_shared_ru_proxy_rules() -> None:
    path = (
        Path(__file__).resolve().parents[2] / "docker" / "demo-litellm" / "rules" / "demo-team.md"
    )
    rules = parse(path.read_text(encoding="utf-8"))

    assert {rule.pattern: rule.replacement for rule in rules.rules} == {
        "kdir": "companynameabc",
        "betadirect": "companynameabd",
        "beta direct": "company name abe",
        "zephyr ledger": "confidential project acn",
        "db-legacy-7": "internalhostaco",
    }
