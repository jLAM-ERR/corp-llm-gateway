"""Metrics exporter seam (ADR-001 interface-registry).

The gateway records three things at the hook boundary: a block counter, a
component-failure counter, and a request-latency histogram. The concrete
exporter is pluggable; the default (``NoopExporter``) records nothing so the
metrics surface is opt-in (zero behavior change).

The exposed series are contract-pinned by shipped ops config — do NOT rename
without updating them together:
  * ``corp_llm_gateway_blocked_requests_total{block_reason}``
    (helm/corp-llm-gateway/templates/siem-alerts.yaml)
  * ``gateway_failure{component}`` (docs/ops/runbook.md)
  * ``corp_llm_gateway_request_latency_seconds`` (histogram)
"""

from __future__ import annotations

from abc import ABC, abstractmethod

_DEFAULT_CONTENT_TYPE = "text/plain; charset=utf-8"


class MetricsDependencyError(RuntimeError):
    """Raised when an exporter's optional dependency (the ``[metrics]`` extra) is absent."""


class MetricsExporter(ABC):
    """Records gateway metrics. Default impl is Noop → nothing is emitted."""

    @abstractmethod
    def record_block(self, block_reason: str) -> None:
        """Count one request refused at a Stage-0/Stage-5/policy block site."""

    @abstractmethod
    def record_failure(self, component: str) -> None:
        """Count one recorded component failure (keyed by coarse component name)."""

    @abstractmethod
    def observe_request_latency(self, seconds: float, *, status: str) -> None:
        """Observe one end-to-end request latency, labelled ok|failed."""

    def render(self) -> bytes:
        """Prometheus exposition for a ``/metrics`` route. Non-scraping exporters return empty."""
        return b""

    @property
    def content_type(self) -> str:
        """Content-Type a ``/metrics`` route should return alongside :meth:`render`."""
        return _DEFAULT_CONTENT_TYPE
