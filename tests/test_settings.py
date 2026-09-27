from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from corp_llm_gateway import config, settings
from corp_llm_gateway.settings import ConfigError, Settings

_EXAMPLE_TOML = Path(__file__).parents[1] / "config.example.toml"


@pytest.fixture
def hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Clear every settings key + point the loader at an empty TOML, so only a
    test's own explicit env/file values resolve."""
    for name in settings.all_keys():
        monkeypatch.delenv(name, raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    config.reset_cache()
    yield cfg
    config.reset_cache()


def _write(cfg: Path, text: str) -> None:
    cfg.write_text(text)
    config.reset_cache()


# ── registry shape ───────────────────────────────────────────────────────────


def test_all_keys_is_nonempty_and_unique() -> None:
    keys = settings.all_keys()
    assert len(keys) > 30
    assert len(set(keys)) == len(keys)


def test_all_keys_contains_core_and_new_knobs() -> None:
    keys = set(settings.all_keys())
    assert {
        "CORP_LLM_ENDPOINT",
        "CORP_LLM_MODEL",
        "CORP_LLM_RULES_DIR",
        "CORP_LLM_PG_DSN",
        "CORP_GATEWAY_RBAC",
        "CORP_LLM_OVERSIZE_POLICY",
        "CORP_LLM_REQUIRE_NER",
        "CORP_LLM_TESTDATA_ALLOWLIST",
        "CORP_LLM_TESTDATA_ALLOWLIST_FILE",
        "CORP_LLM_ORACLE_ENABLED",
        "CORP_LLM_FORWARD_ANTHROPIC_AUTH",
    } <= keys


def test_secret_flag_marks_credentials() -> None:
    assert settings.is_secret("CORP_LLM_BEARER_TOKEN")
    assert settings.is_secret("CORP_LLM_PG_DSN")
    assert not settings.is_secret("CORP_LLM_ENDPOINT")


# ── validate(): required ─────────────────────────────────────────────────────


def test_validate_ok_when_endpoint_set(hermetic: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.corp.lan/v1")
    result = config.validate()
    assert isinstance(result, Settings)
    assert result["CORP_LLM_ENDPOINT"] == "https://corp-llm.corp.lan/v1"


def test_validate_hard_fails_on_missing_endpoint(hermetic: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        config.validate()
    assert "CORP_LLM_ENDPOINT" in str(exc.value)
    assert any("CORP_LLM_ENDPOINT" in p for p in exc.value.problems)


def test_validate_reports_every_problem_at_once(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # endpoint missing AND oversize malformed — both surface, not just the first.
    monkeypatch.setenv("CORP_LLM_OVERSIZE_POLICY", "banana")
    with pytest.raises(ConfigError) as exc:
        config.validate()
    joined = "\n".join(exc.value.problems)
    assert "CORP_LLM_ENDPOINT" in joined
    assert "CORP_LLM_OVERSIZE_POLICY" in joined


# ── validate(): corp NER (B4) ───────────────────────────────────────────────


def test_corp_ner_keys_are_registered() -> None:
    assert {
        "CORP_NER_ENABLED",
        "CORP_NER_ENDPOINT",
        "CORP_NER_TIMEOUT_S",
        "CORP_NER_MAX_TEXTS",
        "CORP_NER_MAX_INPUT_CHARS",
        "CORP_NER_CA_BUNDLE",
    } <= set(settings.all_keys())
    # No REQUIRE flag: corp NER is fail-closed unconditionally.
    assert not any(k.startswith("CORP_NER_REQUIRE") for k in settings.all_keys())


def test_corp_ner_defaults_leave_existing_deploys_untouched(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    result = config.validate()
    assert result.flag("CORP_NER_ENABLED") is False
    assert result["CORP_NER_TIMEOUT_S"] == "30"  # deliberately under the service's 60s
    assert result["CORP_NER_MAX_TEXTS"] == "256"
    assert result["CORP_NER_MAX_INPUT_CHARS"] == "200000"
    assert not result["CORP_NER_ENDPOINT"]


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
def test_validate_requires_corp_ner_endpoint_when_enabled(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    # Same reason as the oracle endpoint: required_when is exact-match string
    # equality and would miss these lenient truthy spellings.
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_NER_ENABLED", truthy)
    with pytest.raises(ConfigError) as exc:
        config.validate()
    assert any("CORP_NER_ENDPOINT" in p for p in exc.value.problems)

    monkeypatch.setenv("CORP_NER_ENDPOINT", "https://corp-ner.corp.lan")
    assert isinstance(config.validate(), Settings)


def test_validate_ignores_corp_ner_endpoint_when_disabled(hermetic: Path) -> None:
    _write(hermetic, 'CORP_LLM_ENDPOINT = "https://x/v1"\nCORP_NER_ENABLED = false\n')
    assert isinstance(config.validate(), Settings)


# ── validate(): oracle enabled switch (local mode) ──────────────────────────


def test_validate_ok_with_oracle_disabled_and_no_endpoint(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", "0")
    result = config.validate()
    assert isinstance(result, Settings)
    assert result.flag("CORP_LLM_ORACLE_ENABLED") is False
    assert not result["CORP_LLM_ENDPOINT"]


def test_validate_fails_when_oracle_and_local_first_both_disabled(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", "0")
    monkeypatch.setenv("CORP_LLM_LOCAL_FIRST", "0")
    with pytest.raises(ConfigError) as exc:
        config.validate()
    joined = "\n".join(exc.value.problems)
    assert "CORP_LLM_ORACLE_ENABLED" in joined
    assert "CORP_LLM_LOCAL_FIRST" in joined
    # the no-op-sanitizer failure must not also demand an endpoint.
    assert "CORP_LLM_ENDPOINT" not in joined


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
def test_validate_still_requires_endpoint_when_oracle_enabled(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    # regression: required_when is exact-match and would silently miss these
    # lenient truthy spellings — the dedicated check uses _as_flag instead.
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", truthy)
    with pytest.raises(ConfigError) as exc:
        config.validate()
    assert any("CORP_LLM_ENDPOINT" in p for p in exc.value.problems)

    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    assert isinstance(config.validate(), Settings)


@pytest.mark.parametrize("falsy", ["0", "false", "off"])
def test_validate_lenient_falsy_forms_disable_oracle(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", falsy)
    result = config.validate()
    assert result.flag("CORP_LLM_ORACLE_ENABLED") is False


# ── validate(): forward-auth bridges are mutually exclusive ──────────────────


def test_forward_anthropic_auth_defaults_off(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    result = config.validate()
    assert result.flag("CORP_LLM_FORWARD_ANTHROPIC_AUTH") is False
    assert result.flag("CORP_LLM_FORWARD_CHATGPT_AUTH") is False


@pytest.mark.parametrize(
    "chatgpt,anthropic",
    [("1", "0"), ("0", "1"), ("0", "0"), ("1", ""), ("", "on")],
)
def test_validate_ok_unless_both_forward_auth_flags_are_on(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, chatgpt: str, anthropic: str
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", chatgpt)
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", anthropic)
    assert isinstance(config.validate(), Settings)


def test_validate_rejects_both_forward_auth_flags_with_the_shared_message(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", "1")
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "1")
    with pytest.raises(ConfigError) as exc:
        config.validate()
    assert settings.FORWARD_AUTH_EXCLUSIVE_MESSAGE in exc.value.problems
    # the message must say WHY it is a v1 limitation, not just that it is one.
    assert "v2" in settings.FORWARD_AUTH_EXCLUSIVE_MESSAGE


@pytest.mark.parametrize("truthy", ["true", "yes", "on", "ON"])
def test_validate_rejects_lenient_truthy_spellings_of_both_flags(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    # regression: a raw `== "1"` comparison would let these two bridges both boot.
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", truthy)
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", truthy)
    with pytest.raises(ConfigError, match="mutually"):
        config.validate()


def test_forward_auth_conflict_is_the_single_shared_rule() -> None:
    assert settings.forward_auth_conflict(chatgpt=True, anthropic=True) == (
        settings.FORWARD_AUTH_EXCLUSIVE_MESSAGE
    )
    assert settings.forward_auth_conflict(chatgpt=True, anthropic=False) is None
    assert settings.forward_auth_conflict(chatgpt=False, anthropic=True) is None
    assert settings.forward_auth_conflict(chatgpt=False, anthropic=False) is None


# ── validate(): a litellm master key cancels either bridge ───────────────────


def test_validate_rejects_a_master_key_next_to_the_anthropic_bridge(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-key-fixture")

    with pytest.raises(ConfigError) as exc:
        config.validate()

    assert settings.MASTER_KEY_VS_FORWARD_AUTH_MESSAGE in exc.value.problems
    # the message must name the symptom (a 401 before pre_call), not just the rule.
    assert "401" in settings.MASTER_KEY_VS_FORWARD_AUTH_MESSAGE
    # and must never echo the key it rejects.
    assert "master-key-fixture" not in str(exc.value)


def test_validate_rejects_a_master_key_next_to_the_chatgpt_bridge(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", "on")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-key-fixture")

    with pytest.raises(ConfigError, match="LITELLM_MASTER_KEY"):
        config.validate()


@pytest.mark.parametrize("master_key", ["", "   "])
def test_validate_rejects_a_blank_master_key_next_to_a_bridge(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, master_key: str
) -> None:
    # litellm does NOT ignore a blank master key: `get_secret_str` returns '' /
    # '   ' verbatim and proxy auth is skipped only for `master_key is None`, so
    # `LITELLM_MASTER_KEY=` in a .env still 401s the developer's bearer before
    # pre_call. Presence is the rule, emptiness buys nothing.
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", master_key)

    with pytest.raises(ConfigError) as exc:
        config.validate()

    assert settings.MASTER_KEY_VS_FORWARD_AUTH_MESSAGE in exc.value.problems


def test_validate_accepts_an_absent_master_key_next_to_a_bridge(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "1")

    assert isinstance(config.validate(), Settings)


@pytest.mark.parametrize("master_key", ["", "   "])
def test_validate_allows_a_blank_master_key_when_no_bridge_is_on(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, master_key: str
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", master_key)

    assert isinstance(config.validate(), Settings)


def test_validate_allows_a_master_key_when_no_bridge_is_on(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plain litellm deploy with virtual keys and no subscription bridge is fine.
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-key-fixture")

    assert isinstance(config.validate(), Settings)


def test_master_key_is_registered_as_a_secret_so_config_check_redacts_it() -> None:
    assert "LITELLM_MASTER_KEY" in settings.all_keys()
    assert settings.is_secret("LITELLM_MASTER_KEY")


def test_master_key_conflict_is_the_single_shared_rule() -> None:
    message = settings.MASTER_KEY_VS_FORWARD_AUTH_MESSAGE
    assert settings.master_key_conflict(master_key="k", chatgpt=True, anthropic=False) == message
    assert settings.master_key_conflict(master_key="k", chatgpt=False, anthropic=True) == message
    assert settings.master_key_conflict(master_key="k", chatgpt=False, anthropic=False) is None
    assert settings.master_key_conflict(master_key=None, chatgpt=False, anthropic=True) is None
    # Set-but-blank is set: litellm enables proxy auth for any non-None value.
    assert settings.master_key_conflict(master_key="", chatgpt=False, anthropic=True) == message
    assert settings.master_key_conflict(master_key="   ", chatgpt=False, anthropic=True) == message


# ── validate(): malformed choices ────────────────────────────────────────────


def test_validate_rejects_unknown_oversize_policy(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_OVERSIZE_POLICY", "nope")
    with pytest.raises(ConfigError, match="CORP_LLM_OVERSIZE_POLICY"):
        config.validate()


@pytest.mark.parametrize(
    "raw",
    ["/internal/ops", "POST", "TRACE /internal/ops", "GET internal/ops", "GET /internal/../key"],
)
def test_validate_rejects_a_malformed_route_gate_extra(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH", raw)
    with pytest.raises(ConfigError, match="CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH"):
        config.validate()


def test_validate_accepts_route_gate_extras_and_resolves_them(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from corp_llm_gateway.route_gate import Verdict

    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv(
        "CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH", "GET /internal/ops-status, POST /internal/ops"
    )
    assert isinstance(config.validate(), Settings)
    extras = config.route_gate_extras()
    assert set(extras) == {("GET", "/internal/ops-status"), ("POST", "/internal/ops")}
    assert all(entry.verdict is Verdict.PASSTHROUGH for entry in extras.values())


def test_route_gate_extras_default_to_empty(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    assert config.route_gate_extras() == {}


def test_validate_rejects_unknown_auth_provider(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "kerberos")
    with pytest.raises(ConfigError, match="CORP_LLM_AUTH_PROVIDER"):
        config.validate()


def test_validate_rejects_unknown_audit_sink(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_AUDIT_SINK", "kafka")
    with pytest.raises(ConfigError, match="CORP_AUDIT_SINK"):
        config.validate()


def test_validate_accepts_valid_choices(hermetic: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_OVERSIZE_POLICY", "chunk")
    monkeypatch.setenv("CORP_AUDIT_SINK", "stdout")
    assert isinstance(config.validate(), Settings)


# ── validate(): conditional credentials ──────────────────────────────────────


def test_bearer_provider_requires_token(hermetic: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "bearer")
    with pytest.raises(ConfigError, match="CORP_LLM_BEARER_TOKEN"):
        config.validate()

    monkeypatch.setenv("CORP_LLM_BEARER_TOKEN", "ct_secret")
    assert isinstance(config.validate(), Settings)


def test_langfuse_sink_requires_keys(hermetic: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_AUDIT_SINK", "langfuse")
    with pytest.raises(ConfigError) as exc:
        config.validate()
    joined = "\n".join(exc.value.problems)
    assert "CORP_LANGFUSE_URL" in joined
    assert "CORP_LANGFUSE_PUBLIC_KEY" in joined
    assert "CORP_LANGFUSE_SECRET_KEY" in joined

    monkeypatch.setenv("CORP_LANGFUSE_URL", "http://lf")
    monkeypatch.setenv("CORP_LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("CORP_LANGFUSE_SECRET_KEY", "sk")
    assert isinstance(config.validate(), Settings)


def test_noop_provider_needs_no_credentials(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    # default provider is noop; no bearer/mtls/oidc keys required.
    assert isinstance(config.validate(), Settings)


# ── validate(): resolution chain (NOT native pydantic env sourcing) ──────────


def test_validate_resolves_endpoint_from_file_with_env_cleared(hermetic: Path) -> None:
    # Proves values flow through config.get (which reads the TOML), not pydantic's
    # native env/dotenv sourcing (which would ignore this file).
    _write(hermetic, 'CORP_LLM_ENDPOINT = "https://from-file/v1"\n')
    result = config.validate()
    assert result["CORP_LLM_ENDPOINT"] == "https://from-file/v1"


def test_env_overrides_file_through_the_chain(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, 'CORP_LLM_ENDPOINT = "https://from-file/v1"\nCORP_AUDIT_SINK = "stdout"\n')
    monkeypatch.setenv("CORP_AUDIT_SINK", "kafka")  # env (invalid) must win over file
    with pytest.raises(ConfigError, match="CORP_AUDIT_SINK"):
        config.validate()


# ── example.toml completeness ────────────────────────────────────────────────


def test_example_toml_documents_every_key() -> None:
    text = _EXAMPLE_TOML.read_text()
    missing = [
        key
        for key in settings.all_keys()
        if not re.search(rf"(?m)^#?\s*{re.escape(key)}\s*=", text)
    ]
    assert not missing, f"config.example.toml is missing keys: {missing}"


# ── existing accessors still resolve through the chain ───────────────────────


def test_existing_accessors_unchanged(hermetic: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(
        hermetic,
        'CORP_GATEWAY_URL = "https://from-file"\n'
        'CORP_LLM_BEARER_TOKEN = "tok-file"\n'
        "CORP_LLM_REQUIRE_NER = true\n"
        "[extensions.audit_sink.langfuse]\nenabled = true\n",
    )
    # get: env wins over file
    monkeypatch.setenv("CORP_GATEWAY_URL", "https://from-env")
    assert config.get("CORP_GATEWAY_URL") == "https://from-env"
    # get_required: file fallback
    assert config.get_required("CORP_LLM_BEARER_TOKEN") == "tok-file"
    # get_table: nested table
    assert config.get_table("extensions")["audit_sink"]["langfuse"]["enabled"] is True
    # require_ner: file truthy
    assert config.require_ner() is True
    # oversize_policy: default
    assert config.oversize_policy() == "fail-closed"
    # corp_llm_verify: default true
    assert config.corp_llm_verify() is True


def test_settings_flag_helper(hermetic: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_REQUIRE_NER", "1")
    monkeypatch.setenv("CORP_LLM_GAZETTEER", "0")
    result = config.validate()
    assert result.flag("CORP_LLM_REQUIRE_NER") is True
    assert result.flag("CORP_LLM_GAZETTEER") is False


# ── pydantic path (skips on the 3.14 graceful-degradation venv) ──────────────


def test_validate_uses_pydantic_when_present(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pydantic")
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    assert isinstance(config.validate(), Settings)

    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "bogus")
    with pytest.raises(ConfigError, match="CORP_LLM_AUTH_PROVIDER"):
        config.validate()


# ── developer token issuance (settings.issuance()) ───────────────────────────

_ISSUER = "https://keycloak.corp.lan/realms/dev"
_TEAM_MAP_TOML = (
    "[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]\n"
    '"/devs/payments" = "payments"\n'
    '"/devs/core" = "core"\n'
    '"/devs/aaa" = "aaa"\n'
)


def _issuance_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_AUDIENCE", "corp-gateway-issuance")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID", "corp-gateway-cli")


def test_issuance_keys_are_registered() -> None:
    assert {
        "CORP_GATEWAY_ISSUE_OIDC_ISSUER",
        "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE",
        "CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID",
        "CORP_GATEWAY_ISSUE_OIDC_JWKS_URL",
        "CORP_GATEWAY_ISSUE_OIDC_TEAM_CLAIM",
        "CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP",
        "CORP_GATEWAY_ISSUE_OIDC_USER_CLAIM",
        "CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS",
        "CORP_GATEWAY_ISSUE_MAX_ACTIVE",
        "CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS",
        "CORP_GATEWAY_ISSUE_MAX_INFLIGHT",
        "CORP_GATEWAY_ISSUE_RATE_PER_MINUTE",
    } <= set(settings.all_keys())


def test_issuance_is_disabled_when_issuer_unset(hermetic: Path) -> None:
    assert settings.issuance() is None


def test_issuance_is_disabled_when_issuer_blank(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", "  ")
    assert settings.issuance() is None


def test_issuance_resolves_with_defaults(hermetic: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_GATEWAY_OIDC_AUDIENCE", "corp-llm-gateway")
    result = settings.issuance()
    assert result is not None
    assert result.issuer == _ISSUER
    assert result.audience == "corp-gateway-issuance"
    assert result.client_id == "corp-gateway-cli"
    assert result.jwks_url == f"{_ISSUER}/protocol/openid-connect/certs"
    assert result.team_claim == "groups"
    assert result.user_claim == "preferred_username"
    assert result.operator_audience == "corp-llm-gateway"
    assert result.ca_bundle is None
    assert result.token_ttl_days == 30
    assert result.max_active == 2
    assert result.min_interval_seconds == 600
    assert result.max_inflight == 4
    assert result.rate_per_minute == 30


def test_issuance_team_map_keeps_config_file_order(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    result = settings.issuance()
    assert result is not None
    assert result.team_map == (
        ("/devs/payments", "payments"),
        ("/devs/core", "core"),
        ("/devs/aaa", "aaa"),
    )


def test_issuance_accepts_an_inline_team_map_table(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, 'CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP = { "devs" = "t1" }\n')
    _issuance_env(monkeypatch)
    result = settings.issuance()
    assert result is not None
    assert result.team_map == (("devs", "t1"),)


def test_issuance_strips_a_trailing_slash_from_the_issuer_and_default_jwks_url(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", _ISSUER + "/")
    result = settings.issuance()
    assert result is not None
    assert result.issuer == _ISSUER
    assert result.jwks_url == f"{_ISSUER}/protocol/openid-connect/certs"


def test_issuance_explicit_overrides(hermetic: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_JWKS_URL", "https://jwks.corp.lan/certs")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_TEAM_CLAIM", "teams")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_USER_CLAIM", "email")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS", "7")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_MAX_ACTIVE", "1")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_MAX_INFLIGHT", "8")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_RATE_PER_MINUTE", "120")
    monkeypatch.setenv("CORP_LLM_CA_BUNDLE", "/etc/ssl/corp-root.pem")
    result = settings.issuance()
    assert result is not None
    assert result.jwks_url == "https://jwks.corp.lan/certs"
    assert result.team_claim == "teams"
    assert result.user_claim == "email"
    assert (
        result.token_ttl_days,
        result.max_active,
        result.min_interval_seconds,
        result.max_inflight,
        result.rate_per_minute,
    ) == (7, 1, 60, 8, 120)
    assert result.ca_bundle == "/etc/ssl/corp-root.pem"


def test_issuance_settings_are_frozen(hermetic: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    result = settings.issuance()
    assert result is not None
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.max_active = 99  # type: ignore[misc]


def test_issuance_refuses_a_partial_config(hermetic: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", _ISSUER)
    with pytest.raises(ConfigError) as exc:
        settings.issuance()
    joined = "\n".join(exc.value.problems)
    assert "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE" in joined
    assert "CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID" in joined
    assert "CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP" in joined


@pytest.mark.parametrize(
    "missing",
    [
        "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE",
        "CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID",
    ],
)
def test_issuance_refuses_each_missing_required_key(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(ConfigError) as exc:
        settings.issuance()
    assert [p for p in exc.value.problems if missing in p]


def test_issuance_refuses_a_missing_team_map(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _issuance_env(monkeypatch)
    with pytest.raises(ConfigError, match="CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP"):
        settings.issuance()


def test_issuance_refuses_a_team_map_given_as_a_scalar(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, 'CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP = "devs=t1"\n')
    _issuance_env(monkeypatch)
    with pytest.raises(ConfigError, match="CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP"):
        settings.issuance()


def test_issuance_refuses_an_empty_team_map(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, "[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]\n")
    _issuance_env(monkeypatch)
    with pytest.raises(ConfigError, match="CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP"):
        settings.issuance()


@pytest.mark.parametrize("value", ["3", '""', '"  "'])
def test_issuance_refuses_a_team_map_entry_without_a_team_string(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _write(hermetic, f'[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]\n"devs" = {value}\n')
    _issuance_env(monkeypatch)
    with pytest.raises(ConfigError, match="CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP"):
        settings.issuance()


def test_issuance_refuses_the_operator_audience(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_GATEWAY_OIDC_AUDIENCE", "corp-gateway-issuance")
    with pytest.raises(ConfigError) as exc:
        settings.issuance()
    joined = "\n".join(exc.value.problems)
    assert "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE" in joined
    assert "CORP_GATEWAY_OIDC_AUDIENCE" in joined


@pytest.mark.parametrize("env", ["prod", "production", " PROD "])
def test_issuance_refuses_a_non_https_issuer_in_prod(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_ENV", env)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", "http://keycloak.corp.lan/realms/dev")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_JWKS_URL", "https://jwks.corp.lan/certs")
    with pytest.raises(ConfigError, match="CORP_GATEWAY_ISSUE_OIDC_ISSUER"):
        settings.issuance()


def test_issuance_refuses_a_non_https_jwks_url_in_prod(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_ENV", "prod")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_JWKS_URL", "http://jwks.corp.lan/certs")
    with pytest.raises(ConfigError, match="CORP_GATEWAY_ISSUE_OIDC_JWKS_URL"):
        settings.issuance()


def test_issuance_derived_jwks_url_inherits_an_http_issuer_refusal_in_prod(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_ENV", "prod")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", "http://keycloak.corp.lan/realms/dev")
    with pytest.raises(ConfigError) as exc:
        settings.issuance()
    joined = "\n".join(exc.value.problems)
    assert "CORP_GATEWAY_ISSUE_OIDC_ISSUER" in joined
    assert "CORP_GATEWAY_ISSUE_OIDC_JWKS_URL" in joined


def test_issuance_allows_http_outside_prod(hermetic: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_ENV", "dev")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", "http://keycloak:8080/realms/dev")
    result = settings.issuance()
    assert result is not None
    assert result.jwks_url == "http://keycloak:8080/realms/dev/protocol/openid-connect/certs"
    assert result.allow_insecure_http is True


@pytest.mark.parametrize(("env", "allowed"), [("", True), ("dev", True), ("production", False)])
def test_issuance_allows_insecure_http_exactly_outside_prod(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, env: str, allowed: bool
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_ENV", env)
    result = settings.issuance()
    assert result is not None
    assert result.allow_insecure_http is allowed


@pytest.mark.parametrize("url", ["ftp://jwks.corp.lan/certs", "not-a-url", "https://"])
def test_issuance_refuses_a_malformed_jwks_url_everywhere(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_JWKS_URL", url)
    with pytest.raises(ConfigError, match="CORP_GATEWAY_ISSUE_OIDC_JWKS_URL"):
        settings.issuance()


@pytest.mark.parametrize(
    "key",
    [
        "CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS",
        "CORP_GATEWAY_ISSUE_MAX_ACTIVE",
        "CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS",
        "CORP_GATEWAY_ISSUE_MAX_INFLIGHT",
        "CORP_GATEWAY_ISSUE_RATE_PER_MINUTE",
    ],
)
@pytest.mark.parametrize("value", ["0", "-1", "abc", "1.5"])
def test_issuance_refuses_non_positive_bounds(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigError, match=key):
        settings.issuance()


def test_issuance_bounds_resolve_from_the_config_file(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, "CORP_GATEWAY_ISSUE_MAX_ACTIVE = 5\n" + _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    result = settings.issuance()
    assert result is not None
    assert result.max_active == 5


def test_validate_reports_issuance_problems(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", _ISSUER)
    with pytest.raises(ConfigError) as exc:
        config.validate()
    joined = "\n".join(exc.value.problems)
    assert "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE" in joined
    assert "CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP" in joined


def test_validate_accepts_a_complete_issuance_config(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://gw:gw@pg:5432/gw")
    _issuance_env(monkeypatch)
    assert isinstance(config.validate(), Settings)


def test_validate_refuses_issuance_without_postgres(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `config check` and the entrypoint's boot check share this resolver.
    _write(hermetic, _TEAM_MAP_TOML)
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    _issuance_env(monkeypatch)
    with pytest.raises(ConfigError, match="CORP_LLM_PG_DSN"):
        config.validate()


def test_serving_issuance_requires_postgres(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    with pytest.raises(ConfigError) as exc:
        settings.serving_issuance()
    assert [p for p in exc.value.problems if p.startswith("CORP_LLM_PG_DSN")]


def test_serving_issuance_resolves_with_postgres(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://gw:gw@pg:5432/gw")
    assert settings.serving_issuance() == settings.issuance()
    assert settings.serving_issuance() is not None


def test_serving_issuance_is_none_when_disabled(hermetic: Path) -> None:
    assert settings.serving_issuance() is None


def test_serving_issuance_reports_a_partial_config_and_the_missing_dsn_together(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", _ISSUER)
    with pytest.raises(ConfigError) as exc:
        settings.serving_issuance()
    joined = "\n".join(exc.value.problems)
    assert "CORP_GATEWAY_ISSUE_OIDC_AUDIENCE" in joined
    assert "CORP_LLM_PG_DSN" in joined


def test_validate_ignores_issuance_keys_when_issuer_unset(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_MAX_ACTIVE", "0")
    assert isinstance(config.validate(), Settings)


@pytest.mark.parametrize("operator", [" corp-gateway-issuance", "corp-gateway-issuance  "])
def test_issuance_refuses_an_operator_audience_that_differs_only_by_whitespace(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, operator: str
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_GATEWAY_OIDC_AUDIENCE", operator)
    with pytest.raises(ConfigError, match="must differ"):
        settings.issuance()


async def test_issuance_bounds_of_one_resolve_and_are_usable(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from corp_llm_gateway.tokens import InMemoryTokenStore, IssuancePolicy, OidcClaims

    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    for key in (
        "CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS",
        "CORP_GATEWAY_ISSUE_MAX_ACTIVE",
        "CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS",
        "CORP_GATEWAY_ISSUE_MAX_INFLIGHT",
        "CORP_GATEWAY_ISSUE_RATE_PER_MINUTE",
    ):
        monkeypatch.setenv(key, " 1 ")
    resolved = settings.issuance()
    assert resolved is not None
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    clock = [t0]
    store = InMemoryTokenStore()
    policy = IssuancePolicy(store, resolved, clock=lambda: clock[0])

    first = await policy.issue(OidcClaims("u", "t1", issuer="i", subject="s", jti="j1"))
    clock[0] = t0 + timedelta(seconds=1)
    second = await policy.issue(OidcClaims("u", "t1", issuer="i", subject="s", jti="j2"))

    assert second.expires_at == t0 + timedelta(days=1, seconds=1)
    old = await store.lookup(first.corp_token)
    assert old is not None and old.revoked_at is not None


# ── issuance: upper bounds, the store bound, runtime needs ───────────────────


def test_the_store_timeout_key_is_registered_with_its_default(hermetic: Path) -> None:
    assert "CORP_GATEWAY_ISSUE_STORE_TIMEOUT_SECONDS" in settings.all_keys()


def test_issuance_store_timeout_defaults_to_ten_seconds(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    result = settings.issuance()
    assert result is not None
    assert result.store_timeout_seconds == 10


@pytest.mark.parametrize(
    ("key", "highest"),
    [
        ("CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS", 3650),
        ("CORP_GATEWAY_ISSUE_MAX_ACTIVE", 100),
        ("CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS", 30 * 86400),
        ("CORP_GATEWAY_ISSUE_MAX_INFLIGHT", 1000),
        ("CORP_GATEWAY_ISSUE_RATE_PER_MINUTE", 100_000),
        ("CORP_GATEWAY_ISSUE_STORE_TIMEOUT_SECONDS", 300),
    ],
)
def test_issuance_bounds_accept_their_ceiling_and_refuse_one_past_it(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, key: str, highest: int
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv(key, str(highest))
    assert settings.issuance() is not None

    monkeypatch.setenv(key, str(highest + 1))
    with pytest.raises(ConfigError) as exc:
        settings.issuance()
    assert [p for p in exc.value.problems if p.startswith(key) and str(highest) in p]


@pytest.mark.parametrize(("value", "ok"), [("4", False), ("5", True), ("0", False)])
def test_the_store_timeout_is_never_below_the_stores_lock_wait(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch, value: str, ok: bool
) -> None:
    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_STORE_TIMEOUT_SECONDS", value)
    if ok:
        assert settings.issuance() is not None
        return
    with pytest.raises(ConfigError, match="CORP_GATEWAY_ISSUE_STORE_TIMEOUT_SECONDS"):
        settings.issuance()


def test_validate_refuses_an_issuance_bound_past_its_ceiling(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `config check` runs validate(): it must refuse what the boot refuses.
    _write(hermetic, _TEAM_MAP_TOML)
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://x/v1")
    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://gw:gw@pg:5432/gw")
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS", "3000000")
    with pytest.raises(ConfigError, match="CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS"):
        config.validate()


@pytest.fixture
def serving(hermetic: Path, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Issuance fully configured, with both extras present as far as imports go."""
    import sys
    import types

    _write(hermetic, _TEAM_MAP_TOML)
    _issuance_env(monkeypatch)
    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://gw:gw@pg:5432/gw")
    for name in ("cryptography", "jwt", "asyncpg"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    return monkeypatch


def test_runtime_problems_are_empty_when_issuance_is_off(hermetic: Path) -> None:
    assert settings.issuance_runtime_problems() == []


def test_runtime_problems_are_empty_when_the_config_is_refused(
    hermetic: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # validate() and the boot report the config itself; this adds nothing twice.
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", _ISSUER)
    assert settings.issuance_runtime_problems() == []


def test_runtime_problems_are_empty_when_everything_is_there(
    serving: pytest.MonkeyPatch,
) -> None:
    assert settings.issuance_runtime_problems() == []


@pytest.mark.parametrize(
    ("module", "problem"),
    [
        ("cryptography", settings.ISSUANCE_NEEDS_OIDC_EXTRA),
        ("jwt", settings.ISSUANCE_NEEDS_OIDC_EXTRA),
        ("asyncpg", settings.ISSUANCE_NEEDS_POSTGRES_EXTRA),
    ],
)
def test_runtime_problems_name_the_missing_extra(
    serving: pytest.MonkeyPatch, module: str, problem: str
) -> None:
    import sys

    serving.setitem(sys.modules, module, None)

    assert settings.issuance_runtime_problems() == [problem]


@pytest.mark.parametrize("kind", ["missing", "directory", "not-pem"])
def test_runtime_problems_refuse_a_ca_bundle_that_cannot_be_used(
    serving: pytest.MonkeyPatch, tmp_path: Path, kind: str
) -> None:
    bundle = tmp_path / "bundle-path-91c4"
    if kind == "directory":
        bundle.mkdir()
    elif kind == "not-pem":
        bundle.write_text("not a certificate\n")
    serving.setenv("CORP_LLM_CA_BUNDLE", str(bundle))

    problems = settings.issuance_runtime_problems()

    expected = (
        settings.ISSUANCE_CA_BUNDLE_INVALID
        if kind == "not-pem"
        else settings.ISSUANCE_CA_BUNDLE_UNREADABLE
    )
    assert problems == [expected]
    assert "bundle-path-91c4" not in problems[0]


def test_runtime_problems_accept_a_loadable_ca_bundle(serving: pytest.MonkeyPatch) -> None:
    import certifi  # httpx's own dependency

    serving.setenv("CORP_LLM_CA_BUNDLE", certifi.where())

    assert settings.issuance_runtime_problems() == []
