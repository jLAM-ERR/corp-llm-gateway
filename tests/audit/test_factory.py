from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from corp_llm_gateway import bootstrap
from corp_llm_gateway.audit import (
    AuditEvent,
    AuditLogger,
    LangfuseSink,
    ListSink,
    NeverFieldPresentError,
    Sink,
    SinkExtension,
    StdoutSink,
    get_sink,
    register_sink,
    sink_name_for,
)
from corp_llm_gateway.extensions import (
    EXTENSION_API_VERSION,
    REGISTRY,
    Extension,
    ExtensionApiVersionError,
    ExtensionRegistry,
    ExtensionSpec,
)
from corp_llm_gateway.healthz import HealthStatus


@pytest.fixture(autouse=True)
def _clean_config(hermetic_gateway_config: None) -> None:
    """Resolve config hermetically (see tests/conftest.py): env cleared, empty
    TOML — so only a test's own explicit values reach the factory."""


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Snapshot/restore the module-level REGISTRY so a test's registrations
    (a mismatched-api-version sink, a build_guardrail sink) never leak."""
    specs = dict(REGISTRY._specs)
    factories = dict(REGISTRY._factories)
    try:
        yield
    finally:
        REGISTRY._specs.clear()
        REGISTRY._specs.update(specs)
        REGISTRY._factories.clear()
        REGISTRY._factories.update(factories)


def _event(**overrides: object) -> AuditEvent:
    base: dict[str, object] = {
        "timestamp": datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
        "request_id": "req-1",
        "user_id": "alice",
        "team_id": "t1",
        "provider": "anthropic",
        "model": "claude-opus-4-7",
        "latency_ms": 1234,
        "prompt_token_count": 100,
        "completion_token_count": 50,
        "redaction_count": 0,
        "finding_label_counts": {},
        "cache_a_hit": False,
        "status": "ok",
    }
    base.update(overrides)
    return AuditEvent(**base)  # type: ignore[arg-type]


def _set_langfuse_env(monkeypatch: pytest.MonkeyPatch, url: str = "https://langfuse.test") -> None:
    monkeypatch.setenv("CORP_LANGFUSE_URL", url)
    monkeypatch.setenv("CORP_LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("CORP_LANGFUSE_SECRET_KEY", "sk")


# ── get_sink: config key selects the right sink ──────────────────────────────


def test_get_sink_default_is_stdout() -> None:
    assert isinstance(get_sink(), StdoutSink)


@pytest.mark.parametrize(
    "value,expected",
    [("stdout", StdoutSink), ("list", ListSink)],
)
def test_get_sink_selects_by_config(
    value: str, expected: type[Sink], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORP_AUDIT_SINK", value)
    assert isinstance(get_sink(), expected)


def test_get_sink_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_AUDIT_SINK", "LIST")
    assert isinstance(get_sink(), ListSink)


def test_get_sink_langfuse_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_AUDIT_SINK", "langfuse")
    _set_langfuse_env(monkeypatch)
    assert isinstance(get_sink(), LangfuseSink)


def test_get_sink_unknown_value_raises_listing_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_AUDIT_SINK", "splunk")
    with pytest.raises(ValueError, match="splunk") as exc:
        get_sink()
    msg = str(exc.value)
    assert "stdout" in msg and "langfuse" in msg and "list" in msg


def test_get_sink_resolves_from_config_file_not_env(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    from corp_llm_gateway import config

    assert isinstance(tmp_path, Path)
    cfg = tmp_path / "config.toml"
    cfg.write_text('CORP_AUDIT_SINK = "list"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    config.reset_cache()
    assert isinstance(get_sink(), ListSink)


# ── Langfuse endpoint is config-only (CORP_LANGFUSE_URL), not hardcoded ───────


def test_langfuse_endpoint_comes_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_AUDIT_SINK", "langfuse")
    _set_langfuse_env(monkeypatch, url="https://langfuse.mycorp.example")
    sink = get_sink()
    assert isinstance(sink, LangfuseSink)
    assert sink._base_url == "https://langfuse.mycorp.example"
    # Not the endpoint hardcoded in the old helm/vector configs.
    assert "langfuse.corp.lan" not in sink._base_url
    assert "langfuse-web" not in sink._base_url


def test_langfuse_missing_url_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_AUDIT_SINK", "langfuse")
    # keys present, URL absent
    monkeypatch.setenv("CORP_LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("CORP_LANGFUSE_SECRET_KEY", "sk")
    with pytest.raises(RuntimeError, match="CORP_LANGFUSE_URL"):
        get_sink()


# ── INVARIANT 2: NEVER-gate runs before EVERY sink kind's write ──────────────


@pytest.mark.parametrize("value", ["stdout", "list", "langfuse"])
async def test_never_gate_runs_before_every_sink_write(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if value == "langfuse":
        _set_langfuse_env(monkeypatch)
    monkeypatch.setenv("CORP_AUDIT_SINK", value)
    sink = get_sink()

    written: list[dict[str, Any]] = []

    async def _spy(record: dict[str, Any]) -> None:
        written.append(record)

    monkeypatch.setattr(sink, "write", _spy)
    logger = AuditLogger(sink, gateway_version="test")
    # Force a NEVER field into the serialized record.
    monkeypatch.setattr(
        logger, "_serialize", lambda event: {"mapping": [["alice", "[N1]"]], "user_id": "x"}
    )

    with pytest.raises(NeverFieldPresentError):
        await logger.emit(_event())
    assert written == []  # gate blocked before the sink saw anything


# ── SinkExtension: spec, health, M4 fail-policy ──────────────────────────────


def test_sink_extension_spec_shape() -> None:
    ext = SinkExtension(StdoutSink(), "stdout")
    assert ext.spec.kind == "audit_sink"
    assert ext.spec.name == "stdout"
    assert ext.spec.api_version == EXTENSION_API_VERSION
    # M4 matrix: audit-sink-down = continue.
    assert ext.spec.fail_policy == "continue"


async def test_sink_extension_stdout_and_list_always_healthy() -> None:
    assert (await SinkExtension(StdoutSink(), "stdout").health()).healthy is True
    assert (await SinkExtension(ListSink(), "list").health()).healthy is True


async def test_sink_extension_langfuse_health_probe_ok() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "OK"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = LangfuseSink("https://lf.test", public_key="pk", secret_key="sk", http=http)
    status = await SinkExtension(sink, "langfuse").health()
    assert status.healthy is True


async def test_sink_extension_langfuse_health_probe_degraded() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sink = LangfuseSink("https://lf.test", public_key="pk", secret_key="sk", http=http)
    status = await SinkExtension(sink, "langfuse").health()
    assert status.healthy is False


def test_sink_name_for_maps_known_types() -> None:
    assert sink_name_for(StdoutSink()) == "stdout"
    assert sink_name_for(ListSink()) == "list"


def test_sink_name_for_unknown_type_raises() -> None:
    class _Weird(Sink):
        async def write(self, record: dict[str, Any]) -> None: ...

    with pytest.raises(ValueError, match="_Weird"):
        sink_name_for(_Weird())


# ── register_sink: cached live instance, not per-call reconstruction ─────────


def test_register_sink_returns_cached_instance() -> None:
    reg = ExtensionRegistry()
    sink = StdoutSink()
    register_sink(reg, sink, "stdout")
    ext1 = reg.get("audit_sink", "stdout")
    ext2 = reg.get("audit_sink", "stdout")
    assert ext1 is ext2  # same cached instance across polls
    assert isinstance(ext1, SinkExtension)
    assert ext1._sink is sink


def test_register_sink_replaces_on_rebuild() -> None:
    reg = ExtensionRegistry()
    register_sink(reg, StdoutSink(), "stdout")
    # A second composition-root pass must not raise on the same key.
    register_sink(reg, StdoutSink(), "stdout")


# ── build_guardrail: registers active sink + wires the api-version gate ──────


def test_build_guardrail_registers_active_sink_as_cached_instance() -> None:
    guardrail = bootstrap.build_guardrail()
    ext1 = REGISTRY.get("audit_sink", "stdout")
    ext2 = REGISTRY.get("audit_sink", "stdout")
    assert ext1 is ext2  # factory returns the cached instance, no rebuild
    assert isinstance(ext1, SinkExtension)
    # The registered extension wraps the SAME live sink the logger writes to.
    assert ext1._sink is guardrail._audit._sink


async def test_health_all_does_not_churn_the_sink_instance() -> None:
    bootstrap.build_guardrail()
    before = REGISTRY.get("audit_sink", "stdout")._sink  # type: ignore[attr-defined]
    await REGISTRY.health_all()
    after = REGISTRY.get("audit_sink", "stdout")._sink  # type: ignore[attr-defined]
    assert before is after  # no per-poll reconstruction


def test_build_guardrail_selects_sink_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_AUDIT_SINK", "list")
    guardrail = bootstrap.build_guardrail()
    assert isinstance(guardrail._audit._sink, ListSink)
    assert isinstance(REGISTRY.get("audit_sink", "list"), SinkExtension)


def test_build_guardrail_sink_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_AUDIT_SINK", "langfuse")  # would need keys; override skips it
    override = ListSink()
    guardrail = bootstrap.build_guardrail(sink=override)
    assert guardrail._audit._sink is override


def test_build_guardrail_rejects_mismatched_api_version() -> None:
    bad_spec = ExtensionSpec(name="badsink", kind="audit_sink", version="1", api_version="99")

    class _Stub(Extension):
        def __init__(self, spec: ExtensionSpec) -> None:
            self.spec = spec

        async def health(self) -> HealthStatus:
            return HealthStatus(True, "stub")

    REGISTRY.register(bad_spec, lambda: _Stub(bad_spec), replace=True)
    with pytest.raises(ExtensionApiVersionError):
        bootstrap.build_guardrail()


# ═══════════════════════════════════════════════════════════════════════════
# Hardening: boundary / error-path / leak-critical edge cases
# ═══════════════════════════════════════════════════════════════════════════


def _langfuse_with(handler: Callable[[httpx.Request], httpx.Response]) -> LangfuseSink:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LangfuseSink("https://lf.test", public_key="pk", secret_key="sk", http=http)


# ── get_sink: empty / whitespace / missing-credential boundaries ─────────────


def test_get_sink_empty_string_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # An env var present-but-empty (e.g. `CORP_AUDIT_SINK=` in a k8s manifest)
    # must resolve to the default, not raise.
    monkeypatch.setenv("CORP_AUDIT_SINK", "")
    assert isinstance(get_sink(), StdoutSink)


def test_get_sink_whitespace_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Whitespace is NOT stripped: "  list  " does not resolve to "list". It fails
    # closed (ValueError listing the known set) rather than silently defaulting —
    # loud, but a stray space in config is a hard error (see report note).
    monkeypatch.setenv("CORP_AUDIT_SINK", "  list  ")
    with pytest.raises(ValueError) as exc:
        get_sink()
    msg = str(exc.value)
    assert "stdout" in msg and "langfuse" in msg and "list" in msg


def test_get_sink_langfuse_missing_public_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_AUDIT_SINK", "langfuse")
    monkeypatch.setenv("CORP_LANGFUSE_URL", "https://lf.test")
    monkeypatch.setenv("CORP_LANGFUSE_SECRET_KEY", "sk")
    with pytest.raises(RuntimeError, match="CORP_LANGFUSE_PUBLIC_KEY"):
        get_sink()


def test_get_sink_langfuse_missing_secret_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_AUDIT_SINK", "langfuse")
    monkeypatch.setenv("CORP_LANGFUSE_URL", "https://lf.test")
    monkeypatch.setenv("CORP_LANGFUSE_PUBLIC_KEY", "pk")
    with pytest.raises(RuntimeError, match="CORP_LANGFUSE_SECRET_KEY"):
        get_sink()


def test_get_sink_langfuse_empty_url_raises_no_silent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A present-but-empty URL must fail-fast; it must NOT build a sink with a
    # blank/hardcoded base_url (a silent fallback endpoint would be a finding).
    monkeypatch.setenv("CORP_AUDIT_SINK", "langfuse")
    monkeypatch.setenv("CORP_LANGFUSE_URL", "")
    monkeypatch.setenv("CORP_LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("CORP_LANGFUSE_SECRET_KEY", "sk")
    with pytest.raises(RuntimeError, match="CORP_LANGFUSE_URL"):
        get_sink()


# ── INVARIANT 2: no sink selection can smuggle a NEVER field past the gate ────


@pytest.mark.parametrize("sink_kind", ["stdout", "list", "langfuse"])
@pytest.mark.parametrize("never_field", ["original_content", "authorization", "corp_token"])
async def test_never_gate_refuses_original_and_credential_fields_for_every_sink(
    sink_kind: str, never_field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross original/credential NEVER fields with every sink kind: the gate in
    AuditLogger.emit refuses before the selected sink's write ever runs."""
    if sink_kind == "langfuse":
        _set_langfuse_env(monkeypatch)
    monkeypatch.setenv("CORP_AUDIT_SINK", sink_kind)
    sink = get_sink()

    written: list[dict[str, Any]] = []

    async def _spy(record: dict[str, Any]) -> None:
        written.append(record)

    monkeypatch.setattr(sink, "write", _spy)
    logger = AuditLogger(sink, gateway_version="test")
    monkeypatch.setattr(
        logger, "_serialize", lambda event: {never_field: "SENSITIVE", "user_id": "x"}
    )

    with pytest.raises(NeverFieldPresentError):
        await logger.emit(_event())
    assert written == []


def test_sink_extension_is_not_a_writable_sink() -> None:
    # The registry adapter must NOT re-expose write(): a consumer that pulls the
    # sink out of REGISTRY.get() cannot write around the AuditLogger gate.
    ext = SinkExtension(StdoutSink(), "stdout")
    assert not isinstance(ext, Sink)
    assert not hasattr(ext, "write")


async def test_langfuse_sink_self_gate_refuses_never_field_on_direct_write() -> None:
    # Defense in depth: even fed directly (bypassing AuditLogger), LangfuseSink's
    # own gate refuses a NEVER field BEFORE any HTTP POST leaves the process.
    posted: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        posted.append(req)
        return httpx.Response(200)

    sink = _langfuse_with(handler)
    with pytest.raises(NeverFieldPresentError):
        await sink.write({"mapping": [["alice", "[N1]"]]})
    assert posted == []


# ── instance lifecycle: health polls reuse the cached sink, never rebuild ─────


class _CountingSink(Sink):
    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1

    async def write(self, record: dict[str, Any]) -> None: ...


async def test_health_all_does_not_reconstruct_sink_per_poll() -> None:
    _CountingSink.instances = 0
    reg = ExtensionRegistry()
    sink = _CountingSink()
    register_sink(reg, sink, "stdout")
    assert _CountingSink.instances == 1
    for _ in range(3):
        report = await reg.health_all()
        assert report["audit_sink:stdout"].healthy is True
    assert _CountingSink.instances == 1  # cached instance reused, never rebuilt
    assert reg.get("audit_sink", "stdout")._sink is sink  # type: ignore[attr-defined]


async def test_health_all_reuses_langfuse_http_client_across_polls() -> None:
    # The whole reason for caching a live instance: a connection-holding sink
    # must keep the SAME AsyncClient across /healthz polls (no client churn).
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    sink = _langfuse_with(handler)
    original_http = sink._http
    reg = ExtensionRegistry()
    register_sink(reg, sink, "langfuse")

    r1 = await reg.health_all()
    r2 = await reg.health_all()
    assert r1["audit_sink:langfuse"].healthy is False
    assert r2["audit_sink:langfuse"].healthy is False
    assert reg.get("audit_sink", "langfuse")._sink._http is original_http  # type: ignore[attr-defined]


# ── registration / replace semantics (C1 fail-closed contract) ───────────────


def test_register_sink_then_plain_register_same_key_rejected() -> None:
    # register_sink uses replace=True; a plain register of the same (kind, name)
    # is still rejected — the fail-closed duplicate guard is intact.
    reg = ExtensionRegistry()
    ext = register_sink(reg, StdoutSink(), "stdout")
    with pytest.raises(ValueError, match="audit_sink:stdout"):
        reg.register(ext.spec, lambda: ext)


def test_build_guardrail_twice_does_not_raise_on_reregister() -> None:
    bootstrap.build_guardrail()
    bootstrap.build_guardrail()  # trusted root re-registers its own sink: no raise
    assert isinstance(REGISTRY.get("audit_sink", "stdout"), SinkExtension)


def test_rebuild_swaps_cached_instance_no_stale_sink() -> None:
    g1 = bootstrap.build_guardrail()
    first = REGISTRY.get("audit_sink", "stdout")._sink  # type: ignore[attr-defined]
    assert first is g1._audit._sink
    g2 = bootstrap.build_guardrail()
    after = REGISTRY.get("audit_sink", "stdout")._sink  # type: ignore[attr-defined]
    assert after is g2._audit._sink
    assert after is not first  # rebuild serves the new instance, not a stale one


# ── api-version gate: the healthy path passes and the active sink matches core ─


def test_build_guardrail_active_sink_passes_api_version_gate() -> None:
    bootstrap.build_guardrail()  # must not raise
    spec = REGISTRY.get("audit_sink", "stdout").spec
    assert spec.api_version == EXTENSION_API_VERSION


# ── Langfuse health probe: status boundary + timeout are degraded, not raised ─


async def test_langfuse_health_5xx_is_degraded() -> None:
    sink = _langfuse_with(lambda _req: httpx.Response(503))
    status = await sink.health()
    assert status.healthy is False
    assert "503" in status.detail


async def test_langfuse_health_4xx_below_500_is_healthy() -> None:
    # Boundary at 500: a 404 (missing/renamed health path) reads as HEALTHY.
    # Lenient by design for a fail-continue audit sink; see report observation.
    sink = _langfuse_with(lambda _req: httpx.Response(404))
    assert (await sink.health()).healthy is True


async def test_langfuse_health_timeout_is_degraded_not_raised() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    sink = _langfuse_with(handler)
    status = await sink.health()
    assert status.healthy is False


def test_sink_name_for_maps_langfuse_instance() -> None:
    sink = _langfuse_with(lambda _req: httpx.Response(200))
    assert sink_name_for(sink) == "langfuse"
