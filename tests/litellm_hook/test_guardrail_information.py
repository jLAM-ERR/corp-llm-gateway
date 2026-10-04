"""Plan 20260926 Task 5: our content-free entry in litellm's ``guardrail_information``.

Our pre-call writes one ``standard_logging_guardrail_information`` entry with litellm's
own writer (``add_standard_logging_guardrail_information_to_request_data``, from a
``CustomGuardrail`` that is never registered) and syncs it into the logging object the
``StandardLoggingPayload`` is built from (hazard 16). The entry carries our name,
mode, status, timing and three facts: ``block_reason``, ``redaction_count`` and
``finding_label_counts`` — never content, never exception text (hazard 8).
"""

from __future__ import annotations

import json
import logging
import sys
import time
import types
from collections.abc import Iterator
from typing import Any, get_args

import pytest

pytest.importorskip("litellm.proxy.proxy_server", reason="litellm proxy not installed")

import litellm
from litellm._logging import verbose_logger, verbose_proxy_logger, verbose_router_logger
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.integrations.custom_logger import CustomLogger
from litellm.integrations.otel.model.payloads import GuardrailSpanData
from litellm.litellm_core_utils.core_helpers import get_metadata_variable_name_from_kwargs
from litellm.proxy import proxy_server
from litellm.types.utils import GuardrailStatus

from corp_llm_gateway import litellm_hook
from corp_llm_gateway.audit import (
    GUARDRAIL_INFORMATION_KEYS,
    GUARDRAIL_STATUSES,
    assert_guardrail_information_allowed,
)
from corp_llm_gateway.litellm_hook import (
    GUARDRAIL_NAME,
    GUARDRAIL_STATUS_BY_BLOCK_REASON,
    CorpLlmGuardrail,
    GuardrailHttpException,
    _sync_guardrail_information,
)
from corp_llm_gateway.metrics import BLOCK_REASONS
from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard
from corp_llm_gateway.sanitizer.orchestrator import OVERSIZE_DELIVERED_REASON
from tests.hook_fixtures import _build_guardrail, _build_guardrail_oversize
from tests.litellm_hook import _dispatch_fixtures
from tests.litellm_hook._dispatch_fixtures import (
    EMAIL,
    ORIGINAL_MARK,
    PLACEHOLDER,
    PLACEHOLDER_MARK,
    DispatchHarness,
    StubUpstream,
    build_ours,
    serialize,
    until,
)
from tests.litellm_hook.test_hazard18_corp_token_snapshot import token_sites

ROUTES = ["chat", "messages", "responses"]
# How a request that passed our pre-call ends (as in test_hazard18_corp_token_snapshot).
OUTCOMES: dict[str, tuple[int | None, dict[str, Any]]] = {
    "success": (None, {}),
    "provider-error": (400, {}),
    "pre-provider-failure": (None, {"tools": "nope"}),
}
KEY = "standard_logging_guardrail_information"
CANARY = "DLP-CANARY-RAW-T5"
STAGE0_ENV = (
    f"ADMIN_EMAIL={EMAIL}\n"
    "DATABASE_URL=postgres://admin:hunter2@db.corp.lan:5432/prod\n"
    "SECRET_KEY=supersecretvalue-abc123\n"
    "DEBUG=False\n"
    "REDIS_URL=redis://cache.corp.lan:6379/0\n"
    "ALLOWED_HOSTS=*.corp.lan\n"
)
# No block_reason on a pass: litellm's writer drops a None inside guardrail_response.
PASS_FACTS = {"redaction_count": 1, "finding_label_counts": {"EMAIL": 1}}
BLOCKS: dict[str, tuple[str, dict[str, Any]]] = {
    "stage0": (
        STAGE0_ENV,
        {"block_reason": "config:env", "redaction_count": 0, "finding_label_counts": {}},
    ),
    "stage5": (
        f"write to {EMAIL} {CANARY}",
        {"block_reason": "dlp:canary", "redaction_count": 1, "finding_label_counts": {"EMAIL": 1}},
    ),
}


class PayloadCapture(CustomLogger):
    """Registered after ours: every payload litellm builds, and every failure hook's
    request, serialised on arrival."""

    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[tuple[str, dict[str, Any]]] = []
        self.failure_hooks: list[dict[str, Any]] = []

    def _payload(self, kind: str, kwargs: dict[str, Any]) -> None:
        self.payloads.append((kind, json.loads(serialize(kwargs.get("standard_logging_object")))))

    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self._payload("success", kwargs)

    async def async_log_failure_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self._payload("failure", kwargs)

    async def async_post_call_failure_hook(
        self,
        request_data: dict[str, Any],
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: str | None = None,
    ) -> None:
        self.failure_hooks.append(json.loads(serialize(request_data)))


def _bucket_entries(request_data: dict[str, Any]) -> list[Any]:
    bucket = request_data.get(get_metadata_variable_name_from_kwargs(request_data)) or {}
    return list(bucket.get(KEY) or ())


def _assert_our_entry(
    entry: dict[str, Any], *, status: str, facts: dict[str, Any], window: tuple[float, float]
) -> None:
    assert set(entry) == GUARDRAIL_INFORMATION_KEYS
    timing = {key: entry[key] for key in ("start_time", "end_time", "duration")}
    assert {key: value for key, value in entry.items() if key not in timing} == {
        "guardrail_name": GUARDRAIL_NAME,
        "guardrail_provider": None,
        "guardrail_mode": "pre_call",
        "guardrail_response": facts,
        "guardrail_status": status,
        "masked_entity_count": None,
    }
    assert window[0] <= timing["start_time"] <= timing["end_time"] <= window[1]
    assert timing["duration"] == pytest.approx(timing["end_time"] - timing["start_time"])
    assert_guardrail_information_allowed(entry)
    # Content-free: neither an original nor a placeholder, which is content-derived too.
    assert token_sites(entry, ORIGINAL_MARK) == []
    assert token_sites(entry, PLACEHOLDER_MARK) == []


@pytest.fixture
def upstream() -> Iterator[StubUpstream]:
    stub = StubUpstream()
    yield stub
    stub.close()


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("outcome", sorted(OUTCOMES))
@pytest.mark.parametrize("route", ROUTES)
async def test_the_payload_carries_exactly_our_entry(
    monkeypatch: pytest.MonkeyPatch, route: str, outcome: str, stream: bool
) -> None:
    """A request our pre-call passed: each ``StandardLoggingPayload`` — success, or
    failure for a provider 400 or a failure before the provider's own pre-call — carries
    exactly our entry, once, status ``success``, and no original anywhere."""
    error_status, extra = OUTCOMES[outcome]
    stub = StubUpstream(error_status=error_status)
    try:
        ours, _ = build_ours()
        capture = PayloadCapture()
        harness = DispatchHarness(monkeypatch, stub, [ours, capture])
        before = time.time()
        exchange = await harness.send(route, stream=stream, extra=extra)
        await until(lambda: len(capture.payloads) >= 1)
        after = time.time()
    finally:
        stub.close()

    assert (exchange.status == 200) is (outcome == "success")
    assert {kind for kind, _ in capture.payloads} == {
        "success" if outcome == "success" else "failure"
    }
    # One payload per attempt: the router retries a failure before the provider call.
    for _, payload in capture.payloads:
        (entry,) = payload["guardrail_information"]
        _assert_our_entry(entry, status="success", facts=PASS_FACTS, window=(before, after))
        assert payload["status_fields"]["guardrail_status"] == "success"
        assert token_sites(payload, ORIGINAL_MARK) == []
    for body in exchange.provider_bodies:
        assert GUARDRAIL_NAME not in body
        assert KEY not in body
    # Nor does it reach the client: no applied-guardrails header names us.
    assert GUARDRAIL_NAME not in str(exchange.headers)


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("block", sorted(BLOCKS))
@pytest.mark.parametrize("route", ROUTES)
async def test_a_refusal_writes_its_entry_where_litellm_reads_it(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, route: str, block: str, stream: bool
) -> None:
    """A Stage 0 / Stage 5 refusal: litellm builds no payload for it (hazard 17b), so our
    entry reaches only what litellm hands every failure hook (and the OTEL span, below).
    Status ``guardrail_intervened``, the block_reason ours."""
    content, facts = BLOCKS[block]
    ours, _ = build_ours()
    ours._dlp_guard = DlpEgressGuard(canary_patterns=[CANARY], secret_rescan=False)
    capture = PayloadCapture()
    harness = DispatchHarness(monkeypatch, upstream, [ours, capture])
    before = time.time()

    exchange = await harness.send(route, stream=stream, content=content)
    after = time.time()

    assert exchange.status == 422
    assert exchange.provider_bodies == []
    assert capture.payloads == []
    assert GUARDRAIL_NAME not in str(exchange.headers)
    (request_data,) = capture.failure_hooks
    (entry,) = _bucket_entries(request_data)
    _assert_our_entry(entry, status="guardrail_intervened", facts=facts, window=(before, after))


@pytest.fixture
def otel_spans(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """litellm's writer emits the OTEL guardrail span from the entry itself
    (``emit_guardrail_span``, custom_guardrail.py). ``opentelemetry`` is not installed:
    a stand-in module receives what the real one would."""
    spans: list[dict[str, Any]] = []
    module = types.ModuleType("litellm.integrations.otel.logger")
    module.emit_guardrail_span = lambda entry: spans.append(dict(entry))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm.integrations.otel.logger", module)
    return spans


@pytest.mark.parametrize("block", ["pass", *sorted(BLOCKS)])
@pytest.mark.parametrize("route", ROUTES)
async def test_the_otel_span_is_built_from_our_entry_only(
    monkeypatch: pytest.MonkeyPatch,
    upstream: StubUpstream,
    otel_spans: list[dict[str, Any]],
    route: str,
    block: str,
) -> None:
    """A pass and both refusals: one span per request, built from our entry only."""
    content, facts = BLOCKS.get(block, (None, PASS_FACTS))
    status = "success" if block == "pass" else "guardrail_intervened"
    ours, _ = build_ours()
    ours._dlp_guard = DlpEgressGuard(canary_patterns=[CANARY], secret_rescan=False)
    harness = DispatchHarness(monkeypatch, upstream, [ours])
    before = time.time()

    await harness.send(route, stream=False, content=content)

    (entry,) = otel_spans
    _assert_our_entry(entry, status=status, facts=facts, window=(before, time.time()))
    span = GuardrailSpanData.from_logging_entry(entry)  # type: ignore[arg-type]
    assert span.guardrail_name == GUARDRAIL_NAME
    assert span.status == status
    assert ORIGINAL_MARK not in repr(span)
    assert PLACEHOLDER_MARK not in repr(span)
    # A refusal marks the span ERROR, with no message: we set no guardrail_action.
    assert (span.error is not None) is (block != "pass")
    assert span.error is None or span.error.message is None


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[tuple[str, int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append((record.pathname.rsplit("/", 1)[-1], record.lineno, record.getMessage()))


@pytest.fixture
def debug_lines() -> Iterator[_Lines]:
    handler = _Lines()
    loggers = [
        verbose_logger,
        verbose_proxy_logger,
        verbose_router_logger,
        logging.getLogger("corp_llm_gateway.litellm_hook"),
    ]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    yield handler
    for lg, level in zip(loggers, levels, strict=True):
        lg.removeHandler(handler)
        lg.setLevel(level)


# litellm's DEBUG lines that print the request before any pre-call hook
# (test_logging_surfaces._REQUEST_LOG_SITES): the only ones allowed to hold an original.
_REQUEST_LOG_SITES = {
    ("common_request_processing.py", 2209),
    ("litellm_pre_call_utils.py", 2438),
}


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("route", ROUTES)
async def test_debug_lines_carrying_our_entry_are_content_free(
    monkeypatch: pytest.MonkeyPatch,
    upstream: StubUpstream,
    debug_lines: _Lines,
    route: str,
    stream: bool,
) -> None:
    ours, _ = build_ours()
    harness = DispatchHarness(monkeypatch, upstream, [ours, PayloadCapture()])

    exchange = await harness.send(route, stream=stream)

    assert exchange.status == 200
    holding_original = {(name, line) for name, line, text in debug_lines.lines if EMAIL in text}
    assert holding_original == _REQUEST_LOG_SITES
    ours_lines = [text for _, _, text in debug_lines.lines if GUARDRAIL_NAME in text]
    # Not vacuous: litellm's request log after the pre-call prints the entry.
    assert ours_lines
    assert all(ORIGINAL_MARK not in text for text in ours_lines)
    assert all(
        "litellm_guardrail_information_failed" not in text for _, _, text in debug_lines.lines
    )


async def test_the_writer_is_never_a_registered_callback(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream
) -> None:
    """A registered ``CustomGuardrail`` switches per-chunk dispatch on for every callback
    (hazard 12) and can be pipeline-skipped or name-substituted (14, 15b); the writer is
    one only to lend litellm's writer its name."""
    ours, _ = build_ours()
    harness = DispatchHarness(monkeypatch, upstream, [ours])

    await harness.send("chat", stream=True)

    assert isinstance(litellm_hook._guardrail_information_writer(), CustomGuardrail)
    assert not any(isinstance(cb, CustomGuardrail) for cb in litellm.callbacks)
    assert proxy_server.proxy_logging_obj.needs_per_chunk_streaming_hook() is False
    assert "apply_guardrail" not in vars(CorpLlmGuardrail)


def test_the_harness_fixtures_reserve_our_name() -> None:
    """The migrated fixtures (hazards 14, 15b) and the arm prototype use this name."""
    assert _dispatch_fixtures.GUARDRAIL_NAME == GUARDRAIL_NAME


# ── the status table ─────────────────────────────────────────────────────────


# By exclusion: listing the pre-call sites here would restate the table's own source.
PRE_CALL_BLOCK_REASONS = {
    reason
    for site, reasons in BLOCK_REASONS.items()
    if site not in ("route_gate", "capacity")
    for reason in reasons
}


def test_the_status_table_covers_every_pre_call_reason_and_nothing_else() -> None:
    assert set(GUARDRAIL_STATUS_BY_BLOCK_REASON) == {
        None,
        OVERSIZE_DELIVERED_REASON,
        *PRE_CALL_BLOCK_REASONS,
    }
    assert set(GUARDRAIL_STATUS_BY_BLOCK_REASON.values()) <= set(get_args(GuardrailStatus))
    assert set(get_args(GuardrailStatus)) == GUARDRAIL_STATUSES


@pytest.mark.parametrize(
    ("block_reason", "status"),
    [
        (None, "success"),
        ("oversize:delivered", "guardrail_flagged"),
        ("config:env", "guardrail_intervened"),
        ("config:kube", "guardrail_intervened"),
        ("config:nginx", "guardrail_intervened"),
        ("config:ini", "guardrail_intervened"),
        ("log:dump", "guardrail_intervened"),
        ("dlp:canary", "guardrail_intervened"),
        ("dlp:secret_leak", "guardrail_intervened"),
        ("oversize:blocked", "guardrail_intervened"),
        ("request:ambiguous_shape", "guardrail_intervened"),
        ("provider:not_allowed", "guardrail_intervened"),
    ],
)
def test_litellms_status_is_derived_from_our_block_reason(
    block_reason: str | None, status: str
) -> None:
    assert GUARDRAIL_STATUS_BY_BLOCK_REASON[block_reason] == status


@pytest.mark.parametrize("reason", sorted(BLOCK_REASONS["route_gate"] + BLOCK_REASONS["capacity"]))
def test_reasons_refused_before_litellm_have_no_status(reason: str) -> None:
    assert reason not in GUARDRAIL_STATUS_BY_BLOCK_REASON


# ── the helper, directly ─────────────────────────────────────────────────────


def _data(content: str = f"write to {EMAIL}") -> dict[str, Any]:
    return {
        "model": "claude",
        "messages": [{"role": "user", "content": content}],
        "headers": {"X-Corp-Auth": "tok-1"},
    }


async def test_an_oversize_delivered_request_is_flagged() -> None:
    guardrail, _ = _build_guardrail_oversize(
        threshold=64, policy="deliver-flag", deliver_teams=frozenset({"t1"})
    )
    data = _data("the quick brown fox jumps over the lazy dog and keeps running along here")
    before = time.time()

    await guardrail.pre_call(data)

    (entry,) = _bucket_entries(data)
    facts = {
        "block_reason": "oversize:delivered",
        "redaction_count": 0,
        "finding_label_counts": {},
    }
    _assert_our_entry(entry, status="guardrail_flagged", facts=facts, window=(before, time.time()))


async def test_a_policy_block_writes_its_entry_before_the_refusal() -> None:
    guardrail, _ = _build_guardrail([(EMAIL, PLACEHOLDER)])
    data = _data() | {"input": "both"}

    with pytest.raises(GuardrailHttpException) as raised:
        await guardrail.pre_call(data)

    assert raised.value.error_code == "E_POLICY_BLOCKED"
    (entry,) = _bucket_entries(data)
    assert entry["guardrail_status"] == "guardrail_intervened"
    assert entry["guardrail_response"]["block_reason"] == "request:ambiguous_shape"


class _Metrics:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def record_failure(self, component: str) -> None:
        self.failures.append(component)

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: None


async def test_an_unknown_block_reason_writes_nothing_and_never_fails_the_request(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    guardrail, _ = _build_guardrail([(EMAIL, PLACEHOLDER)])
    guardrail._metrics = _Metrics()  # type: ignore[assignment]
    monkeypatch.setattr(litellm_hook, "GUARDRAIL_STATUS_BY_BLOCK_REASON", {})
    data = _data()

    with caplog.at_level(logging.ERROR, logger="corp_llm_gateway.litellm_hook"):
        out = await guardrail.pre_call(data)

    assert PLACEHOLDER in json.dumps(out["messages"])
    assert _bucket_entries(data) == []
    assert guardrail._metrics.failures == ["audit"]  # type: ignore[attr-defined]
    assert "litellm_guardrail_information_failed" in caplog.text
    assert "error=UnknownBlockReasonError" in caplog.text


async def test_a_raising_writer_is_logged_by_type_and_leaves_no_entry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Hazard 8's shape, from our side: an exception carrying content never becomes
    text in a log line or an entry."""
    guardrail, _ = _build_guardrail([(EMAIL, PLACEHOLDER)])
    guardrail._metrics = _Metrics()  # type: ignore[assignment]

    class _Raising:
        def add_standard_logging_guardrail_information_to_request_data(self, **_: Any) -> None:
            raise ValueError(f"cannot record {EMAIL}")

    monkeypatch.setattr(litellm_hook, "_WRITER", _Raising())
    data = _data()

    with caplog.at_level(logging.DEBUG, logger="corp_llm_gateway.litellm_hook"):
        await guardrail.pre_call(data)

    assert _bucket_entries(data) == []
    assert "error=ValueError" in caplog.text
    assert ORIGINAL_MARK not in caplog.text
    assert guardrail._metrics.failures == ["audit"]  # type: ignore[attr-defined]


OTHER_GUARDRAIL = {"guardrail_name": "someone-else", "guardrail_status": "success"}


@pytest.mark.parametrize("earlier", [[], [OTHER_GUARDRAIL]], ids=["alone", "after-another"])
@pytest.mark.parametrize("shape", ["widened", "nothing-added"])
async def test_an_entry_litellm_shaped_differently_is_removed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    shape: str,
    earlier: list[dict[str, Any]],
) -> None:
    """A litellm bump whose writer adds a key to the entry (or quotes something into it),
    or writes nothing where we read: what this call added is taken back out, another
    guardrail's entry stays, and nothing reaches the logging object."""
    guardrail, _ = _build_guardrail([(EMAIL, PLACEHOLDER)])
    guardrail._metrics = _Metrics()  # type: ignore[assignment]
    real = litellm_hook._guardrail_information_writer()

    class _Writer:
        def add_standard_logging_guardrail_information_to_request_data(self, **kw: Any) -> None:
            if shape == "nothing-added":
                return
            real.add_standard_logging_guardrail_information_to_request_data(**kw)
            request_data = kw["request_data"]
            bucket = request_data[get_metadata_variable_name_from_kwargs(request_data)]
            bucket[KEY][-1]["guardrail_request"] = "quoted"

    logging_obj = types.SimpleNamespace(litellm_params={"metadata": {}}, model_call_details={})
    monkeypatch.setattr(litellm_hook, "_WRITER", _Writer())
    data = _data() | {
        "litellm_logging_obj": logging_obj,
        "litellm_metadata": {KEY: [dict(entry) for entry in earlier]},
    }

    with caplog.at_level(logging.ERROR, logger="corp_llm_gateway.litellm_hook"):
        await guardrail.pre_call(data)

    assert _bucket_entries(data) == earlier
    assert KEY not in logging_obj.litellm_params["metadata"]
    assert "error=GuardrailInformationShapeError" in caplog.text
    assert guardrail._metrics.failures == ["audit"]  # type: ignore[attr-defined]


async def test_without_litellm_nothing_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    guardrail, _ = _build_guardrail([(EMAIL, PLACEHOLDER)])
    guardrail._metrics = _Metrics()  # type: ignore[assignment]
    monkeypatch.setattr(litellm_hook, "_guardrail_information_writer", lambda: None)
    data = _data()

    await guardrail.pre_call(data)

    assert _bucket_entries(data) == []
    assert guardrail._metrics.failures == []  # type: ignore[attr-defined]


# ── the logging-object sync (hazard 16, defence in depth) ────────────────────


def _entry() -> dict[str, Any]:
    return {
        "guardrail_name": GUARDRAIL_NAME,
        "guardrail_provider": None,
        "guardrail_mode": "pre_call",
        "guardrail_response": dict(PASS_FACTS),
        "guardrail_status": "success",
        "start_time": 1.0,
        "end_time": 2.0,
        "duration": 1.0,
        "masked_entity_count": None,
    }


def test_the_sync_shares_the_request_list_or_copies_into_one_of_its_own() -> None:
    other = {"guardrail_name": "someone-else", "guardrail_status": "success"}
    request_entries: list[Any] = [_entry()]
    params: dict[str, Any] = {"metadata": {KEY: [other]}}
    details: dict[str, Any] = {"litellm_params": {"metadata": {}}}
    logging_obj = types.SimpleNamespace(litellm_params=params, model_call_details=details)

    _sync_guardrail_information(logging_obj, _entry(), request_entries)
    _sync_guardrail_information(logging_obj, _entry(), request_entries)

    assert params["metadata"][KEY] == [other, _entry()]
    assert details["litellm_params"]["metadata"][KEY] is request_entries
    assert request_entries == [_entry()]


def test_an_entry_written_after_ours_still_reaches_a_shared_list() -> None:
    """litellm itself carries the request's list into the payload where the logging
    object has none; a list of our own there would hide a later callback's entry."""
    request_entries: list[Any] = [_entry()]
    params: dict[str, Any] = {"metadata": {}}

    _sync_guardrail_information(
        types.SimpleNamespace(litellm_params=params), _entry(), request_entries
    )
    later = {"guardrail_name": "someone-else", "guardrail_status": "success"}
    request_entries.append(later)

    assert params["metadata"][KEY] == [_entry(), later]


def test_the_copy_holds_the_allow_listed_keys_only() -> None:
    params: dict[str, Any] = {"metadata": {KEY: []}}
    entry = _entry() | {"extra": "never copied"}

    _sync_guardrail_information(types.SimpleNamespace(litellm_params=params), entry, [entry])

    (copied,) = params["metadata"][KEY]
    assert set(copied) == GUARDRAIL_INFORMATION_KEYS


@pytest.mark.parametrize(
    "logging_obj",
    [
        None,
        object(),
        types.SimpleNamespace(litellm_params="x", model_call_details=None),
        types.SimpleNamespace(litellm_params={}, model_call_details={"litellm_params": 3}),
    ],
    ids=["none", "bare-object", "odd-params", "odd-details"],
)
def test_the_sync_tolerates_any_logging_object(logging_obj: Any) -> None:
    _sync_guardrail_information(logging_obj, _entry(), [_entry()])


@pytest.mark.parametrize("params", [{}, {"metadata": None}], ids=["absent", "none"])
def test_the_sync_never_creates_the_metadata_an_auth_bridge_scrubbed(
    params: dict[str, Any],
) -> None:
    before = dict(params)

    _sync_guardrail_information(types.SimpleNamespace(litellm_params=params), _entry(), [])

    assert params == before


def test_the_sync_leaves_a_foreign_value_alone() -> None:
    params: dict[str, Any] = {"metadata": {KEY: "not a list"}}

    _sync_guardrail_information(types.SimpleNamespace(litellm_params=params), _entry(), [])

    assert params["metadata"][KEY] == "not a list"


@pytest.mark.parametrize(("route", "stream"), [("chat", False), ("responses", True)])
async def test_the_entry_lands_without_the_sync_too(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, route: str, stream: bool
) -> None:
    """Hazard 16 on 1.101.0 (not reproduced): the request-metadata write alone reaches
    the payload; the sync is defence in depth. If a bump breaks this, the synced path
    above still holds."""
    monkeypatch.setattr(litellm_hook, "_sync_guardrail_information", lambda *_: None)
    ours, _ = build_ours()
    capture = PayloadCapture()
    harness = DispatchHarness(monkeypatch, upstream, [ours, capture])

    await harness.send(route, stream=stream)
    await until(lambda: len(capture.payloads) >= 1)

    ((_, payload),) = capture.payloads
    (entry,) = payload["guardrail_information"]
    assert entry["guardrail_name"] == GUARDRAIL_NAME
