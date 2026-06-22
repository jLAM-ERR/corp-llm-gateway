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
