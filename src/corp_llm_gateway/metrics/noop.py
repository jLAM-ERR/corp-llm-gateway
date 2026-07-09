"""Default metrics exporter — records nothing (zero behavior change)."""

from __future__ import annotations

from corp_llm_gateway.metrics.base import MetricsExporter


class NoopExporter(MetricsExporter):
    """No-op exporter: the default when ``CORP_METRICS_EXPORTER`` is unset."""

    def record_block(self, block_reason: str) -> None:
        return None

    def record_failure(self, component: str) -> None:
        return None

    def observe_request_latency(self, seconds: float, *, status: str) -> None:
        return None
