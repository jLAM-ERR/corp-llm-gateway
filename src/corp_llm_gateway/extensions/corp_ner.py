"""Corp NER as a detector-kind extension (B4).

The first ``kind="detector"`` extension, and the first extension of any kind with
RUNTIME health: ``health()`` polls the service's ``/ready``, which is exactly what
``Extension.health()`` models. Registering it on the shared ``REGISTRY`` from
``bootstrap.build_guardrail()`` is what makes ``/healthz/extensions`` report it and
``validate_api_version()`` cover it.

``fail_policy="fail-closed"`` is unconditional — corp NER sits on the egress path,
so an unscanned text must never read as clean (invariant 6, M4 fail-policy matrix).

Health carries status codes and exception types only, never request/response text
or transport messages that may name internal hosts (M1-14).
"""

from __future__ import annotations

import httpx

from corp_llm_gateway.extensions import EXTENSION_API_VERSION, Extension, ExtensionSpec
from corp_llm_gateway.extensions.registry import ExtensionRegistry
from corp_llm_gateway.healthz import HealthStatus

EXTENSION_NAME = "corp_ner"
EXTENSION_KIND = "detector"
READY_PATH = "/ready"

_VERSION = "1"


class CorpNerExtension(Extension):
    """Adapt the corp NER service to the extension registry (kind=detector).

    Takes the SAME ``httpx.AsyncClient`` the ``CorpNerClient`` was built with, so
    the readiness poll shares its connection pool and TLS configuration and can
    never disagree with the request path about how it reaches the service.
    """

    def __init__(self, base_url: str, *, http: httpx.AsyncClient) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http
        self.spec = ExtensionSpec(
            name=EXTENSION_NAME,
            kind=EXTENSION_KIND,
            version=_VERSION,
            api_version=EXTENSION_API_VERSION,
            capabilities=frozenset({"detect", "detect_batch"}),
            fail_policy="fail-closed",
        )

    async def health(self) -> HealthStatus:
        try:
            resp = await self._http.get(f"{self._base_url}{READY_PATH}")
        except httpx.HTTPError as exc:
            # httpx timeouts stringify to '' — name the type, never the message.
            return HealthStatus(False, f"corp_ner_error:{type(exc).__name__}")
        if resp.status_code >= 400:
            return HealthStatus(False, f"corp_ner_unhealthy:{resp.status_code}")
        return HealthStatus(True, "corp_ner_ok")


def register_corp_ner(registry: ExtensionRegistry, extension: CorpNerExtension) -> CorpNerExtension:
    """Register a live extension with a cached-instance factory.

    The factory returns that SAME object on every call, so a ``/healthz/extensions``
    poll never churns the shared HTTP client (the instance-lifecycle rule from
    ``audit.factory.register_sink``). ``replace=True`` for the same reason it is
    safe there: ``build_guardrail`` is the trusted composition root re-registering
    its own well-known extension, not an untrusted plugin shadowing one.
    """
    registry.register(extension.spec, lambda: extension, replace=True)
    return extension
