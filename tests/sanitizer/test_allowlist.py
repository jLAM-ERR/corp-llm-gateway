"""Tests for DP-8: test-data allowlist that never suppresses secrets."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from corp_llm_gateway import config
from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.allowlist import SECRET_LABELS, Allowlist
from corp_llm_gateway.storage import InMemoryMappingStore

# ---------------------------------------------------------------------------
# filter_pairs — core drop logic
# ---------------------------------------------------------------------------


def test_allowlisted_person_drops_pair() -> None:
    al = Allowlist(["Иван Тестов"])
    assert al.filter_pairs((("Иван Тестов", "[PERSON_001]"),)) == ()


def test_allowlisted_email_drops_pair() -> None:
    al = Allowlist(["test@fixture.local"])
    assert al.filter_pairs((("test@fixture.local", "[EMAIL_001]"),)) == ()


def test_non_allowlisted_person_kept() -> None:
    al = Allowlist(["Иван Тестов"])
    pairs = (("Алексей Иванов", "[PERSON_001]"),)
    assert al.filter_pairs(pairs) == pairs


# ---------------------------------------------------------------------------
# filter_pairs — secrets cannot be suppressed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("API_KEY", "sk-test-deadbeef"),
        ("TOKEN", "ghp_test_token"),
        ("PASSWORD", "hunter2"),
        ("JWT", "eyJhbGci.eyJzdWI.sig"),
        ("PEM_PRIVATE_KEY", "-----BEGIN PRIVATE KEY-----"),
        ("SECRET", "super-secret-value"),
    ],
)
def test_allowlisted_secret_label_not_dropped(label: str, value: str) -> None:
    al = Allowlist([value])
    pair = ((value, f"[{label}_001]"),)
    assert al.filter_pairs(pair) == pair, f"secret label {label} must never be suppressed"


# ---------------------------------------------------------------------------
# filter_pairs — exact-match only
# ---------------------------------------------------------------------------


def test_exact_match_only_substring_still_redacted() -> None:
    # "John" is a substring of the allowlisted "John Doe" — not a match.
    al = Allowlist(["John Doe"])
    pairs = (("John", "[PERSON_001]"),)
    assert al.filter_pairs(pairs) == pairs


def test_exact_match_only_superset_still_redacted() -> None:
    # "John Doe Jr." is a superset of the allowlisted "John Doe" — not a match.
    al = Allowlist(["John Doe"])
    pairs = (("John Doe Jr.", "[PERSON_001]"),)
    assert al.filter_pairs(pairs) == pairs


# ---------------------------------------------------------------------------
# filter_pairs — edge cases
# ---------------------------------------------------------------------------


def test_unparseable_placeholder_treated_as_non_secret_drops() -> None:
    # If placeholder doesn't match the label pattern, treat as non-secret → drop.
    al = Allowlist(["fixture-value"])
    pairs = (("fixture-value", "custom_replacement"),)
    assert al.filter_pairs(pairs) == ()


def test_empty_allowlist_returns_all_pairs_unchanged() -> None:
    al = Allowlist([])
    pairs = (("alice", "[PERSON_001]"), ("bob@x.local", "[EMAIL_001]"))
    assert al.filter_pairs(pairs) == pairs


def test_mixed_pairs_correct_filtering() -> None:
    # Only allowlisted + non-secret entries are dropped.
    al = Allowlist(["Иван Тестов", "sk-live-key"])
    pairs = (
        ("Иван Тестов", "[PERSON_001]"),  # allowlisted + non-secret → drop
        ("sk-live-key", "[API_KEY_001]"),  # allowlisted + secret → keep
        ("prod.corp.lan", "[HOST_001]"),  # not allowlisted → keep
    )
    result = al.filter_pairs(pairs)
    assert result == (
        ("sk-live-key", "[API_KEY_001]"),
        ("prod.corp.lan", "[HOST_001]"),
    )


def test_secret_labels_set_exactly_matches_spec() -> None:
    assert (
        frozenset({"API_KEY", "TOKEN", "PASSWORD", "JWT", "PEM_PRIVATE_KEY", "SECRET"})
        == SECRET_LABELS
    )


# ---------------------------------------------------------------------------
# from_config
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_config() -> None:
    config.reset_cache()


def test_from_config_empty_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORP_LLM_TESTDATA_ALLOWLIST", raising=False)
    monkeypatch.delenv("CORP_LLM_TESTDATA_ALLOWLIST_FILE", raising=False)
    al = Allowlist.from_config()
    pairs = (("alice", "[PERSON_001]"),)
    assert al.filter_pairs(pairs) == pairs


def test_from_config_inline_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_TESTDATA_ALLOWLIST", "Иван Тестов, test@fixture.local")
    monkeypatch.delenv("CORP_LLM_TESTDATA_ALLOWLIST_FILE", raising=False)
    al = Allowlist.from_config()
    pairs = (
        ("Иван Тестов", "[PERSON_001]"),
        ("test@fixture.local", "[EMAIL_001]"),
        ("other", "[PERSON_002]"),
    )
    assert al.filter_pairs(pairs) == (("other", "[PERSON_002]"),)


def test_from_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    allow_file = tmp_path / "allowlist.txt"
    allow_file.write_text("Иван Тестов\ntest@fixture.local\n")
    monkeypatch.delenv("CORP_LLM_TESTDATA_ALLOWLIST", raising=False)
    monkeypatch.setenv("CORP_LLM_TESTDATA_ALLOWLIST_FILE", str(allow_file))
    al = Allowlist.from_config()
    pairs = (
        ("Иван Тестов", "[PERSON_001]"),
        ("test@fixture.local", "[EMAIL_001]"),
    )
    assert al.filter_pairs(pairs) == ()


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


class _StaticRulesLoader(RulesLoader):
    async def load(self, team_id: str) -> Rules:
        return Rules(rules=())


def _mock_client(pairs: list[tuple[str, str]]) -> CorpLlmClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": SANITIZE_TOOL_NAME,
                                        "arguments": json.dumps(
                                            {
                                                "pairs": [
                                                    {"original": o, "replacement": r}
                                                    for o, r in pairs
                                                ]
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CorpLlmClient("https://corp-llm.example", model="m", http=http)


async def test_orchestrator_allowlist_test_pdn_passes_secret_stays() -> None:
    """Allowlisted test ПДн appears un-redacted; secret remains redacted."""
    al = Allowlist(["Иван Тестов"])
    client = _mock_client(
        [
            ("Иван Тестов", "[PERSON_001]"),  # test ПДн → allowlisted → passes through
            ("sk-live-key-999", "[API_KEY_001]"),  # secret → must stay redacted
        ]
    )
    orch = SanitizationOrchestrator(
        client, InMemoryMappingStore(), _StaticRulesLoader(), allowlist=al
    )
    result = await orch.sanitize(
        "Data: Иван Тестов, key: sk-live-key-999",
        team_id="t1",
        conversation_id="c1",
    )
    assert "Иван Тестов" in result.sanitized_text
    assert "sk-live-key-999" not in result.sanitized_text
    assert "[API_KEY_001]" in result.sanitized_text


async def test_orchestrator_no_allowlist_unchanged_behavior() -> None:
    """allowlist=None: all pairs redacted (existing behavior preserved)."""
    client = _mock_client([("Иван Тестов", "[PERSON_001]")])
    orch = SanitizationOrchestrator(
        client,
        InMemoryMappingStore(),
        _StaticRulesLoader(),
        # allowlist defaults to None
    )
    result = await orch.sanitize("Иван Тестов", team_id="t1", conversation_id="c1")
    assert "Иван Тестов" not in result.sanitized_text
    assert "[PERSON_001]" in result.sanitized_text
