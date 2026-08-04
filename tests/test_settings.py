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
