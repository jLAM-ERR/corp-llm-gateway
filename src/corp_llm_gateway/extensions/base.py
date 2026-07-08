from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from corp_llm_gateway.healthz import HealthStatus
from corp_llm_gateway.team_config.models import FailPolicy

ExtensionKind = Literal[
    "audit_sink",
    "metrics",
    "tracing",
    "provider",
    "detector",
    "rules",
    "payload_policy",
]


@dataclass(frozen=True)
class ExtensionSpec:
    name: str
    kind: ExtensionKind
    version: str
    api_version: str
    capabilities: frozenset[str] = frozenset()
    # Unspecified failure posture fails closed — a zero-leak gateway never
    # defaults an extension to fail-open (invariant 6, M4 fail-policy matrix).
    fail_policy: FailPolicy = "fail-closed"


class Extension(ABC):
    spec: ExtensionSpec

    @abstractmethod
    async def health(self) -> HealthStatus: ...
