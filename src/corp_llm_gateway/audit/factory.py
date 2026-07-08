"""Audit-sink factory + extension-registry adapter (Task C2).

``get_sink()`` makes audit-sink selection genuinely config-only: ``CORP_AUDIT_SINK``
picks ``stdout | langfuse | list`` through the ``config`` loader (never
``os.environ``); an unknown value raises ``ValueError`` listing the known set
(ADR-001 rule 5). The Langfuse endpoint comes from ``CORP_LANGFUSE_URL`` — no
product URL is hardcoded here.

``SinkExtension`` adapts a *live* sink to the extension registry. Instance
lifecycle (safe-extension-registry skill): the registry factory returns the
already-built sink instance (cached), NOT a fresh construction per call — so
``health_all()`` on every ``/healthz/extensions`` poll never churns a
connection-holding sink's HTTP client (Langfuse). ``build_guardrail`` builds the
sink once, registers that instance, and feeds the SAME object to ``AuditLogger``.

The NEVER-fields gate (invariant 2) is untouched: it runs in ``AuditLogger.emit``
before every sink write regardless of which sink this factory selects.
"""

from __future__ import annotations

from collections.abc import Callable

from corp_llm_gateway import config
from corp_llm_gateway.audit.langfuse_sink import LangfuseSink
from corp_llm_gateway.audit.sinks import ListSink, Sink, StdoutSink
from corp_llm_gateway.extensions import EXTENSION_API_VERSION, Extension, ExtensionSpec
from corp_llm_gateway.extensions.registry import ExtensionRegistry
from corp_llm_gateway.healthz import HealthStatus

_DEFAULT_SINK = "stdout"
_SINK_VERSION = "1"


def _make_stdout() -> Sink:
    return StdoutSink()


def _make_list() -> Sink:
    return ListSink()


def _make_langfuse() -> Sink:
    return LangfuseSink(
        config.get_required("CORP_LANGFUSE_URL"),
        public_key=config.get_required("CORP_LANGFUSE_PUBLIC_KEY"),
        secret_key=config.get_required("CORP_LANGFUSE_SECRET_KEY"),
    )


_SINK_FACTORIES: dict[str, Callable[[], Sink]] = {
    "stdout": _make_stdout,
    "langfuse": _make_langfuse,
    "list": _make_list,
}

_KNOWN_SINKS = tuple(_SINK_FACTORIES)

# Reverse map so an already-built sink (incl. an injected override) resolves to
# the same name its factory used — the registered extension name always matches
# the live object.
_SINK_NAMES: dict[type[Sink], str] = {
    StdoutSink: "stdout",
    ListSink: "list",
    LangfuseSink: "langfuse",
}


def get_sink() -> Sink:
    """Build the audit sink selected by ``CORP_AUDIT_SINK`` (default ``stdout``)."""
    name = (config.get("CORP_AUDIT_SINK", _DEFAULT_SINK) or _DEFAULT_SINK).lower()
    factory = _SINK_FACTORIES.get(name)
    if factory is None:
        raise ValueError(f"Unknown CORP_AUDIT_SINK={name!r}; expected one of {_KNOWN_SINKS}")
    return factory()


def sink_name_for(sink: Sink) -> str:
    """Extension name for a built sink instance (for spec + registration)."""
    name = _SINK_NAMES.get(type(sink))
    if name is None:
        raise ValueError(
            f"no audit_sink extension name for {type(sink).__name__}; "
            f"known sink types: {tuple(t.__name__ for t in _SINK_NAMES)}"
        )
    return name


class SinkExtension(Extension):
    """Adapt a live audit ``Sink`` to the extension registry (kind=audit_sink)."""

    def __init__(self, sink: Sink, name: str) -> None:
        self._sink = sink
        self.spec = ExtensionSpec(
            name=name,
            kind="audit_sink",
            version=_SINK_VERSION,
            api_version=EXTENSION_API_VERSION,
            capabilities=frozenset({"write"}),
            # M4 fail-policy matrix: audit-sink-down = continue. A dead sink must
            # never block egress (mirrors FailPolicyOverrides.audit_sink_down).
            fail_policy="continue",
        )

    async def health(self) -> HealthStatus:
        if isinstance(self._sink, LangfuseSink):
            return await self._sink.health()
        # stdout / list hold no external resource — always healthy.
        return HealthStatus(True, f"{self.spec.name}_ok")


def register_sink(registry: ExtensionRegistry, sink: Sink, name: str) -> SinkExtension:
    """Register a live sink as an audit_sink Extension with a cached-instance factory.

    The factory closes over the already-built ``ext`` and returns that SAME
    object on every call (no per-poll reconstruction — the instance-lifecycle
    decision from the safe-extension-registry skill). ``replace=True`` is safe:
    ``build_guardrail`` is the trusted composition root re-registering its own
    well-known sink across repeated calls, not an untrusted plugin shadowing a
    security component.
    """
    ext = SinkExtension(sink, name)
    registry.register(ext.spec, lambda: ext, replace=True)
    return ext
