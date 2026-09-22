"""Pluggable metrics exporter (ADR-001 interface-registry).

``get_exporter()`` selects the exporter by ``CORP_METRICS_EXPORTER``
(``noop`` | ``prometheus``, default ``noop``) through the ``config`` loader —
never ``os.environ`` — mirroring ``auth/factory.py`` and ``audit/factory.py``.
An unknown value raises ``ValueError`` listing the known set. Default ``noop``
emits nothing (zero behavior change).

``get_exporter()`` returns ONE process-wide instance; ``build_exporter()`` is
the unshared factory for callers that want their own registry (tests).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from corp_llm_gateway import config
from corp_llm_gateway.metrics.base import (
    BLOCK_REASONS,
    FAILURE_COMPONENTS,
    MetricsDependencyError,
    MetricsExporter,
)
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


_shared: MetricsExporter | None = None
# PrometheusExporter registers its collectors on construction, and a duplicate
# name raises. Two threads racing the first call would build two exporters.
_shared_lock = threading.Lock()


def build_exporter() -> MetricsExporter:
    """Build a NEW exporter selected by ``CORP_METRICS_EXPORTER`` (default noop)."""
    name = (config.get("CORP_METRICS_EXPORTER", _DEFAULT_EXPORTER) or _DEFAULT_EXPORTER).lower()
    factory = _EXPORTER_FACTORIES.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown CORP_METRICS_EXPORTER={name!r}; expected one of {_KNOWN_EXPORTERS}"
        )
    return factory()


def get_exporter() -> MetricsExporter:
    """The process-wide exporter, built on first use.

    One instance, deliberately: ``PrometheusExporter`` gives each instance its
    OWN registry, so a second one would count into a registry nothing scrapes.
    The route gate and the guardrail both record blocks, and ``asgi.py`` exposes
    exactly this instance at ``/metrics`` — they have to be the same object or
    half the counts are invisible.
    """
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                _shared = build_exporter()
    return _shared


def reset_exporter() -> None:
    """Drop the shared instance (tests; a re-read of ``CORP_METRICS_EXPORTER``)."""
    global _shared
    with _shared_lock:
        _shared = None


__all__ = [
    "BLOCK_REASONS",
    "FAILURE_COMPONENTS",
    "MetricsDependencyError",
    "MetricsExporter",
    "NoopExporter",
    "PrometheusExporter",
    "build_exporter",
    "get_exporter",
    "reset_exporter",
]
