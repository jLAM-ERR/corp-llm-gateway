from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from corp_llm_gateway import config
from corp_llm_gateway.corp_llm import CorpLlmClient
from corp_llm_gateway.extensions import EXTENSION_API_VERSION, Extension, ExtensionSpec
from corp_llm_gateway.litellm_hook import _detect_provider
from corp_llm_gateway.providers import (
    CORP_VLLM_SPEC,
    REGISTRY,
    V1_ALLOWED,
    CorpVllmProvider,
    ProviderRegistry,
    ProviderSpec,
    UpstreamProvider,
    detect_provider,
    register_builtins,
    validate_provider,
)


@pytest.fixture
def hermetic_v2_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate the v1-guard from both process env and a real config.toml so a
    test decides the flag alone; env still wins over the empty file."""
    monkeypatch.delenv("CORP_ALLOW_V2_PROVIDERS", raising=False)
    empty = tmp_path / "config.toml"
    empty.write_text("")
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(empty))
    config.reset_cache()
    yield
    config.reset_cache()


def _spec(name: str, *, role: str = "upstream", wire: str = "openai") -> ProviderSpec:
    return ProviderSpec(
        name=name,
        kind="provider",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        role=role,  # type: ignore[arg-type]
        wire_format=wire,  # type: ignore[arg-type]
    )


def _fresh() -> ProviderRegistry:
    reg = ProviderRegistry()
    register_builtins(reg)
    return reg


# ProviderSpec shape ---------------------------------------------------------


def test_provider_spec_is_an_extension_spec_with_provider_fields() -> None:
    spec = CORP_VLLM_SPEC
    assert isinstance(spec, ExtensionSpec)
    assert spec.kind == "provider"
    assert spec.role == "oracle"
    assert spec.wire_format == "openai"
    assert spec.health_url is None
    assert spec.capabilities == frozenset()
    assert spec.fail_policy == "fail-closed"


def test_provider_spec_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        CORP_VLLM_SPEC.name = "other"  # type: ignore[misc]


# built-ins: the three v1 providers -----------------------------------------


def test_builtins_register_exactly_the_v1_set() -> None:
    assert set(_fresh().known()) == V1_ALLOWED


def test_upstream_providers_have_upstream_role() -> None:
    reg = _fresh()
    for name in ("anthropic", "openai"):
        assert isinstance(reg.get(name), UpstreamProvider)
        assert reg.spec(name).role == "upstream"


def test_corp_vllm_is_the_oracle() -> None:
    reg = _fresh()
    ext = reg.get("corp-vllm")
    assert isinstance(ext, CorpVllmProvider)
    assert reg.spec("corp-vllm").role == "oracle"


def test_module_singleton_has_the_builtins() -> None:
    assert set(REGISTRY.known()) == V1_ALLOWED


# v1 guard: the load-bearing executable rule --------------------------------


def test_v2_provider_refused_without_flag(hermetic_v2_flag: None) -> None:
    reg = _fresh()
    with pytest.raises(ValueError, match="not permitted in v1") as exc:
        reg.register(_spec("bedrock"), lambda: UpstreamProvider(_spec("bedrock")))
    msg = str(exc.value)
    assert "CORP_ALLOW_V2_PROVIDERS" in msg
    assert "anthropic" in msg and "openai" in msg
    assert "bedrock" not in set(reg.known())


def test_v2_provider_allowed_with_flag(
    hermetic_v2_flag: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_ALLOW_V2_PROVIDERS", "1")
    reg = _fresh()
    bedrock = _spec("bedrock")
    reg.register(bedrock, lambda: UpstreamProvider(bedrock))
    assert "bedrock" in set(reg.known())
    assert isinstance(reg.get("bedrock"), UpstreamProvider)


def test_flag_falsey_values_still_refuse(
    hermetic_v2_flag: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_ALLOW_V2_PROVIDERS", "0")
    reg = _fresh()
    with pytest.raises(ValueError, match="not permitted in v1"):
        reg.register(_spec("gemini"), lambda: UpstreamProvider(_spec("gemini")))


def test_module_singleton_refuses_v2_without_flag(hermetic_v2_flag: None) -> None:
    # A rejected register does not mutate, so the shared singleton is safe here.
    with pytest.raises(ValueError, match="not permitted in v1"):
        REGISTRY.register(_spec("azure"), lambda: UpstreamProvider(_spec("azure")))
    assert set(REGISTRY.known()) == V1_ALLOWED


# lookup + validation fail closed -------------------------------------------


def test_get_unknown_provider_raises_listing_known_set() -> None:
    reg = _fresh()
    with pytest.raises(ValueError, match="unknown provider") as exc:
        reg.get("mistral")
    assert "anthropic" in str(exc.value)


def test_spec_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        _fresh().spec("mistral")


def test_validate_returns_registered_name() -> None:
    assert validate_provider("anthropic", _fresh()) == "anthropic"


def test_validate_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        validate_provider("mistral", _fresh())


def test_duplicate_registration_fails_closed() -> None:
    reg = _fresh()
    with pytest.raises(ValueError, match="already registered"):
        reg.register(_spec("anthropic"), lambda: UpstreamProvider(_spec("anthropic")))


def test_duplicate_registration_allowed_with_replace() -> None:
    reg = _fresh()
    replacement = _spec("anthropic", wire="anthropic")
    reg.register(replacement, lambda: UpstreamProvider(replacement), replace=True)
    assert reg.spec("anthropic") is replacement


# corp-vllm wraps CorpLlmClient lazily --------------------------------------


def test_corp_vllm_client_unwired_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="client factory not wired"):
        CorpVllmProvider().client()


def test_corp_vllm_client_uses_injected_factory() -> None:
    sentinel = CorpLlmClient("https://corp-llm.example", model="GLM-5.1-AWQ")
    provider = CorpVllmProvider(lambda: sentinel)
    assert provider.client() is sentinel


async def test_provider_health_reports_registered() -> None:
    reg = _fresh()
    assert (await reg.get("anthropic").health()).healthy is True
    assert (await reg.get("corp-vllm").health()).healthy is True


def test_provider_get_returns_extension() -> None:
    assert isinstance(_fresh().get("openai"), Extension)


# _detect_provider parity with the old substring rule -----------------------


def _old_rule(model: str) -> str:
    """The pre-registry substring behavior, verbatim from litellm_hook."""
    if model.startswith("claude") or "anthropic" in model.lower():
        return "anthropic"
    return "openai"


REPRESENTATIVE_MODELS = [
    "claude-opus-4-7",
    "claude-3-5-sonnet-20241022",
    "anthropic/claude-x",
    "some-anthropic-proxy",
    "gpt-anthropic-weird",
    "gpt-4o",
    "gpt-4.1-mini",
    "o1-preview",
    "",
    # case-sensitivity quirks of the original rule (startswith is case-sensitive)
    "Claude-Opus",
    "CLAUDE",
]


@pytest.mark.parametrize("model", REPRESENTATIVE_MODELS)
def test_detect_provider_parity_with_old_rule(model: str) -> None:
    assert detect_provider(model) == _old_rule(model)


@pytest.mark.parametrize("model", REPRESENTATIVE_MODELS)
def test_hook_detect_provider_delegates_with_parity(model: str) -> None:
    assert _detect_provider({"model": model}) == _old_rule(model)


def test_hook_detect_provider_handles_missing_model() -> None:
    assert _detect_provider({}) == "openai"


def test_detect_only_yields_registered_upstreams() -> None:
    for model in REPRESENTATIVE_MODELS:
        name = detect_provider(model)
        assert REGISTRY.spec(name).role == "upstream"
