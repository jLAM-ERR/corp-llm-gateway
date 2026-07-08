from __future__ import annotations

from pathlib import Path

import pytest

from corp_llm_gateway import config


@pytest.fixture(autouse=True)
def _reset_config_cache() -> None:
    config.reset_cache()
    yield
    config.reset_cache()


def test_env_wins_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('CORP_GATEWAY_URL = "https://from-file.example"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("CORP_GATEWAY_URL", "https://from-env.example")

    assert config.get("CORP_GATEWAY_URL") == "https://from-env.example"


def test_file_used_when_env_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('CORP_GATEWAY_URL = "https://from-file.example"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("CORP_GATEWAY_URL", raising=False)

    assert config.get("CORP_GATEWAY_URL") == "https://from-file.example"


def test_default_used_when_neither_source_provides_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("# empty\n")
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("CORP_GATEWAY_URL", raising=False)

    assert config.get("CORP_GATEWAY_URL", "fallback") == "fallback"
    assert config.get("CORP_GATEWAY_URL") is None


def test_missing_config_file_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", "/nonexistent/config.toml")
    monkeypatch.delenv("CORP_GATEWAY_URL", raising=False)

    assert config.get("CORP_GATEWAY_URL", "default") == "default"


def test_get_required_raises_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("# empty\n")
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("CORP_LLM_BEARER_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="CORP_LLM_BEARER_TOKEN"):
        config.get_required("CORP_LLM_BEARER_TOKEN")


def test_get_required_uses_file_when_env_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('CORP_LLM_BEARER_TOKEN = "tok-from-file"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("CORP_LLM_BEARER_TOKEN", raising=False)

    assert config.get_required("CORP_LLM_BEARER_TOKEN") == "tok-from-file"


def test_non_string_values_are_stringified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("SOME_PORT = 9999\n")
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("SOME_PORT", raising=False)

    assert config.get("SOME_PORT") == "9999"


def test_corp_llm_verify_ca_bundle_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_CA_BUNDLE", "/etc/certs/bundle.pem")
    monkeypatch.delenv("SSL_VERIFY", raising=False)

    assert config.corp_llm_verify() == "/etc/certs/bundle.pem"


def test_corp_llm_verify_ca_bundle_takes_precedence_over_ssl_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORP_LLM_CA_BUNDLE", "/etc/certs/bundle.pem")
    monkeypatch.setenv("SSL_VERIFY", "false")

    assert config.corp_llm_verify() == "/etc/certs/bundle.pem"


def test_corp_llm_verify_ssl_verify_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORP_LLM_CA_BUNDLE", raising=False)
    monkeypatch.setenv("SSL_VERIFY", "false")

    assert config.corp_llm_verify() is False


def test_corp_llm_verify_defaults_to_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORP_LLM_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_VERIFY", raising=False)

    assert config.corp_llm_verify() is True


def _isolate_from_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", "/nonexistent/config.toml")
    config.reset_cache()


def test_corp_llm_verify_prod_refuses_ssl_verify_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """F9: SSL_VERIFY=false disables TLS on the raw-content corp-LLM call; refuse in prod."""
    _isolate_from_file(monkeypatch)
    monkeypatch.delenv("CORP_LLM_CA_BUNDLE", raising=False)
    monkeypatch.setenv("CORP_ENV", "prod")
    monkeypatch.setenv("SSL_VERIFY", "false")

    with pytest.raises(RuntimeError, match="SSL_VERIFY=false"):
        config.corp_llm_verify()


def test_corp_llm_verify_prod_allows_ca_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """CA bundle keeps verification ON, so prod + SSL_VERIFY=false + bundle is fine."""
    _isolate_from_file(monkeypatch)
    monkeypatch.setenv("CORP_ENV", "prod")
    monkeypatch.setenv("SSL_VERIFY", "false")
    monkeypatch.setenv("CORP_LLM_CA_BUNDLE", "/etc/certs/bundle.pem")

    assert config.corp_llm_verify() == "/etc/certs/bundle.pem"


def test_corp_llm_verify_demo_allows_ssl_verify_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_from_file(monkeypatch)
    monkeypatch.delenv("CORP_LLM_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CORP_ENV", raising=False)
    monkeypatch.setenv("SSL_VERIFY", "false")

    assert config.corp_llm_verify() is False


def test_corp_llm_verify_prod_true_when_verify_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_from_file(monkeypatch)
    monkeypatch.delenv("CORP_LLM_CA_BUNDLE", raising=False)
    monkeypatch.setenv("CORP_ENV", "prod")
    monkeypatch.delenv("SSL_VERIFY", raising=False)

    assert config.corp_llm_verify() is True


@pytest.mark.parametrize("value", ["prod", "production", "PROD", "Production"])
def test_is_prod_true(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_from_file(monkeypatch)
    monkeypatch.setenv("CORP_ENV", value)
    assert config.is_prod() is True


@pytest.mark.parametrize("value", ["", "dev", "demo", "staging"])
def test_is_prod_false(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_from_file(monkeypatch)
    monkeypatch.setenv("CORP_ENV", value)
    assert config.is_prod() is False


def test_require_ner_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default off so the dev / Python-3.14 graceful path stays (F2/A2)."""
    monkeypatch.delenv("CORP_LLM_REQUIRE_NER", raising=False)
    assert config.require_ner() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on"])
def test_require_ner_truthy_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_REQUIRE_NER", value)
    assert config.require_ner() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_require_ner_falsey_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_REQUIRE_NER", value)
    assert config.require_ner() is False


def test_require_ner_reads_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text("CORP_LLM_REQUIRE_NER = true\n")
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("CORP_LLM_REQUIRE_NER", raising=False)
    assert config.require_ner() is True


def test_get_table_reads_nested_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[extensions.audit_sink.langfuse]\nenabled = true\nendpoint = "https://langfuse.corp.lan"\n'
    )
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))

    assert config.get_table("extensions") == {
        "audit_sink": {"langfuse": {"enabled": True, "endpoint": "https://langfuse.corp.lan"}}
    }


def test_get_table_dotted_prefix_descends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[providers.anthropic]\nrole = "upstream"\n[providers.corp-vllm]\nrole = "oracle"\n'
    )
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))

    assert config.get_table("providers.anthropic") == {"role": "upstream"}
    assert config.get_table("providers")["corp-vllm"] == {"role": "oracle"}


def test_get_table_missing_returns_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('CORP_GATEWAY_URL = "https://x.example"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))

    assert config.get_table("extensions") == {}
    assert config.get_table("extensions.audit_sink") == {}


def test_get_table_no_file_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", "/nonexistent/config.toml")

    assert config.get_table("extensions") == {}


def test_get_table_on_scalar_path_returns_empty_dict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('CORP_GATEWAY_URL = "https://x.example"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))

    assert config.get_table("CORP_GATEWAY_URL") == {}


def test_get_table_does_not_break_scalar_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'CORP_GATEWAY_URL = "https://from-file.example"\n'
        "[extensions.audit_sink.langfuse]\n"
        "enabled = true\n"
    )
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("CORP_GATEWAY_URL", "https://from-env.example")

    assert config.get("CORP_GATEWAY_URL") == "https://from-env.example"
    assert config.get_table("extensions")["audit_sink"]["langfuse"]["enabled"] is True


def test_auth_factory_reads_from_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('CORP_LLM_AUTH_PROVIDER = "bearer"\nCORP_LLM_BEARER_TOKEN = "tok-from-file"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.delenv("CORP_LLM_AUTH_PROVIDER", raising=False)
    monkeypatch.delenv("CORP_LLM_BEARER_TOKEN", raising=False)

    from corp_llm_gateway.auth.factory import get_auth_provider
    from corp_llm_gateway.auth.providers import BearerAuthProvider

    provider = get_auth_provider()
    assert isinstance(provider, BearerAuthProvider)
