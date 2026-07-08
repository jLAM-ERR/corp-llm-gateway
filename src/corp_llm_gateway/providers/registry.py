from __future__ import annotations

from collections.abc import Callable

from corp_llm_gateway import config
from corp_llm_gateway.extensions import EXTENSION_API_VERSION, Extension
from corp_llm_gateway.providers.base import ProviderSpec, UpstreamProvider
from corp_llm_gateway.providers.corp_vllm import (
    CORP_VLLM_SPEC,
    CorpVllmClientFactory,
    CorpVllmProvider,
)

ProviderFactory = Callable[[], Extension]

# The only names v1 may egress to (Anthropic / OpenAI) plus the local oracle.
# CLAUDE.md forbids a non-OpenAI/Anthropic provider in v1; this set turns that
# rule into an executable guard. Bedrock / Gemini / Azure are explicit v2.
V1_ALLOWED = frozenset({"anthropic", "openai", "corp-vllm"})

_FALSEY = frozenset({"0", "false", "False", "FALSE"})


def _v2_providers_enabled() -> bool:
    return (config.get("CORP_ALLOW_V2_PROVIDERS", "0") or "0") not in _FALSEY


class ProviderRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ProviderSpec] = {}
        self._factories: dict[str, ProviderFactory] = {}

    def register(
        self, spec: ProviderSpec, factory: ProviderFactory, *, replace: bool = False
    ) -> None:
        self._ensure_allowed(spec.name)
        # Duplicate name fails closed: a silent overwrite could shadow the
        # oracle or an upstream on the egress path (safe-extension-registry #1).
        if not replace and spec.name in self._factories:
            raise ValueError(
                f"provider {spec.name!r} already registered; pass replace=True to override"
            )
        self._specs[spec.name] = spec
        self._factories[spec.name] = factory

    def get(self, name: str) -> Extension:
        factory = self._factories.get(name)
        if factory is None:
            raise ValueError(f"unknown provider {name!r}; expected one of {self.known()}")
        return factory()

    def spec(self, name: str) -> ProviderSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise ValueError(f"unknown provider {name!r}; expected one of {self.known()}")
        return spec

    def validate(self, name: str) -> str:
        """Return ``name`` iff it is a registered provider, else raise. The
        audit ``Provider`` type is a registry-validated str; this is the check."""
        self.spec(name)
        return name

    def detect(self, model: str) -> str:
        """Infer the UPSTREAM provider for a model string, then confirm it via
        the registry. The substring rule is byte-for-byte the pre-registry
        ``_detect_provider`` — audit provider attribution stays identical."""
        name = "anthropic" if _looks_anthropic(model) else "openai"
        spec = self.spec(name)
        if spec.role != "upstream":
            raise ValueError(f"provider {name!r} is not an upstream egress target")
        return spec.name

    def known(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def _ensure_allowed(self, name: str) -> None:
        if name in V1_ALLOWED or _v2_providers_enabled():
            return
        raise ValueError(
            f"provider {name!r} is not permitted in v1 (allowed: {sorted(V1_ALLOWED)}); "
            f"set CORP_ALLOW_V2_PROVIDERS=1 to enable v2 providers"
        )


def _looks_anthropic(model: str) -> bool:
    return model.startswith("claude") or "anthropic" in model.lower()


def register_builtins(
    registry: ProviderRegistry,
    *,
    corp_vllm_client_factory: CorpVllmClientFactory | None = None,
) -> None:
    anthropic = ProviderSpec(
        name="anthropic",
        kind="provider",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        role="upstream",
        wire_format="anthropic",
        health_url="https://api.anthropic.com",
    )
    openai = ProviderSpec(
        name="openai",
        kind="provider",
        version="1.0.0",
        api_version=EXTENSION_API_VERSION,
        role="upstream",
        wire_format="openai",
        health_url="https://api.openai.com",
    )
    registry.register(anthropic, lambda: UpstreamProvider(anthropic))
    registry.register(openai, lambda: UpstreamProvider(openai))
    registry.register(CORP_VLLM_SPEC, lambda: CorpVllmProvider(corp_vllm_client_factory))


# Import-side-effect-free singleton: registering the three built-ins allocates
# dicts and dataclasses only — every name is in V1_ALLOWED, so the guard short-
# circuits before reading config, and the factories are not called (no sockets).
REGISTRY = ProviderRegistry()
register_builtins(REGISTRY)


def detect_provider(model: str, registry: ProviderRegistry | None = None) -> str:
    return (registry or REGISTRY).detect(model)


def validate_provider(name: str, registry: ProviderRegistry | None = None) -> str:
    return (registry or REGISTRY).validate(name)
