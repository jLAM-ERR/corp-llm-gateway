import json

import pytest

from corp_llm_gateway.cli.admin import main
from corp_llm_gateway.extensions import Extension, ExtensionRegistry, ExtensionSpec
from corp_llm_gateway.healthz import HealthStatus


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


def test_team_create(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["team", "create", "--team-id", "t1", "--name", "Team One"])
    assert rc == 0
    assert "team.create team_id=t1 name=Team One" in capsys.readouterr().out


def test_team_set_rules(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["team", "set-rules", "--team-id", "t1", "--from-file", "rules.md"])
    assert rc == 0
    assert "team.set_rules team_id=t1 from_file=rules.md" in capsys.readouterr().out


def test_team_set_retention_defaults(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["team", "set-retention", "--team-id", "t1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "team.set_retention team_id=t1 hot_days=90 cold_years=7" in out


def test_team_set_retention_overrides(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["team", "set-retention", "--team-id", "t1", "--hot-days", "30", "--cold-years", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "team.set_retention team_id=t1 hot_days=30 cold_years=1" in out


def test_token_revoke(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["token", "revoke", "--user", "alice"])
    assert rc == 0
    assert "token.revoke user=alice" in capsys.readouterr().out


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
