import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from corp_llm_gateway.cli.admin import main
from corp_llm_gateway.extensions import Extension, ExtensionRegistry, ExtensionSpec
from corp_llm_gateway.healthz import HealthStatus
from corp_llm_gateway.team_config import InMemoryTeamConfigStore, TeamConfig
from corp_llm_gateway.tokens import InMemoryTokenStore, TokenInfo


@pytest.fixture(autouse=True)
def _bypass_rbac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "0")


@pytest.fixture
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> ExtensionRegistry:
    """Swap the shared extension REGISTRY for an empty one so `extensions`
    verbs (which populate it on demand) run isolated from other tests."""
    reg = ExtensionRegistry()
    monkeypatch.setattr("corp_llm_gateway.extensions.REGISTRY", reg)
    return reg


class _FakeExt(Extension):
    def __init__(self, spec: ExtensionSpec, *, healthy: bool) -> None:
        self.spec = spec
        self._healthy = healthy

    async def health(self) -> HealthStatus:
        return HealthStatus(self._healthy, "ok" if self._healthy else "boom")


def _register_fake(registry: ExtensionRegistry, *, healthy: bool, fail_policy: str) -> None:
    spec = ExtensionSpec(
        name="fake",
        kind="detector",
        version="1",
        api_version="1",
        fail_policy=fail_policy,  # type: ignore[arg-type]
    )
    registry.register(spec, lambda: _FakeExt(spec, healthy=healthy))


# ---------------------------------------------------------------------------
# team / token — store-backed verbs (in-memory store injected)
# ---------------------------------------------------------------------------


@pytest.fixture
def team_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryTeamConfigStore:
    store = InMemoryTeamConfigStore()
    monkeypatch.setattr("corp_llm_gateway.cli.admin._team_store", lambda: store)
    return store


@pytest.fixture
def token_store(monkeypatch: pytest.MonkeyPatch) -> InMemoryTokenStore:
    store = InMemoryTokenStore()
    monkeypatch.setattr("corp_llm_gateway.cli.admin._token_store", lambda: store)
    return store


def _token_info(corp_token: str, user_id: str = "alice") -> TokenInfo:
    now = datetime.now(UTC)
    return TokenInfo(
        corp_token=corp_token,
        user_id=user_id,
        team_id="t1",
        scopes=("read",),
        issued_at=now,
        expires_at=now + timedelta(days=30),
    )


def test_team_create_persists(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["team", "create", "--team-id", "t1", "--name", "Team One"])
    assert rc == 0
    assert "team created: t1" in capsys.readouterr().out
    assert asyncio.run(team_store.get("t1")).name == "Team One"


def test_team_create_duplicate_errors(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(team_store.upsert(TeamConfig(team_id="t1", name="Existing")))
    rc = main(["team", "create", "--team-id", "t1", "--name", "Dup"])
    assert rc == 2
    assert "already exists" in capsys.readouterr().err


def test_team_set_rules_updates_path(team_store: InMemoryTeamConfigStore) -> None:
    asyncio.run(team_store.upsert(TeamConfig(team_id="t1", name="One")))
    rc = main(["team", "set-rules", "--team-id", "t1", "--from-file", "/etc/rules/t1.md"])
    assert rc == 0
    assert asyncio.run(team_store.get("t1")).replace_md_path == "/etc/rules/t1.md"


def test_team_set_rules_unknown_team(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["team", "set-rules", "--team-id", "ghost", "--from-file", "r.md"])
    assert rc == 2
    assert "unknown team" in capsys.readouterr().err


def test_team_set_retention_persists(team_store: InMemoryTeamConfigStore) -> None:
    asyncio.run(team_store.upsert(TeamConfig(team_id="t1", name="One")))
    rc = main(["team", "set-retention", "--team-id", "t1", "--hot-days", "30", "--cold-years", "1"])
    assert rc == 0
    cfg = asyncio.run(team_store.get("t1"))
    assert cfg.retention_hot_days == 30
    assert cfg.retention_cold_years == 1


def test_team_list_renders(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(team_store.upsert(TeamConfig(team_id="t1", name="One")))
    asyncio.run(team_store.upsert(TeamConfig(team_id="t2", name="Two")))
    rc = main(["team", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "TEAM_ID" in out
    assert "t1" in out and "t2" in out


def test_team_list_json(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(team_store.upsert(TeamConfig(team_id="t1", name="One")))
    rc = main(["team", "list", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["team_id"] == "t1"
    assert data[0]["fail_policy"]["audit_buffer_full"] == "fail-closed"


def test_team_list_empty(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["team", "list"])
    assert rc == 0
    assert "no teams configured" in capsys.readouterr().out


def test_team_show(team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]) -> None:
    asyncio.run(team_store.upsert(TeamConfig(team_id="t1", name="One", replace_md_path="/r.md")))
    rc = main(["team", "show", "--team-id", "t1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "team_id: t1" in out
    assert "/r.md" in out


def test_team_show_unknown(
    team_store: InMemoryTeamConfigStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["team", "show", "--team-id", "ghost"])
    assert rc == 2
    assert "unknown team" in capsys.readouterr().err


def test_token_issue_persists(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["token", "issue", "--user", "alice", "--team", "t1", "--scopes", "read,write"])
    assert rc == 0
    assert "token: ct_" in capsys.readouterr().out
    tokens = asyncio.run(token_store.list_tokens("alice"))
    assert len(tokens) == 1
    assert tokens[0].team_id == "t1"
    assert tokens[0].scopes == ("read", "write")


def test_token_issue_json(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["token", "issue", "--user", "alice", "--team", "t1", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["user_id"] == "alice"
    assert data["corp_token"].startswith("ct_")


def test_token_revoke_actually_revokes(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    token_store.upsert(_token_info("ct-1", user_id="alice"))
    rc = main(["token", "revoke", "--user", "alice"])
    assert rc == 0
    assert "revoked 1 token(s) for user=alice" in capsys.readouterr().out
    got = asyncio.run(token_store.lookup("ct-1"))
    assert got is not None and got.revoked_at is not None


def test_token_list_masks_secret(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    token_store.upsert(_token_info("ct_supersecretvalue", user_id="alice"))
    rc = main(["token", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alice" in out
    assert "ct_supersecretvalue" not in out  # full secret never printed
    assert "secretvalue" not in out


def test_token_list_filters_by_user(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    token_store.upsert(_token_info("ct-a", user_id="alice"))
    token_store.upsert(_token_info("ct-b", user_id="bob"))
    rc = main(["token", "list", "--user", "alice"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alice" in out
    assert "bob" not in out


def test_token_list_empty(
    token_store: InMemoryTokenStore, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["token", "list"])
    assert rc == 0
    assert "no tokens issued" in capsys.readouterr().out


# team / token — RBAC (mutations gated, reads ungated) ----------------------


def test_team_create_rbac_enforced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")  # override autouse bypass
    monkeypatch.delenv("CORP_GATEWAY_ADMIN_TOKEN", raising=False)
    rc = main(["team", "create", "--team-id", "t1", "--name", "X"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


def test_token_revoke_rbac_enforced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")
    monkeypatch.delenv("CORP_GATEWAY_ADMIN_TOKEN", raising=False)
    rc = main(["token", "revoke", "--user", "alice"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


def test_team_list_read_verb_skips_rbac(
    team_store: InMemoryTeamConfigStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")  # enforce; read verb must still run
    assert main(["team", "list"]) == 0


def test_token_list_read_verb_skips_rbac(
    token_store: InMemoryTokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")
    assert main(["token", "list"]) == 0


def test_team_backend_not_configured_errors(
    hermetic_gateway_config: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # No CORP_LLM_PG_DSN and no injected store: fail clearly, never fake success.
    rc = main(["team", "create", "--team-id", "t1", "--name", "X"])
    assert rc == 2
    assert "CORP_LLM_PG_DSN" in capsys.readouterr().err


def test_token_backend_not_configured_errors(
    hermetic_gateway_config: None, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["token", "list"])
    assert rc == 2
    assert "CORP_LLM_PG_DSN" in capsys.readouterr().err


def test_missing_required_arg_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["team", "create", "--team-id", "t1"])
    assert excinfo.value.code != 0


def test_no_command_errors() -> None:
    with pytest.raises(SystemExit):
        main([])


# ---------------------------------------------------------------------------
# extensions — read verbs (no RBAC)
# ---------------------------------------------------------------------------


def test_extensions_list_renders(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "FAIL-POLICY" in out
    for token in ("provider", "anthropic", "openai", "corp-vllm", "audit_sink", "stdout"):
        assert token in out


def test_extensions_list_json(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "list", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    names = {entry["name"] for entry in data}
    assert {"anthropic", "openai", "corp-vllm", "stdout"} <= names
    assert set(data[0]) == {"kind", "name", "version", "api_version", "fail_policy"}


def test_extensions_list_kind_filter(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "list", "--kind", "provider"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "anthropic" in out
    assert "stdout" not in out  # audit_sink filtered out


def test_extensions_list_unknown_kind_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["extensions", "list", "--kind", "bogus"])


def test_extensions_inspect_provider(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "inspect", "provider:anthropic"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "anthropic" in out
    assert "upstream" in out
    assert "wire_format" in out


def test_extensions_inspect_json(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "inspect", "provider:anthropic", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "provider"
    assert data["role"] == "upstream"
    assert data["wire_format"] == "anthropic"
    assert "capabilities" in data


def test_extensions_inspect_audit_sink(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "inspect", "audit_sink:stdout"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stdout" in out
    assert "continue" in out  # audit sink fail_policy


def test_extensions_inspect_bad_ref(
    fresh_registry: ExtensionRegistry,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "inspect", "bogus"])
    assert rc == 2
    assert "KIND:NAME" in capsys.readouterr().err


def test_extensions_inspect_unknown(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "inspect", "provider:nope"])
    assert rc == 2
    assert "Unknown extension" in capsys.readouterr().err


def test_extensions_health_ok(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "health"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "anthropic" in out
    assert "OK" in out


def test_extensions_health_json(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "health", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["healthy"] is True
    assert data["extensions"]


def test_extensions_health_unhealthy_fail_closed_exits_nonzero(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_fake(fresh_registry, healthy=False, fail_policy="fail-closed")
    rc = main(["extensions", "health"])
    assert rc != 0
    assert "UNHEALTHY" in capsys.readouterr().out


def test_extensions_health_unhealthy_continue_stays_zero(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
) -> None:
    _register_fake(fresh_registry, healthy=False, fail_policy="continue")
    rc = main(["extensions", "health"])
    assert rc == 0  # a `continue`-policy ext being down never fails the probe


def test_extensions_read_verb_skips_rbac(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")  # enforce; read verb must still run
    assert main(["extensions", "list"]) == 0


# ---------------------------------------------------------------------------
# extensions — mutating verbs (RBAC-gated + persistence stub)
# ---------------------------------------------------------------------------


def test_extensions_enable_rbac_enforced(
    fresh_registry: ExtensionRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "1")  # override the autouse bypass
    monkeypatch.delenv("CORP_GATEWAY_ADMIN_TOKEN", raising=False)
    rc = main(["extensions", "enable", "audit_sink:stdout"])
    assert rc == 2
    assert "gateway:operator" in capsys.readouterr().err


def test_extensions_enable_stub_raises(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
) -> None:
    with pytest.raises(NotImplementedError, match="extension-state store"):
        main(["extensions", "enable", "audit_sink:stdout"])


def test_extensions_disable_stub_raises(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
) -> None:
    with pytest.raises(NotImplementedError, match="extension-state store"):
        main(["extensions", "disable", "provider:anthropic"])


def test_extensions_enable_unknown_target(
    fresh_registry: ExtensionRegistry,
    hermetic_gateway_config: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["extensions", "enable", "provider:nope"])
    assert rc == 2
    assert "Unknown extension" in capsys.readouterr().err
