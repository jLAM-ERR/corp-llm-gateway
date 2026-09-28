"""Plan 20260926 Task 0: what litellm's logging surfaces hold (hazards 1, 8, 9, 12, 16).

Driven through litellm's real proxy app (``_dispatch_fixtures``): the sink capture is a
``CustomLogger.async_log_success_event`` receiving the real ``StandardLoggingPayload``,
the same object Langfuse / S3 / SIEM callbacks read.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("litellm.proxy.proxy_server", reason="litellm proxy not installed")

import litellm
from litellm._logging import (
    verbose_logger,
    verbose_proxy_logger,
    verbose_router_logger,
)
from litellm.integrations import custom_guardrail
from litellm.integrations.custom_guardrail import (
    CustomGuardrail,
    _sync_guardrail_info_to_logging_obj,
)
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy import proxy_server
from litellm.proxy.utils import ProxyLogging
from litellm.types.guardrails import GuardrailEventHooks

from corp_llm_gateway.route_gate import Verdict, classify
from corp_llm_gateway.route_gate.desanitize_middleware import (
    DesanitizeMiddleware,
    ResponseMappings,
)
from tests.litellm_hook._dispatch_fixtures import (
    EMAIL,
    GUARDRAIL_NAME,
    ORIGINAL_MARK,
    PLACEHOLDER,
    PLACEHOLDER_MARK,
    ApplyGuardrailMigrated,
    Capture,
    CaptureNoChunkHook,
    DispatchHarness,
    OptionAPreCall,
    StubUpstream,
    UnsafeMigratedGuardrail,
    arm_problems,
    build_ours,
    until,
)

FLOWS = [(route, stream) for route in ("chat", "messages", "responses") for stream in (False, True)]
FLOW_IDS = [f"{route}-{'sse' if stream else 'unary'}" for route, stream in FLOWS]

# What our guardrail_information entry may carry: counts and labels, never content.
ALLOWED_RESPONSE = {"redaction_count": 1, "finding_label_counts": {"EMAIL": 1}}
_RECORDER = CustomGuardrail(guardrail_name=GUARDRAIL_NAME, event_hook=GuardrailEventHooks.post_call)


@pytest.fixture
def upstream() -> Iterator[StubUpstream]:
    stub = StubUpstream()
    yield stub
    stub.close()


class _GuardrailInfoWriter(CustomLogger):
    """A plain callback writing our entry the way Task 3's helper would, at post-call."""

    def __init__(self, *, sync: bool) -> None:
        super().__init__()
        self.sync = sync

    def _write(self, data: dict[str, Any]) -> None:
        # _RECORDER is never registered: litellm's writer, not a CustomGuardrail callback.
        _RECORDER.add_standard_logging_guardrail_information_to_request_data(
            guardrail_json_response=dict(ALLOWED_RESPONSE),
            request_data=data,
            guardrail_status="success",
            event_type=GuardrailEventHooks.post_call,
        )
        if self.sync:
            _sync_guardrail_info_to_logging_obj(data, data.get("litellm_logging_obj"))

    async def async_post_call_success_hook(
        self, data: dict[str, Any], user_api_key_dict: Any, response: Any
    ) -> None:
        self._write(data)

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: Any, response: Any, request_data: dict[str, Any]
    ) -> Any:
        async for chunk in response:
            yield chunk
        self._write(request_data)


def _payload_fields_holding(payload: dict[str, Any], needle: str) -> set[str]:
    return {key for key, value in payload.items() if needle in json.dumps(value, default=str)}


@pytest.mark.parametrize("sync", [True, False], ids=["synced", "unsynced"])
@pytest.mark.parametrize(("route", "stream"), FLOWS, ids=FLOW_IDS)
async def test_standard_logging_payload_is_content_free(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, route: str, stream: bool, sync: bool
) -> None:
    """Hazards 1 and 16, and the Option 0 proof for Task 3.

    Hazard 1: no original in the payload a sink receives, metadata included, on all six
    flows. ``/v1/responses`` holds only since our pre-call refreshes litellm's logging
    snapshot (hazard 17, ``test_hazard17_logging_snapshot.py``).

    Hazard 16: a plain ``CustomLogger`` writes an allow-listed entry with litellm's
    ``add_standard_logging_guardrail_information_to_request_data`` (from a never-registered
    ``CustomGuardrail`` used only as the writer); the payload carries exactly that entry.
    On 1.101.0 it lands with and without ``_sync_guardrail_info_to_logging_obj`` on all six
    flows, so the sync is defence in depth here, not the fix the plan expected. The OTEL
    guardrail span is emitted from this same entry object (custom_guardrail.py:1197-1217),
    so the entry being content-free is the span being content-free.
    """
    ours, _ = build_ours()
    sink = Capture("sink")
    harness = DispatchHarness(monkeypatch, upstream, [ours, _GuardrailInfoWriter(sync=sync), sink])

    exchange = await harness.send(route, stream=stream)
    await until(lambda: len(sink.seen.logged) >= 2)

    assert exchange.status == 200
    assert len(sink.seen.logged) == 2, sink.seen.logged
    payload = json.loads(sink.seen.logged[0])
    response_obj = sink.seen.logged[1]
    # TODAY_BEFORE_TASK1: on /v1/responses the payload's `messages` held the ORIGINAL
    # input (leaking == {"messages"}): litellm's logging object snapshots the request
    # before the pre-call hook rewrites it, and nothing refreshed it on that route.
    assert _payload_fields_holding(payload, ORIGINAL_MARK) == set()
    assert ORIGINAL_MARK not in response_obj
    ours_entries = [
        entry
        for entry in payload["guardrail_information"] or ()
        if entry["guardrail_name"] == GUARDRAIL_NAME
    ]
    assert len(ours_entries) == 1
    assert ours_entries[0]["guardrail_response"] == ALLOWED_RESPONSE
    assert ours_entries[0]["guardrail_status"] == "success"


RESPONSES_LIST_INPUTS: dict[str, list[dict[str, Any]]] = {
    "role-content": [{"role": "user", "content": f"write to {EMAIL}"}],
    "typed-items": [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": f"write to {EMAIL}"}],
        }
    ],
}


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("shape", sorted(RESPONSES_LIST_INPUTS))
async def test_responses_list_input_reaches_the_success_log_payload(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, shape: str, stream: bool
) -> None:
    """Hazard 17, list-shaped ``input`` (the test above sends ``input: str`` only).

    litellm's logging object copies only the outer list (``copy.copy``,
    litellm_logging.py:479), so its items are the request's own dicts: an in-place edit of
    those dicts WOULD reach a list input (never an ``input: str``, which the logging object
    wraps in a dict of its own). Our pre-call does not edit in place; it stores a new list
    of new items (``_store_request_items``), so the fix replaces the logging object's
    ``messages`` (``_refresh_logging_snapshot``).
    """
    ours, _ = build_ours()
    sink = Capture("sink")
    harness = DispatchHarness(monkeypatch, upstream, [ours, sink])
    sent_input = RESPONSES_LIST_INPUTS[shape]

    exchange = await harness.send("responses", stream=stream, extra={"input": sent_input})
    await until(lambda: len(sink.seen.logged) >= 2)

    assert exchange.status == 200
    assert len(exchange.provider_bodies) == 1
    assert ORIGINAL_MARK not in exchange.provider_bodies[0]
    payload = json.loads(sink.seen.logged[0])
    # TODAY_BEFORE_TASK1: the payload's `messages` was the ORIGINAL input, item for item
    # (holding == {"messages"}, payload["messages"] == sent_input).
    assert _payload_fields_holding(payload, ORIGINAL_MARK) == set()
    assert payload["messages"] != sent_input
    assert PLACEHOLDER in json.dumps(payload["messages"])
    assert ORIGINAL_MARK not in sink.seen.logged[1]


async def test_decorated_apply_guardrail_logs_exception_text() -> None:
    """Hazard 8: any ``apply_guardrail`` is wrapped by ``log_guardrail_information`` at class
    creation (custom_guardrail.py:155-160), and a raising one has ``str(exc)`` written into
    ``standard_logging_guardrail_information`` (``_process_error`` :1296, then :1140-1141) —
    a content-to-log path. Why content-free logging comes before any ``guardrail_information``
    use, and one more reason our class never defines ``apply_guardrail``.
    """

    class _Raising(CustomGuardrail):
        async def apply_guardrail(
            self,
            inputs: Any,
            request_data: dict[str, Any],
            input_type: Any,
            logging_obj: Any = None,
        ) -> Any:
            raise ValueError(f"cannot scan {EMAIL}")

    assert custom_guardrail.LOGS_GUARDRAIL_INFORMATION_MARKER in vars(_Raising.apply_guardrail)
    assert custom_guardrail.LOGS_GUARDRAIL_INFORMATION_MARKER in vars(
        ApplyGuardrailMigrated.apply_guardrail
    )
    data: dict[str, Any] = {"metadata": {}}
    guardrail = _Raising(guardrail_name=GUARDRAIL_NAME, event_hook=GuardrailEventHooks.pre_call)

    with pytest.raises(ValueError):
        await guardrail.apply_guardrail(
            inputs={"texts": ["hello"]}, request_data=data, input_type="request"
        )

    (entry,) = data["metadata"]["standard_logging_guardrail_information"]
    assert EMAIL in str(entry["guardrail_response"])


@pytest.mark.parametrize(
    "route", ["/guardrails/apply_guardrail", "/guardrails/test_custom_code", "/policies"]
)
def test_native_guardrail_endpoints_stay_refused(route: str) -> None:
    """Hazard 9: litellm's endpoint hands the request text to the logging object
    (guardrail_endpoints.py:2240-2246); it and the policy mutation surface stay REFUSE."""
    assert classify("POST", route).verdict is Verdict.REFUSE


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def holding(self, needle: str) -> set[tuple[str, int]]:
        return {
            (record.pathname.rsplit("/", 1)[-1], record.lineno)
            for record in self.records
            if needle in record.getMessage()
        }


@pytest.fixture
def litellm_debug(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Records]:
    """What ``LITELLM_LOG=DEBUG`` does in ``initialize()`` (proxy_server.py:8214-8223)."""
    handler = _Records()
    loggers = (verbose_proxy_logger, verbose_logger, verbose_router_logger)
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        for lg, level in zip(loggers, levels, strict=True):
            lg.removeHandler(handler)
            lg.setLevel(level)


# litellm DEBUG lines that print the whole request before the pre-call hook rewrites it.
_REQUEST_LOG_SITES = {
    ("common_request_processing.py", 2209),
    ("litellm_pre_call_utils.py", 2438),
}
# The streaming chunk log, after the iterator chain (common_request_processing.py:3630).
_CHUNK_LOG_SITE = ("common_request_processing.py", 3630)


@pytest.mark.parametrize(("route", "stream"), FLOWS, ids=FLOW_IDS)
async def test_debug_chunk_log_and_per_chunk_hooks(
    monkeypatch: pytest.MonkeyPatch,
    upstream: StubUpstream,
    litellm_debug: _Records,
    route: str,
    stream: bool,
) -> None:
    """Hazard 12. With litellm at DEBUG:

    - the chunk log (common_request_processing.py:3630) prints no original: the
      reversal runs outside litellm. With the callback reversal (before Task 3) it ran
      after our iterator hook and printed restored originals on ``/v1/messages`` SSE;
    - two request logs print the ORIGINAL request before any pre-call hook runs, on every
      flow, whatever reverses the response. DEBUG cannot be made safe: the arm step
      refuses it (``arm_problems``);
    - per-chunk hooks receive what litellm streams: placeholders only.
    """
    ours, _ = build_ours()
    capture = Capture("per_chunk")
    harness = DispatchHarness(monkeypatch, upstream, [ours, capture])

    served = await harness.send(route, stream=stream)

    assert ORIGINAL_MARK in served.text
    today = litellm_debug.holding(ORIGINAL_MARK)
    assert today == _REQUEST_LOG_SITES
    if (route, stream) == ("messages", True):
        # Not vacuous: the chunk log ran, with placeholders.
        assert _CHUNK_LOG_SITE in litellm_debug.holding(PLACEHOLDER_MARK)
    assert all(ORIGINAL_MARK not in value for value in capture.seen.per_chunk)
    if route == "chat" and stream:
        assert any(PLACEHOLDER_MARK in value for value in capture.seen.per_chunk)

    litellm_debug.records.clear()
    mappings = ResponseMappings()
    engine, _ = build_ours()
    option_a = DispatchHarness(
        monkeypatch,
        upstream,
        [OptionAPreCall(engine, mappings), Capture("per_chunk")],
        wrap=lambda app: DesanitizeMiddleware(app, mappings),
    )

    await option_a.send(route, stream=stream)

    assert litellm_debug.holding(ORIGINAL_MARK) == _REQUEST_LOG_SITES
    assert "litellm_debug_logging" in arm_problems([ours], proxy_logger=verbose_proxy_logger)


def test_a_custom_guardrail_turns_per_chunk_hooks_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hazard 12, the switch: per-chunk dispatch runs for every callback once any
    ``CustomGuardrail`` is registered (proxy/utils.py:2234-2241), even when no callback
    overrides the per-chunk hook. Today's plain callback keeps it off."""
    ours, _ = build_ours()
    monkeypatch.setattr(litellm, "callbacks", [ours, CaptureNoChunkHook("c")])
    monkeypatch.setattr(ProxyLogging, "_callback_capabilities_cache", {})
    assert proxy_server.proxy_logging_obj.needs_per_chunk_streaming_hook() is False

    monkeypatch.setattr(
        litellm, "callbacks", [UnsafeMigratedGuardrail(ours), CaptureNoChunkHook("c")]
    )
    assert proxy_server.proxy_logging_obj.needs_per_chunk_streaming_hook() is True


def test_debug_and_set_verbose_are_refused_at_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hazard 12, the refusal prototype: ``LITELLM_LOG=DEBUG`` (proxy logger at DEBUG) and
    ``litellm.set_verbose`` each block arming; INFO arms."""
    ours, _ = build_ours()
    level = verbose_proxy_logger.level
    try:
        verbose_proxy_logger.setLevel(logging.INFO)
        assert arm_problems([ours], proxy_logger=verbose_proxy_logger) == []

        verbose_proxy_logger.setLevel(logging.DEBUG)
        assert arm_problems([ours], proxy_logger=verbose_proxy_logger) == ["litellm_debug_logging"]
    finally:
        verbose_proxy_logger.setLevel(level)

    monkeypatch.setattr(litellm, "set_verbose", True)
    assert arm_problems([ours], set_verbose=litellm.set_verbose) == ["litellm_set_verbose"]
