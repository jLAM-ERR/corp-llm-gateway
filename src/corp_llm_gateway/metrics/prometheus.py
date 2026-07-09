"""Prometheus metrics exporter (needs the ``[metrics]`` extra: prometheus-client).

Lazy-imports ``prometheus_client`` inside ``__init__`` so the package stays
importable on the 3.14 graceful-degradation venv (no wheel there) — mirrors how
the NER detectors keep the package importable without their optional deps.

Metric TYPE matters for the exposed name (empirically verified):
  * ``Counter("gateway_failure")`` would expose ``gateway_failure_total`` — WRONG;
    the runbook series is exactly ``gateway_failure``, so it is a ``Gauge`` here.
  * ``Counter("…_total")`` exposes exactly ``…_total`` (no double-append), so the
    block counter keeps the ``_total`` suffix the alert rule references.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from corp_llm_gateway.metrics.base import MetricsDependencyError, MetricsExporter

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry

# Exposed series names — pinned by shipped ops config (siem-alerts.yaml, runbook.md).
BLOCKED_REQUESTS_METRIC = "corp_llm_gateway_blocked_requests_total"
GATEWAY_FAILURE_METRIC = "gateway_failure"
REQUEST_LATENCY_METRIC = "corp_llm_gateway_request_latency_seconds"

_MISSING_DEP_HINT = (
    "the prometheus metrics exporter requires the [metrics] extra: "
    "pip install 'corp-llm-gateway[metrics]'"
)


class PrometheusExporter(MetricsExporter):
    """Exports the gateway series to a private Prometheus registry, served at /metrics."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        try:
            from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
        except ImportError as exc:  # no wheel on the 3.14 venv
            raise MetricsDependencyError(_MISSING_DEP_HINT) from exc
        # Own registry (not the global default) so instances never collide and
        # tests stay isolated.
        self._registry = registry if registry is not None else CollectorRegistry()
        self._blocked = Counter(
            BLOCKED_REQUESTS_METRIC,
            "Requests refused at a Stage-0/Stage-5/policy block site.",
            ["block_reason"],
            registry=self._registry,
        )
        # Gauge (not Counter) so the exposed name is exactly `gateway_failure`.
        self._failure = Gauge(
            GATEWAY_FAILURE_METRIC,
            "Component failures recorded by the gateway.",
            ["component"],
            registry=self._registry,
        )
        self._latency = Histogram(
            REQUEST_LATENCY_METRIC,
            "End-to-end gateway request latency in seconds.",
            ["status"],
            registry=self._registry,
        )

    def record_block(self, block_reason: str) -> None:
        self._blocked.labels(block_reason=block_reason).inc()

    def record_failure(self, component: str) -> None:
        self._failure.labels(component=component).inc()

    def observe_request_latency(self, seconds: float, *, status: str) -> None:
        self._latency.labels(status=status).observe(seconds)

    def render(self) -> bytes:
        from prometheus_client import generate_latest

        payload: bytes = generate_latest(self._registry)
        return payload

    @property
    def content_type(self) -> str:
        from prometheus_client import CONTENT_TYPE_LATEST

        return str(CONTENT_TYPE_LATEST)

    def asgi_app(self) -> Any:
        """ASGI app exposing this registry — mount at ``/metrics`` on the proxy app."""
        from prometheus_client import make_asgi_app

        return make_asgi_app(registry=self._registry)
