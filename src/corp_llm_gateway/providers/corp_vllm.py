from __future__ import annotations

from collections.abc import Callable

from corp_llm_gateway.corp_llm import CorpLlmClient
from corp_llm_gateway.extensions import EXTENSION_API_VERSION, Extension
from corp_llm_gateway.healthz import HealthStatus
from corp_llm_gateway.providers.base import ProviderSpec

CorpVllmClientFactory = Callable[[], CorpLlmClient]

# vLLM speaks the OpenAI-compatible /v1/chat/completions wire format (openapi.json).
CORP_VLLM_SPEC = ProviderSpec(
    name="corp-vllm",
    kind="provider",
    version="1.0.0",
    api_version=EXTENSION_API_VERSION,
    role="oracle",
    wire_format="openai",
    health_url=None,  # endpoint is config-driven (CORP_LLM_ENDPOINT), not fixed
)


class CorpVllmProvider(Extension):
    """The sanitization oracle, wrapping :class:`CorpLlmClient`. The client is
    built lazily via an injected factory — the registry never opens a socket at
    import or on a health poll."""

    def __init__(self, client_factory: CorpVllmClientFactory | None = None) -> None:
        self.spec = CORP_VLLM_SPEC
        self._client_factory = client_factory

    def client(self) -> CorpLlmClient:
        if self._client_factory is None:
            raise NotImplementedError(
                "corp-vllm oracle client factory not wired; inject one at "
                "registration (bootstrap oracle routing is v2)"
            )
        return self._client_factory()

    async def health(self) -> HealthStatus:
        return HealthStatus(True, "corp-vllm oracle registered")
