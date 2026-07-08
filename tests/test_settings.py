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
