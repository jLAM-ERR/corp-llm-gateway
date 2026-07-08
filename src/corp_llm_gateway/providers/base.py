from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from corp_llm_gateway.extensions import Extension, ExtensionSpec
from corp_llm_gateway.healthz import HealthStatus

ProviderRole = Literal["upstream", "oracle"]
WireFormat = Literal["openai", "anthropic"]


@dataclass(frozen=True, kw_only=True)
class ProviderSpec(ExtensionSpec):
    # ``capabilities`` (inherited) is descriptive-only in v1 — the negotiation
    # engine that would consume it is explicitly deferred to v2.
    role: ProviderRole
    wire_format: WireFormat
    health_url: str | None = None


class UpstreamProvider(Extension):
    """An egress target (Anthropic / OpenAI). Requests reach it via LiteLLM
    with BYOK Authorization passthrough, so the gateway holds no client here —
    the spec is descriptive metadata."""

    def __init__(self, spec: ProviderSpec) -> None:
        self.spec = spec

    async def health(self) -> HealthStatus:
        # Upstream reachability is deliberately NOT a gateway health signal
        # (mirrors ReadyCheck, which never probes upstreams). A later task may
        # poll ``spec.health_url``; the MVP reports the seam is registered.
        return HealthStatus(True, self.spec.name)
