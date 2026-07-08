"""Tests for gateway-admin sanitize subcommand."""

import json
from typing import Any

import httpx
import pytest

from corp_llm_gateway import config
from corp_llm_gateway.cli.admin import main
from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import InMemoryMappingStore


class _StaticRules(RulesLoader):
    async def load(self, team_id: str) -> Rules:
        return Rules(rules=())


def _corp_llm_returning(pairs: list[tuple[str, str]]) -> CorpLlmClient:
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


def _corp_llm_error() -> CorpLlmClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CorpLlmClient("https://corp-llm.example", model="m", http=http)


def _mock_orchestrator(
    pairs: list[tuple[str, str]],
) -> tuple[SanitizationOrchestrator, CorpLlmClient]:
    corp_llm = _corp_llm_returning(pairs)
    orch = SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _StaticRules())
    return orch, corp_llm


def _mock_orchestrator_error() -> tuple[SanitizationOrchestrator, CorpLlmClient]:
    corp_llm = _corp_llm_error()
    orch = SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _StaticRules())
    return orch, corp_llm


@pytest.fixture(autouse=True)
def _clear_config_cache() -> None:
    config.reset_cache()


def test_sanitize_happy_path_human(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator([("alice@example.com", "[EMAIL_001]")]),
    )
    rc = main(["sanitize", "send a note to alice@example.com"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "BEFORE: send a note to alice@example.com" in out
    assert "[EMAIL_001]" in out
    assert "redactions: 1" in out
    assert "alice@example.com -> [EMAIL_001]" in out


def test_sanitize_happy_path_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator([("secret-key", "[SECRET_001]")]),
    )
    rc = main(["sanitize", "--json", "use secret-key to authenticate"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["before"] == "use secret-key to authenticate"
    assert "[SECRET_001]" in payload["after"]
    assert payload["redaction_count"] == 1
    assert payload["pairs"] == [["secret-key", "[SECRET_001]"]]
    assert isinstance(payload["cache_a_hit"], bool)
    assert isinstance(payload["skipped"], bool)


def test_sanitize_no_redactions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator([]),
    )
    rc = main(["sanitize", "hello world"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "BEFORE: hello world" in out
    assert "AFTER : hello world" in out
    assert "redactions: 0" in out


def test_sanitize_missing_endpoint_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CORP_LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("CORP_LLM_GATEWAY_CONFIG_FILE", raising=False)
    rc = main(["sanitize", "some text"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "CORP_LLM_ENDPOINT" in err


def test_sanitize_corp_llm_error_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator_error(),
    )
    rc = main(["sanitize", "some text"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "corp sanitization LLM unavailable" in captured.err
    # no partial/original output on stdout (single capture — a second
    # readouterr() would drain the buffer and assert vacuously)
    assert "some text" not in captured.out


def test_sanitize_team_id_forwarded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    seen: dict[str, str] = {}

    class _CapturingRules(RulesLoader):
        async def load(self, team_id: str) -> Rules:
            seen["team_id"] = team_id
            return Rules(rules=())

    def _build() -> tuple[SanitizationOrchestrator, CorpLlmClient]:
        corp_llm = _corp_llm_returning([])
        orch = SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _CapturingRules())
        return orch, corp_llm

    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _build(),
    )
    rc = main(["sanitize", "--team-id", "my-team", "hello"])
    assert rc == 0
    # The CLI must forward --team-id through to the orchestrator (the rules
    # loader receives it), not silently use the default.
    assert seen["team_id"] == "my-team"


def test_sanitize_json_multiple_pairs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator(
            [("alice", "[NAME_001]"), ("bob@corp.com", "[EMAIL_002]")]
        ),
    )
    rc = main(["sanitize", "--json", "alice emailed bob@corp.com"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["redaction_count"] == 2
    assert len(payload["pairs"]) == 2


# ---------------------------------------------------------------------------
# Hardened / adversarial tests
# ---------------------------------------------------------------------------


def _corp_llm_unreachable() -> CorpLlmClient:
    """Transport that always raises ConnectTimeout — corp LLM unreachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CorpLlmClient("https://corp-llm.example", model="m", http=http)


def _corp_llm_garbage() -> CorpLlmClient:
    """Returns 200 but with content that no strategy can parse (all-strategies-fail path)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "I cannot help with that.",
                        }
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CorpLlmClient("https://corp-llm.example", model="m", http=http)


def _mock_orchestrator_unreachable() -> tuple[SanitizationOrchestrator, CorpLlmClient]:
    corp_llm = _corp_llm_unreachable()
    orch = SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _StaticRules())
    return orch, corp_llm


def _mock_orchestrator_garbage() -> tuple[SanitizationOrchestrator, CorpLlmClient]:
    corp_llm = _corp_llm_garbage()
    orch = SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _StaticRules())
    return orch, corp_llm


# 1. ConnectTimeout → CorpLlmHttpError → exit 1, stderr names the exception type
def test_sanitize_connect_timeout_exits_1_names_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator_unreachable(),
    )
    rc = main(["sanitize", "some sensitive text"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "corp sanitization LLM unavailable" in err
    assert "CorpLlmHttpError" in err


# 2. All strategies fail → AllStrategiesFailedError → exit 1
def test_sanitize_all_strategies_failed_exits_1(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator_garbage(),
    )
    rc = main(["sanitize", "some sensitive text"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "corp sanitization LLM unavailable" in err
    assert "AllStrategiesFailedError" in err


# 3a. Zero-redaction passthrough — human output: AFTER == BEFORE, redactions: 0
def test_sanitize_zero_redactions_human_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator([]),
    )
    rc = main(["sanitize", "public info only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "BEFORE: public info only" in out
    assert "AFTER : public info only" in out
    assert "redactions: 0" in out


# 3b. Zero-redaction passthrough — json output: redaction_count == 0, pairs == []
def test_sanitize_zero_redactions_json_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator([]),
    )
    rc = main(["sanitize", "--json", "public info only"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["redaction_count"] == 0
    assert payload["pairs"] == []
    assert payload["before"] == payload["after"]


# 4a. Multiple pairs — human output lists each orig -> repl
def test_sanitize_multiple_pairs_human_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    pairs = [("alice@corp.com", "[EMAIL_001]"), ("secret-pw", "[SECRET_002]")]
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator(pairs),
    )
    rc = main(["sanitize", "alice@corp.com uses secret-pw"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alice@corp.com -> [EMAIL_001]" in out
    assert "secret-pw -> [SECRET_002]" in out
    assert "redactions: 2" in out


# 4b. Length-descending substitution: longer original is a prefix of another;
#     the shorter must not shadow it in the AFTER text.
def test_sanitize_length_descending_substitution(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    # "john.smith@corp.com" contains "john" — if short replaces first,
    # the email placeholder is corrupted.
    pairs = [
        ("john", "[NAME_001]"),
        ("john.smith@corp.com", "[EMAIL_002]"),
    ]
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator(pairs),
    )
    rc = main(["sanitize", "--json", "contact john.smith@corp.com and john"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    after = payload["after"]
    # The email must be fully replaced (not "[NAME_001].smith@corp.com")
    assert "[EMAIL_002]" in after
    assert "[NAME_001]" in after
    assert "john.smith@corp.com" not in after
    assert "@corp.com" not in after


# 5. JSON shape: required keys exist with correct types; pairs is list of 2-element lists
def test_sanitize_json_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator([("tok-abc", "[TOKEN_001]")]),
    )
    rc = main(["sanitize", "--json", "use tok-abc"])
    assert rc == 0
    payload: Any = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    for key in ("before", "after", "redaction_count", "pairs", "cache_a_hit", "skipped"):
        assert key in payload, f"missing key: {key}"
    assert isinstance(payload["before"], str)
    assert isinstance(payload["after"], str)
    assert isinstance(payload["redaction_count"], int)
    assert isinstance(payload["cache_a_hit"], bool)
    assert isinstance(payload["skipped"], bool)
    assert isinstance(payload["pairs"], list)
    for item in payload["pairs"]:
        assert isinstance(item, list)
        assert len(item) == 2
        assert isinstance(item[0], str)
        assert isinstance(item[1], str)


# 6. Missing CORP_LLM_ENDPOINT → exit 2, stderr mentions the setting name
def test_sanitize_missing_endpoint_no_config_file_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CORP_LLM_ENDPOINT", raising=False)
    monkeypatch.delenv("CORP_LLM_GATEWAY_CONFIG_FILE", raising=False)
    config.reset_cache()
    rc = main(["sanitize", "any text"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "CORP_LLM_ENDPOINT" in err


# 7. corp_llm.aclose() is called even when sanitize raises (error path cleanup)
def test_sanitize_aclose_called_on_error_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    aclose_called = False

    class _TrackingClient(CorpLlmClient):
        async def aclose(self) -> None:
            nonlocal aclose_called
            aclose_called = True
            await super().aclose()

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("", request=request)

    tracking_http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    corp_llm = _TrackingClient("https://corp-llm.example", model="m", http=tracking_http)
    orch = SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _StaticRules())

    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: (orch, corp_llm),
    )
    rc = main(["sanitize", "some text"])
    assert rc == 1
    assert aclose_called, "corp_llm.aclose() must be called on error path"


# 7b. corp_llm.aclose() is called on the success path too
def test_sanitize_aclose_called_on_success_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    aclose_called = False

    class _TrackingClient(CorpLlmClient):
        async def aclose(self) -> None:
            nonlocal aclose_called
            aclose_called = True
            await super().aclose()

    def _handler(request: httpx.Request) -> httpx.Response:
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
                                        "arguments": json.dumps({"pairs": []}),
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    tracking_http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    corp_llm = _TrackingClient("https://corp-llm.example", model="m", http=tracking_http)
    orch = SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _StaticRules())

    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: (orch, corp_llm),
    )
    rc = main(["sanitize", "clean text"])
    assert rc == 0
    assert aclose_called, "corp_llm.aclose() must be called on success path"


# 8. Error paths produce no plaintext on stdout (no original leaks via stdout)
def test_sanitize_error_no_stdout_leak(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator_unreachable(),
    )
    rc = main(["sanitize", "my-secret-text"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "my-secret-text" not in out


# 9. Oversize payload (F1): the pre-pass now fails CLOSED — the payload is
#    refused, never sent unredacted. The human output must say BLOCKED.
def test_sanitize_oversize_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _mock_orchestrator([]),
    )
    big = "A" * (101 * 1024)  # over the 100 KB threshold → orchestrator fails closed
    rc = main(["sanitize", big])
    assert rc == 1
    captured = capsys.readouterr()
    assert "BLOCKED" in captured.err
    # The refusal must not echo the oversize payload back.
    assert big not in captured.out
    assert big not in captured.err
