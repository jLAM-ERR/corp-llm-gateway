"""Pluggable metrics exporter (ADR-001 interface-registry).

``get_exporter()`` selects the exporter by ``CORP_METRICS_EXPORTER``
(``noop`` | ``prometheus``, default ``noop``) through the ``config`` loader —
never ``os.environ`` — mirroring ``auth/factory.py`` and ``audit/factory.py``.
An unknown value raises ``ValueError`` listing the known set. Default ``noop``
emits nothing (zero behavior change).
"""

from __future__ import annotations

from collections.abc import Callable

from corp_llm_gateway import config
from corp_llm_gateway.metrics.base import MetricsDependencyError, MetricsExporter
from corp_llm_gateway.metrics.noop import NoopExporter
from corp_llm_gateway.metrics.prometheus import PrometheusExporter

_DEFAULT_EXPORTER = "noop"


def _make_noop() -> MetricsExporter:
    return NoopExporter()


def _make_prometheus() -> MetricsExporter:
    return PrometheusExporter()


# Keyed dispatch — factories build lazily so the optional dep is imported only on
# selection (mirrors auth/factory.py + audit/factory.py).
_EXPORTER_FACTORIES: dict[str, Callable[[], MetricsExporter]] = {
    "noop": _make_noop,
    "prometheus": _make_prometheus,
}

_KNOWN_EXPORTERS = tuple(_EXPORTER_FACTORIES)


def get_exporter() -> MetricsExporter:
    """Build the metrics exporter selected by ``CORP_METRICS_EXPORTER`` (default noop)."""
    name = (config.get("CORP_METRICS_EXPORTER", _DEFAULT_EXPORTER) or _DEFAULT_EXPORTER).lower()
    factory = _EXPORTER_FACTORIES.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown CORP_METRICS_EXPORTER={name!r}; expected one of {_KNOWN_EXPORTERS}"
        )
    return factory()


__all__ = [
    "MetricsDependencyError",
    "MetricsExporter",
    "NoopExporter",
    "PrometheusExporter",
    "get_exporter",
]
