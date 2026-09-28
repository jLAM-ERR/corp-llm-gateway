"""B4 metrics: pluggable exporter + the litellm_hook block/failure/latency series.

The naming trap is pinned here: ``gateway_failure`` MUST expose exactly that name
(a Gauge), not ``gateway_failure_total`` (what a Counter would expose). Prometheus
tests ``importorskip`` the wheel, so they skip on the 3.14 graceful-degradation
venv and run under CI's ``[metrics]`` extra.
"""

from __future__ import annotations

import ast
import importlib.util
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import corp_llm_gateway as gateway_package
from corp_llm_gateway import config
from corp_llm_gateway import metrics as metrics_module
from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.corp_llm import CorpLlmClient
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
from corp_llm_gateway.metrics import (
    BLOCK_REASONS,
    FAILURE_COMPONENTS,
    MetricsDependencyError,
    MetricsExporter,
    NoopExporter,
    PrometheusExporter,
    build_exporter,
    get_exporter,
    reset_exporter,
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


# ── One exporter per process (the /metrics route and the gate share it) ──────


def test_get_exporter_returns_the_same_instance_every_time() -> None:
    # PrometheusExporter gives each instance its OWN registry, so a second one
    # would count into a registry nothing scrapes. The route gate, the guardrail
    # and asgi.py's /metrics route all resolve through here.
    assert get_exporter() is get_exporter()


def test_build_exporter_returns_a_new_instance_each_time() -> None:
    assert build_exporter() is not build_exporter()


def test_reset_exporter_re_reads_the_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Other(NoopExporter):
        pass

    first = get_exporter()
    assert isinstance(first, NoopExporter)

    monkeypatch.setitem(metrics_module._EXPORTER_FACTORIES, "noop", _Other)
    assert get_exporter() is first, "a cached exporter must not change under a live process"

    reset_exporter()
    assert isinstance(get_exporter(), _Other)


# ── the /metrics ASGI app ────────────────────────────────────────────────────


async def _call(app: object) -> tuple[int, bytes, dict[bytes, bytes]]:
    messages: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    await app({"type": "http", "method": "GET", "path": "/metrics", "headers": []}, receive, send)  # type: ignore[operator]
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], body, dict(start["headers"])


async def test_the_noop_exporter_still_serves_metrics() -> None:
    # A 404 here is indistinguishable from a dead pod to a scrape; answer 200
    # with nothing instead.
    status, body, headers = await _call(NoopExporter().asgi_app())

    assert status == 200
    assert body == b""
    assert headers[b"content-type"] == b"text/plain; charset=utf-8"


async def test_the_prometheus_exporter_serves_its_own_registry() -> None:
    exporter = _prom()
    exporter.record_block("route_gate_listed")

    status, body, _ = await _call(exporter.asgi_app())

    assert status == 200
    assert b'corp_llm_gateway_blocked_requests_total{block_reason="route_gate_listed"}' in body


# ── the label values the series can carry ────────────────────────────────────

ALL_BLOCK_REASONS: tuple[str, ...] = tuple(
    reason for site in BLOCK_REASONS.values() for reason in site
)


def test_the_route_gate_reasons_match_the_gate_itself() -> None:
    # The gate's set is the source; base.py restates it (importing route_gate
    # there would be a cycle), so the restatement has to be pinned.
    from corp_llm_gateway.route_gate.classify import BLOCK_REASONS as GATE_REASONS

    assert set(BLOCK_REASONS["route_gate"]) == set(GATE_REASONS)


def _returned_string_literals(module: object) -> set[str]:
    """Every `return "literal"` in a module — the reason codes it can produce.

    Literal returns only: a reason built at runtime (an f-string, a name) is
    invisible here, so a block site that stops returning literals silently
    narrows this check.
    """
    source = Path(module.__file__).read_text(encoding="utf-8")  # type: ignore[attr-defined]
    return {
        node.value.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def test_the_stage0_reasons_match_the_payload_classifier() -> None:
    # `dlp:secret` sat in docs/audit-schema.md for a release while the guard
    # emitted `dlp:secret_leak`. Both block sites are now read off their own
    # source, so a renamed code fails here instead of silencing an alert.
    from corp_llm_gateway.payload import classifier

    assert set(BLOCK_REASONS["stage0"]) == _returned_string_literals(classifier)


def test_the_stage5_reasons_match_the_dlp_guard() -> None:
    from corp_llm_gateway.sanitizer import dlp_guard

    assert set(BLOCK_REASONS["stage5"]) == _returned_string_literals(dlp_guard)


def test_every_enumerated_block_reason_is_documented_in_the_audit_schema() -> None:
    # docs/audit-schema.md is where the audit-side reader looks up a reason code.
    schema = (Path(gateway_package.__file__).parents[2] / "docs" / "audit-schema.md").read_text(
        encoding="utf-8"
    )
    row = next(
        (line for line in schema.splitlines() if line.startswith("| `block_reason` |")), None
    )
    assert row is not None, "docs/audit-schema.md has no `block_reason` row"
    missing = [reason for reason in ALL_BLOCK_REASONS if f"`{reason}`" not in row]

    assert not missing, missing


def test_every_recorded_block_reason_literal_is_enumerated() -> None:
    # A new `record_block("...")` anywhere in src has to appear in the
    # enumeration, or an alert written from it silently never fires.
    literals = set()
    for path in Path(gateway_package.__file__).parent.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "record_block"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                literals.add(node.args[0].value)

    assert literals, "no record_block call with a literal reason was found; re-read the walker"
    assert literals <= set(ALL_BLOCK_REASONS), literals - set(ALL_BLOCK_REASONS)


def test_the_failure_components_match_the_hooks_own_map() -> None:
    from corp_llm_gateway.litellm_hook import _FAILURE_COMPONENT, TEAM_CONFIG_COMPONENT
    from corp_llm_gateway.route_gate import desanitize_middleware, terminal_audit
    from corp_llm_gateway.route_gate.middleware import COMPONENT

    assert set(FAILURE_COMPONENTS) == set(_FAILURE_COMPONENT.values()) | {
        "other",
        COMPONENT,
        TEAM_CONFIG_COMPONENT,
        desanitize_middleware.COMPONENT,
        terminal_audit.COMPONENT,
    }


@pytest.mark.parametrize("reason", ALL_BLOCK_REASONS)
def test_every_enumerated_block_reason_renders_on_the_real_series(reason: str) -> None:
    exporter = _prom()

    exporter.record_block(reason)

    text = exporter.render().decode()
    assert f'corp_llm_gateway_blocked_requests_total{{block_reason="{reason}"}}' in text


@pytest.mark.parametrize("component", FAILURE_COMPONENTS)
def test_every_enumerated_failure_component_renders_on_the_real_series(component: str) -> None:
    exporter = _prom()

    exporter.record_failure(component)

    text = exporter.render().decode()
    assert f'gateway_failure{{component="{component}"}}' in text


async def test_the_gate_records_its_reason_and_component_through_the_exporter() -> None:
    # End to end through the middleware, not a hand-called exporter: the gate is
    # what an operator's `route_gate_*` alert actually counts.
    from corp_llm_gateway.route_gate import RouteGateMiddleware
    from corp_llm_gateway.route_gate.middleware import COMPONENT

    exporter = _prom()

    async def _never_called(scope: object, receive: object, send: object) -> None:
        raise AssertionError("the gate forwarded a request it had to refuse")

    gate = RouteGateMiddleware(
        _never_called,
        metrics=exporter,
        audit_logger=AuditLogger(ListSink(), gateway_version="0.0.1"),
    )

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        return None

    for method, path in (
        ("POST", "/v1/messages/count_tokens"),
        ("POST", "/v1/some/future/route"),
        ("POST", "/v1/messages"),  # REWRITTEN, and the gate is not armed
    ):
        await gate(
            {
                "type": "http",
                "method": method,
                "path": path,
                "raw_path": path.encode(),
                "headers": [],
            },
            receive,
            send,
        )

    text = exporter.render().decode()
    for reason in ("route_gate_listed", "route_gate_unlisted", "route_gate_unarmed"):
        assert f'corp_llm_gateway_blocked_requests_total{{block_reason="{reason}"}} 1.0' in text
    assert f'gateway_failure{{component="{COMPONENT}"}} 1.0' in text


# ── the in-flight cap's series (route_gate/inflight.py) ──────────────────────


def test_the_limiter_reasons_are_enumerated() -> None:
    from corp_llm_gateway.route_gate.inflight import (
        LIMITER_BLOCK_REASONS,
        OVERSIZE_BLOCKED,
        ROUTE_GATE_BODY_TIMEOUT,
        ROUTE_GATE_CAPACITY,
    )

    assert BLOCK_REASONS["capacity"] == (ROUTE_GATE_CAPACITY, ROUTE_GATE_BODY_TIMEOUT)
    assert OVERSIZE_BLOCKED in BLOCK_REASONS["policy"]
    assert set(ALL_BLOCK_REASONS) >= LIMITER_BLOCK_REASONS


def test_prometheus_exports_the_inflight_gauge_and_the_cancelled_counter() -> None:
    exporter = _prom()

    exporter.set_inflight(3)
    exporter.set_inflight(2)
    exporter.record_cancelled()
    exporter.record_cancelled()

    text = exporter.render().decode()
    assert "# TYPE gateway_inflight_requests gauge" in text
    assert "gateway_inflight_requests 2.0" in text
    assert "# TYPE gateway_cancelled_requests_total counter" in text
    assert "gateway_cancelled_requests_total 2.0" in text


def test_prometheus_exports_the_draining_bytes_gauge() -> None:
    exporter = _prom()

    exporter.set_draining_bytes(4096)
    exporter.set_draining_bytes(1024)

    text = exporter.render().decode()
    assert "# TYPE gateway_draining_bytes gauge" in text
    assert "gateway_draining_bytes 1024.0" in text


def test_the_noop_exporter_ignores_the_inflight_series() -> None:
    exporter = NoopExporter()

    assert exporter.set_inflight(5) is None
    assert exporter.record_cancelled() is None
    assert exporter.set_draining_bytes(7) is None
    assert exporter.render() == b""


def test_an_exporter_written_before_the_inflight_series_still_instantiates() -> None:
    # Extension exporters implement the three original abstract methods only.
    class _Legacy(MetricsExporter):
        def record_block(self, block_reason: str) -> None:
            return None

        def record_failure(self, component: str) -> None:
            return None

        def observe_request_latency(self, seconds: float, *, status: str) -> None:
            return None

    legacy = _Legacy()

    assert legacy.set_inflight(1) is None
    assert legacy.record_cancelled() is None
    assert legacy.set_draining_bytes(1) is None


async def test_the_capacity_refusal_is_counted_on_the_real_series() -> None:
    import asyncio

    from corp_llm_gateway.route_gate import RouteGateMiddleware
    from corp_llm_gateway.route_gate.inflight import InflightLimiter

    exporter = _prom()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def holding(scope: object, receive: object, send: object) -> None:
        entered.set()
        await release.wait()

    gate = RouteGateMiddleware(
        holding,
        metrics=exporter,
        audit_logger=AuditLogger(ListSink(), gateway_version="0.0.1"),
        limiter=InflightLimiter(1, metrics=exporter),
    )
    gate.arm()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "headers": [],
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: dict) -> None:
        return None

    held = asyncio.create_task(gate(scope, receive, send))
    await asyncio.wait_for(entered.wait(), 2)
    await gate(scope, receive, send)
    held_text = exporter.render().decode()
    release.set()
    await held

    assert 'corp_llm_gateway_blocked_requests_total{block_reason="capacity"} 1.0' in held_text
    assert "gateway_inflight_requests 1.0" in held_text
    assert "gateway_inflight_requests 0.0" in exporter.render().decode()


async def test_a_restoration_failure_counts_on_the_shared_exporter_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No exporter and no reporter injected: the response restorer reports through the
    process-wide ``get_exporter()`` and the real ``record_failure``."""
    pytest.importorskip("prometheus_client")
    from corp_llm_gateway.route_gate.desanitize_middleware import (
        DesanitizeMiddleware,
        ResponseMappings,
    )
    from corp_llm_gateway.route_gate.inflight import _TICKET, RequestTicket
    from corp_llm_gateway.sanitizer.strategies import StrategyResult

    monkeypatch.setenv("CORP_METRICS_EXPORTER", "prometheus")
    monkeypatch.setattr(metrics_module, "_shared", None)

    def broken(payload: object, mapping: StrategyResult) -> object:
        raise ValueError("restore failed")

    async def app(scope: object, receive: object, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    mappings = ResponseMappings()
    ticket = RequestTicket("e" * 32)
    mappings.register(ticket, StrategyResult(pairs=(("a@b.c", "[EMAIL_1]"),)))
    token = _TICKET.set(ticket)
    try:
        await DesanitizeMiddleware(app, mappings, enabled=True, restore_json=broken)(
            {"type": "http"}, receive, send
        )
    finally:
        _TICKET.reset(token)

    exporter = get_exporter()
    assert isinstance(exporter, PrometheusExporter)
    assert 'gateway_failure{component="desanitize"} 1.0' in exporter.render().decode()
    assert sent[0]["status"] == 500
