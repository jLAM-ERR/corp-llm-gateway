"""B4 metrics: pluggable exporter + the litellm_hook block/failure/latency series.

The naming trap is pinned here: ``gateway_failure`` MUST expose exactly that name
(a Gauge), not ``gateway_failure_total`` (what a Counter would expose). Prometheus
tests ``importorskip`` the wheel, so they skip on the 3.14 graceful-degradation
venv and run under CI's ``[metrics]`` extra.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from corp_llm_gateway import config
from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.corp_llm import CorpLlmClient
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
from corp_llm_gateway.metrics import (
    MetricsDependencyError,
    MetricsExporter,
    NoopExporter,
    PrometheusExporter,
    get_exporter,
)
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo
from tests.test_litellm_hook import (
    _corp_llm_returning,
    _corp_llm_unreachable,
    _data_with_token,
    _StaticRules,
)

_HAS_PROM = importlib.util.find_spec("prometheus_client") is not None

_ENV_PAYLOAD = (
    "DATABASE_URL=postgres://admin:pass@db/prod\n"
    "SECRET_KEY=supersecretvalue\n"
    "DEBUG=False\n"
    "REDIS_URL=redis://cache\n"
    "LOG_LEVEL=ERROR\n"
)


@pytest.fixture(autouse=True)
def _hermetic_metrics_config(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Resolve the metrics keys hermetically: cleared env + empty TOML file."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    for name in ("CORP_METRICS_EXPORTER", "CORP_TRACING_EXPORTER"):
        monkeypatch.delenv(name, raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    config.reset_cache()
    yield
    config.reset_cache()


def _prom() -> PrometheusExporter:
    pytest.importorskip("prometheus_client")
    return PrometheusExporter()


def _guardrail(
    metrics: MetricsExporter | None,
    *,
    corp_llm: CorpLlmClient | None = None,
    dlp_guard: DlpEgressGuard | None = None,
) -> tuple[CorpLlmGuardrail, ListSink]:
    store = InMemoryTokenStore()
    now = datetime.now(UTC)
    store.upsert(
        TokenInfo(
            corp_token="tok-1",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    orch = SanitizationOrchestrator(
        corp_llm if corp_llm is not None else _corp_llm_returning([]),
        InMemoryMappingStore(),
        _StaticRules(),
    )
    sink = ListSink()
    g = CorpLlmGuardrail(
        orch,
        AuthMiddleware(store),
        AuditLogger(sink, gateway_version="0.0.1"),
        dlp_guard=dlp_guard,
        metrics=metrics,
    )
    return g, sink


# ── ABC + Noop default ───────────────────────────────────────────────────────


def test_metrics_exporter_abc_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        MetricsExporter()  # type: ignore[abstract]


def test_noop_records_nothing_and_renders_empty() -> None:
    exporter = NoopExporter()
    # No-ops return cleanly and expose no series.
    exporter.record_block("dlp:canary")
    exporter.record_failure("corp_llm")
    exporter.observe_request_latency(0.1, status="ok")
    assert exporter.render() == b""


def test_get_exporter_default_is_noop() -> None:
    assert isinstance(get_exporter(), NoopExporter)


def test_get_exporter_empty_value_falls_back_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_METRICS_EXPORTER", "")
    assert isinstance(get_exporter(), NoopExporter)


def test_get_exporter_unknown_raises_listing_known(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_METRICS_EXPORTER", "statsd")
    with pytest.raises(ValueError, match="statsd") as exc:
        get_exporter()
    msg = str(exc.value)
    assert "noop" in msg and "prometheus" in msg


@pytest.mark.skipif(_HAS_PROM, reason="prometheus_client installed; absent-dep path unreachable")
def test_get_exporter_prometheus_without_dep_raises_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORP_METRICS_EXPORTER", "prometheus")
    with pytest.raises(MetricsDependencyError, match="metrics"):
        get_exporter()


# ── get_exporter selection (prometheus) ──────────────────────────────────────


def test_get_exporter_selects_prometheus(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("prometheus_client")
    monkeypatch.setenv("CORP_METRICS_EXPORTER", "prometheus")
    assert isinstance(get_exporter(), PrometheusExporter)


def test_get_exporter_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("prometheus_client")
    monkeypatch.setenv("CORP_METRICS_EXPORTER", "PROMETHEUS")
    assert isinstance(get_exporter(), PrometheusExporter)


def test_get_exporter_resolves_from_config_file_not_env(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    pytest.importorskip("prometheus_client")
    assert isinstance(tmp_path, Path)
    monkeypatch.delenv("CORP_METRICS_EXPORTER", raising=False)
    cfg = tmp_path / "from-file.toml"
    cfg.write_text('CORP_METRICS_EXPORTER = "prometheus"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    config.reset_cache()
    assert isinstance(get_exporter(), PrometheusExporter)


# ── Prometheus exporter: exposition + the gateway_failure naming trap ─────────


def test_prometheus_render_contains_all_three_series() -> None:
    exporter = _prom()
    exporter.record_block("dlp:canary")
    exporter.record_failure("corp_llm")
    exporter.observe_request_latency(0.25, status="ok")
    text = exporter.render().decode()
    assert "corp_llm_gateway_blocked_requests_total" in text
    assert "gateway_failure" in text
    assert "corp_llm_gateway_request_latency_seconds" in text


def test_prometheus_block_counter_labels_block_reason() -> None:
    exporter = _prom()
    exporter.record_block("dlp:canary")
    text = exporter.render().decode()
    assert 'corp_llm_gateway_blocked_requests_total{block_reason="dlp:canary"}' in text


def test_gateway_failure_exposed_name_is_exactly_gateway_failure() -> None:
    # The trap: a Counter would expose `gateway_failure_total`; the runbook series
    # is the bare `gateway_failure`, so the exporter uses a Gauge.
    exporter = _prom()
    exporter.record_failure("corp_llm")
    text = exporter.render().decode()
    assert 'gateway_failure{component="corp_llm"}' in text
    assert "gateway_failure_total" not in text
    assert "# TYPE gateway_failure gauge" in text


def test_prometheus_latency_histogram_observed() -> None:
    exporter = _prom()
    exporter.observe_request_latency(0.5, status="failed")
    text = exporter.render().decode()
    assert "corp_llm_gateway_request_latency_seconds_bucket{" in text
    assert 'corp_llm_gateway_request_latency_seconds_count{status="failed"}' in text


def test_prometheus_content_type_is_exposition_format() -> None:
    exporter = _prom()
    assert "text/plain" in exporter.content_type


def test_prometheus_asgi_app_is_callable() -> None:
    exporter = _prom()
    assert callable(exporter.asgi_app())


def test_prometheus_instances_have_independent_registries() -> None:
    _prom()  # skip-guard when the wheel is absent
    e1 = PrometheusExporter()
    e2 = PrometheusExporter()  # a second instance must not raise "Duplicated timeseries"
    e1.record_failure("corp_llm")
    assert 'gateway_failure{component="corp_llm"}' in e1.render().decode()
    assert 'gateway_failure{component="corp_llm"}' not in e2.render().decode()


# ── Hook instrumentation: blocked_requests_total at block sites ───────────────


async def test_hook_stage0_block_increments_blocked_counter() -> None:
    exporter = _prom()
    g, _ = _guardrail(exporter)
    data = _data_with_token("tok-1", content=_ENV_PAYLOAD)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_POLICY_BLOCKED"
    text = exporter.render().decode()
    assert 'corp_llm_gateway_blocked_requests_total{block_reason="config:env"}' in text
    # _record_failure also fires gateway_failure for the policy component.
    assert 'gateway_failure{component="policy"}' in text


async def test_hook_stage5_dlp_block_increments_blocked_counter() -> None:
    exporter = _prom()
    canary = "DLP-CANARY-RAW-99999"
    g, _ = _guardrail(
        exporter, dlp_guard=DlpEgressGuard(canary_patterns=[canary], secret_rescan=False)
    )
    data = _data_with_token("tok-1", content=f"here is {canary}")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_DLP_BLOCKED"
    text = exporter.render().decode()
    assert 'corp_llm_gateway_blocked_requests_total{block_reason="dlp:canary"}' in text
    assert 'gateway_failure{component="dlp"}' in text
    # No raw canary value leaks into the exposition.
    assert canary not in text


# ── Hook instrumentation: gateway_failure{component} at _record_failure ───────


async def test_hook_corp_llm_down_increments_gateway_failure_corp_llm() -> None:
    exporter = _prom()
    g, _ = _guardrail(exporter, corp_llm=_corp_llm_unreachable())
    data = _data_with_token("tok-1", content="hello alice")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_CORP_LLM_DOWN"
    text = exporter.render().decode()
    # Matches the runbook's exact series (component + bare name, no _total).
    assert 'gateway_failure{component="corp_llm"}' in text
    assert "gateway_failure_total" not in text


async def test_hook_auth_failure_increments_gateway_failure_auth() -> None:
    exporter = _prom()
    g, _ = _guardrail(exporter)
    # Missing token: _record_failure fires before per-request state exists.
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call({"messages": [], "headers": {}})
    assert ei.value.error_code == "E_MISSING_TOKEN"
    assert 'gateway_failure{component="auth"}' in exporter.render().decode()


async def test_hook_latency_histogram_observed_on_audit() -> None:
    exporter = _prom()
    g, _ = _guardrail(exporter)
    start = datetime.now(UTC)
    await g.audit({"model": "claude"}, None, start_time=start, end_time=start, status="ok")
    text = exporter.render().decode()
    assert 'corp_llm_gateway_request_latency_seconds_count{status="ok"}' in text


# ── Default noop: zero behavior change on the block path ──────────────────────


async def test_hook_default_noop_leaves_block_path_unchanged() -> None:
    # No exporter passed → the guardrail builds its own NoopExporter.
    g, sink = _guardrail(None)
    data = _data_with_token("tok-1", content=_ENV_PAYLOAD)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_POLICY_BLOCKED"
    # The Stage-0 block still audits inline exactly once — unchanged by the noop path.
    assert len(sink.records) == 1
