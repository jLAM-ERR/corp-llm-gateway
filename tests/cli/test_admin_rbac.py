"""RBAC enforcement tests for gateway-admin mutating commands."""

from __future__ import annotations

import json
import time

import httpx
import jwt
import pytest

from corp_llm_gateway.auth.rbac import OperatorDenied, verify_operator
from corp_llm_gateway.cli.admin import main
from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import InMemoryMappingStore

_SECRET = "test-rbac-secret-key-for-hs256-gate"  # ≥32 bytes per RFC 7518 §3.2
_ALG = "HS256"


def _make_token(
    roles: list[str] | None = None,
    flat_roles: list[str] | None = None,
    scope: str | None = None,
    exp_offset: int = 3600,
) -> str:
    payload: dict[str, object] = {"sub": "test-user", "exp": int(time.time()) + exp_offset}
    if roles is not None:
        payload["realm_access"] = {"roles": roles}
    if flat_roles is not None:
        payload["roles"] = flat_roles
    if scope is not None:
        payload["scope"] = scope
    return jwt.encode(payload, _SECRET, algorithm=_ALG)


@pytest.fixture(autouse=True)
def _jwt_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_GATEWAY_OIDC_KEY", _SECRET)
    monkeypatch.setenv("CORP_GATEWAY_OIDC_ALG", _ALG)
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")


# ---------------------------------------------------------------------------
# verify_operator unit tests
# ---------------------------------------------------------------------------


def test_operator_via_realm_access_roles_allowed() -> None:
    verify_operator(_make_token(roles=["gateway:operator"]))


def test_operator_via_flat_roles_allowed() -> None:
    verify_operator(_make_token(flat_roles=["gateway:operator"]))


def test_operator_via_scope_string_allowed() -> None:
    verify_operator(_make_token(scope="openid gateway:operator profile"))


def test_missing_role_denied() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator(_make_token(roles=["some:other-role"]))


def test_empty_roles_denied() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator(_make_token(roles=[]))


def test_empty_token_denied() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator("")


def test_malformed_token_denied() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator("not.a.valid.jwt")


def test_expired_token_denied() -> None:
    with pytest.raises(OperatorDenied):
        verify_operator(_make_token(roles=["gateway:operator"], exp_offset=-10))


def test_wrong_signature_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    # token signed with a different key (also ≥32 bytes)
    token = jwt.encode(
        {"sub": "u", "realm_access": {"roles": ["gateway:operator"]}, "exp": int(time.time()) + 60},
        "different-secret-key-that-is-long-enough",
        algorithm=_ALG,
    )
    with pytest.raises(OperatorDenied):
        verify_operator(token)


# ---------------------------------------------------------------------------
# CLI integration — team subcommands
# ---------------------------------------------------------------------------


def test_team_create_with_operator_token_returns_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = _make_token(roles=["gateway:operator"])
    rc = main(["--token", token, "team", "create", "--team-id", "t1", "--name", "Team One"])
    assert rc == 0
    assert "team.create" in capsys.readouterr().out


def test_team_create_without_operator_role_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = _make_token(roles=["developer"])
    rc = main(["--token", token, "team", "create", "--team-id", "t1", "--name", "Team One"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "gateway:operator" in captured.err
    assert "team.create" not in captured.out


def test_team_set_rules_without_token_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["team", "set-rules", "--team-id", "t1", "--from-file", "rules.md"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


def test_team_set_retention_without_token_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["team", "set-retention", "--team-id", "t1"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


def test_team_set_retention_with_operator_token_returns_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = _make_token(roles=["gateway:operator"])
    rc = main(["--token", token, "team", "set-retention", "--team-id", "t1"])
    assert rc == 0
    assert "team.set_retention" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CLI integration — token revoke
# ---------------------------------------------------------------------------


def test_token_revoke_with_operator_token_returns_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = _make_token(roles=["gateway:operator"])
    rc = main(["--token", token, "token", "revoke", "--user", "alice"])
    assert rc == 0
    assert "token.revoke" in capsys.readouterr().out


def test_token_revoke_without_role_returns_2_no_side_effect(
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = _make_token(roles=[])
    rc = main(["--token", token, "token", "revoke", "--user", "alice"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "gateway:operator" in captured.err
    assert "token.revoke" not in captured.out


def test_token_revoke_missing_token_returns_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["token", "revoke", "--user", "alice"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Denial message must not expose the raw token text
# ---------------------------------------------------------------------------


def test_denial_message_contains_no_raw_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad_token = _make_token(roles=["auditor"])
    rc = main(["--token", bad_token, "team", "create", "--team-id", "t1", "--name", "x"])
    assert rc == 2
    captured = capsys.readouterr()
    assert bad_token not in captured.err
    assert bad_token not in captured.out


# ---------------------------------------------------------------------------
# sanitize is diagnostic — no RBAC gate
# ---------------------------------------------------------------------------


class _NullRules(RulesLoader):
    async def load(self, team_id: str) -> Rules:
        return Rules(rules=())


def _no_op_orchestrator() -> tuple[SanitizationOrchestrator, CorpLlmClient]:
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

    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    corp_llm = CorpLlmClient("https://corp-llm.example", model="m", http=http)
    orch = SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _NullRules())
    return orch, corp_llm


def test_sanitize_works_without_operator_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.example/v1")
    monkeypatch.setattr(
        "corp_llm_gateway.cli.admin._build_orchestrator",
        lambda model: _no_op_orchestrator(),
    )
    # No --token; RBAC=1 from autouse fixture; sanitize must still succeed
    rc = main(["sanitize", "hello world"])
    assert rc == 0
    assert "BEFORE: hello world" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CORP_GATEWAY_RBAC=0 bypass
# ---------------------------------------------------------------------------


def test_rbac_disabled_allows_without_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "0")
    rc = main(["team", "create", "--team-id", "t1", "--name", "Dev Team"])
    assert rc == 0
    assert "team.create" in capsys.readouterr().out
