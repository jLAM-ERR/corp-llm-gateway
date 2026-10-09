"""RBAC enforcement tests for gateway-admin mutating commands.

Operator-token signing uses RS256 (F11), so tests that need a valid token pull
the ``make_token`` fixture, which skips when ``cryptography`` (the 'oidc' extra)
is absent. No-token / bypass / sanitize paths run everywhere. Pure
``verify_operator`` unit tests live in ``tests/auth/test_rbac.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import jwt
import pytest

from corp_llm_gateway import config
from corp_llm_gateway.cli.admin import main
from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.team_config import InMemoryTeamConfigStore, TeamConfig
from corp_llm_gateway.tokens import InMemoryTokenStore

_AUDIENCE = "corp-llm-gateway"
_ISSUER = "https://keycloak.corp.lan/realms/corp"

MakeToken = Callable[..., str]


@pytest.fixture(autouse=True)
def _rbac_env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    empty = tmp_path / "config.toml"
    empty.write_text("")
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(empty))
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")
    for name in (
        "CORP_GATEWAY_OIDC_KEY",
        "CORP_GATEWAY_OIDC_AUDIENCE",
        "CORP_GATEWAY_OIDC_ISSUER",
        "CORP_GATEWAY_ADMIN_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    config.reset_cache()
    yield
    config.reset_cache()


@pytest.fixture
def make_token(_rbac_env: None, monkeypatch: pytest.MonkeyPatch) -> MakeToken:
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    monkeypatch.setenv("CORP_GATEWAY_OIDC_KEY", pub_pem)
    monkeypatch.setenv("CORP_GATEWAY_OIDC_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("CORP_GATEWAY_OIDC_ISSUER", _ISSUER)

    def _make(
        roles: list[str] | None = None,
        flat_roles: list[str] | None = None,
        scope: str | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "sub": "test-user",
            "exp": int(time.time()) + 3600,
            "aud": _AUDIENCE,
            "iss": _ISSUER,
        }
        if roles is not None:
            payload["realm_access"] = {"roles": roles}
        if flat_roles is not None:
            payload["roles"] = flat_roles
        if scope is not None:
            payload["scope"] = scope
        return jwt.encode(payload, priv_pem, algorithm="RS256")

    return _make


# ---------------------------------------------------------------------------
# CLI integration — team subcommands
# ---------------------------------------------------------------------------


def test_team_create_with_operator_token_returns_0(
    make_token: MakeToken,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("corp_llm_gateway.cli.admin._team_store", lambda: InMemoryTeamConfigStore())
    token = make_token(roles=["gateway:operator"])
    rc = main(["--token", token, "team", "create", "--team-id", "t1", "--name", "Team One"])
    assert rc == 0
    assert "team created: t1" in capsys.readouterr().out


def test_team_create_without_operator_role_returns_2(
    make_token: MakeToken,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = make_token(roles=["developer"])
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
    make_token: MakeToken,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = InMemoryTeamConfigStore()
    asyncio.run(store.upsert(TeamConfig(team_id="t1", name="One")))
    monkeypatch.setattr("corp_llm_gateway.cli.admin._team_store", lambda: store)
    token = make_token(roles=["gateway:operator"])
    rc = main(["--token", token, "team", "set-retention", "--team-id", "t1"])
    assert rc == 0
    assert "retention" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# CLI integration — token revoke
# ---------------------------------------------------------------------------


def test_token_revoke_with_operator_token_returns_0(
    make_token: MakeToken,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("corp_llm_gateway.cli.admin._token_store", lambda: InMemoryTokenStore())
    token = make_token(roles=["gateway:operator"])
    rc = main(["--token", token, "token", "revoke", "--user", "alice"])
    assert rc == 0
    assert "revoked" in capsys.readouterr().out


def test_token_revoke_without_role_returns_2_no_side_effect(
    make_token: MakeToken,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = make_token(roles=[])
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
# CLI integration — token issue: the global --token JWT still reaches RBAC
# with and without the token-level --value
# ---------------------------------------------------------------------------

_VALUE = "local-team-token-7Qx2"


@pytest.mark.parametrize("with_value", [True, False], ids=["with-value", "without-value"])
def test_token_issue_with_operator_token_returns_0(
    with_value: bool,
    make_token: MakeToken,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = InMemoryTokenStore()
    monkeypatch.setattr("corp_llm_gateway.cli.admin._token_store", lambda: store)
    token = make_token(roles=["gateway:operator"])
    argv = ["--token", token, "token", "issue", "--user", "alice", "--team", "t1"]
    if with_value:
        argv += ["--value", _VALUE]
    assert main(argv) == 0
    rows = asyncio.run(store.list_tokens("alice"))
    assert len(rows) == 1
    if with_value:
        assert rows[0].corp_token == _VALUE
        assert _VALUE not in capsys.readouterr().out


@pytest.mark.parametrize("with_value", [True, False], ids=["with-value", "without-value"])
def test_token_issue_without_operator_role_returns_2_no_side_effect(
    with_value: bool,
    make_token: MakeToken,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = InMemoryTokenStore()
    monkeypatch.setattr("corp_llm_gateway.cli.admin._token_store", lambda: store)
    token = make_token(roles=["developer"])
    argv = ["--token", token, "token", "issue", "--user", "alice", "--team", "t1"]
    if with_value:
        argv += ["--value", _VALUE]
    assert main(argv) == 2
    captured = capsys.readouterr()
    assert "gateway:operator" in captured.err
    assert _VALUE not in captured.out + captured.err
    assert asyncio.run(store.list_tokens()) == ()


@pytest.mark.parametrize("with_value", [True, False], ids=["with-value", "without-value"])
def test_token_issue_without_token_returns_2(
    with_value: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = InMemoryTokenStore()
    monkeypatch.setattr("corp_llm_gateway.cli.admin._token_store", lambda: store)
    argv = ["token", "issue", "--user", "alice", "--team", "t1"]
    if with_value:
        argv += ["--value", _VALUE]
    assert main(argv) == 2
    assert "gateway:operator" in capsys.readouterr().err
    assert asyncio.run(store.list_tokens()) == ()


# ---------------------------------------------------------------------------
# Denial message must not expose the raw token text
# ---------------------------------------------------------------------------


def test_denial_message_contains_no_raw_token(
    make_token: MakeToken,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad_token = make_token(roles=["auditor"])
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
    monkeypatch.setattr("corp_llm_gateway.cli.admin._team_store", lambda: InMemoryTeamConfigStore())
    rc = main(["team", "create", "--team-id", "t1", "--name", "Dev Team"])
    assert rc == 0
    assert "team created" in capsys.readouterr().out


@pytest.mark.parametrize("corp_env", ["", "production"])
def test_rbac_bypass_does_not_depend_on_corp_env(
    corp_env: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "0")
    monkeypatch.setenv("CORP_ENV", corp_env)
    store = InMemoryTeamConfigStore()
    monkeypatch.setattr("corp_llm_gateway.cli.admin._team_store", lambda: store)
    rc = main(["team", "create", "--team-id", "local", "--name", "local", "--if-absent"])
    assert rc == 0
    assert "team created: local" in capsys.readouterr().out


@pytest.mark.parametrize("raw", ["false", "no", "off"])
def test_rbac_bypass_needs_exactly_0(
    raw: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", raw)
    rc = main(["team", "create", "--team-id", "t1", "--name", "X"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI integration — db init
# ---------------------------------------------------------------------------


def test_db_init_without_token_returns_2(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["db", "init"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


def test_db_init_without_operator_role_returns_2(
    make_token: MakeToken, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--token", make_token(roles=["developer"]), "db", "init"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


def test_db_init_with_operator_token_passes_rbac(
    make_token: MakeToken,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("CORP_LLM_PG_DSN", raising=False)
    rc = main(["--token", make_token(roles=["gateway:operator"]), "db", "init"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "gateway:operator" not in err
    assert "CORP_LLM_PG_DSN" in err
