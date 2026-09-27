"""A cancel reaches only the request it belongs to.

litellm 1.101.0 takes ``litellm_call_id`` from the client's ``x-litellm-call-id``
header when present (``proxy/common_request_processing.py``), so two HTTP
requests can carry the same call id; and a litellm event can run outside the
request's context while its cancel record is still being written."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from corp_llm_gateway.metrics import NoopExporter
from corp_llm_gateway.route_gate.inflight import _TICKET, RequestTicket
from tests.litellm_hook.test_on_request_cancelled import EMAIL
from tests.test_litellm_hook import _build_guardrail, _data_with_token


async def _pre_call_in(guardrail: Any, ticket: RequestTicket, call_id: str) -> None:
    token = _TICKET.set(ticket)
    try:
        data = _data_with_token("tok-1", content=f"write to {EMAIL}")
        data["litellm_call_id"] = call_id
        await asyncio.create_task(guardrail.pre_call(data))
    finally:
        _TICKET.reset(token)


async def test_a_disconnect_never_ends_another_request_that_shares_its_call_id() -> None:
    guardrail, sink = _build_guardrail([(EMAIL, "[EMAIL_1]")])
    gone, live = RequestTicket("a" * 32), RequestTicket("b" * 32)
    await _pre_call_in(guardrail, gone, "client-chosen")
    await _pre_call_in(guardrail, live, "client-chosen")

    # The route gate cancels the first request: its ticket names the shared id.
    for request_id in gone.request_ids():
        await guardrail.on_request_cancelled(request_id)

    # The second request is still being served: its state must still be there
    # for post-call, and its own terminal record must still be written.
    assert "client-chosen" in guardrail._req_state
    token = _TICKET.set(live)
    try:
        now = datetime.now(UTC)
        await guardrail.async_log_success_event(
            {"litellm_call_id": "client-chosen"}, None, now, now
        )
    finally:
        _TICKET.reset(token)
    assert "ok" in [r["status"] for r in sink.records]


async def test_a_litellm_event_during_the_cancel_emit_adds_no_second_record() -> None:
    guardrail, sink = _build_guardrail([(EMAIL, "[EMAIL_1]")])
    data = _data_with_token("tok-1", content=f"write to {EMAIL}")
    data["litellm_call_id"] = "call-1"
    await guardrail.pre_call(data)
    writing = asyncio.Event()
    proceed = asyncio.Event()
    write = sink.write

    async def slow(record: dict[str, Any]) -> None:
        writing.set()
        await proceed.wait()
        await write(record)

    sink.write = slow  # type: ignore[method-assign]
    cancel = asyncio.create_task(guardrail.on_request_cancelled("call-1"))
    await asyncio.wait_for(writing.wait(), 2)
    now = datetime.now(UTC)
    late = asyncio.create_task(
        guardrail.async_log_failure_event({"litellm_call_id": "call-1"}, None, now, now)
    )
    await asyncio.sleep(0.01)
    proceed.set()
    await asyncio.gather(cancel, late)

    assert [r["status"] for r in sink.records] == ["cancelled"]


async def _cancel_in(guardrail: Any, ticket: RequestTicket, call_id: str) -> None:
    # As the route gate calls it: from inside the cancelled request's context.
    ticket.cancelled = True
    token = _TICKET.set(ticket)
    try:
        await asyncio.create_task(guardrail.on_request_cancelled(call_id))
    finally:
        _TICKET.reset(token)


class _Failures(NoopExporter):
    def __init__(self) -> None:
        self.components: list[str] = []

    def record_failure(self, component: str) -> None:
        self.components.append(component)


async def test_a_mismatched_cancel_writes_nothing_and_is_logged_with_counts_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    guardrail, sink = _build_guardrail([(EMAIL, "[EMAIL_1]")])
    guardrail._metrics = metrics = _Failures()
    gone, live = RequestTicket("a" * 32), RequestTicket("b" * 32)
    await _pre_call_in(guardrail, gone, "client-chosen")
    await _pre_call_in(guardrail, live, "client-chosen")

    with caplog.at_level(logging.DEBUG):
        await _cancel_in(guardrail, gone, "client-chosen")

    assert guardrail._req_state["client-chosen"].ticket is live
    assert sink.records == []
    assert metrics.components == ["route_gate"]
    assert "route_gate_cancel_call_id_mismatch redaction_count=1" in caplog.text
    assert EMAIL not in caplog.text
    assert "b" * 32 not in caplog.text


async def test_when_both_sharers_are_cancelled_each_cancel_ends_only_its_own_state() -> None:
    guardrail, sink = _build_guardrail([(EMAIL, "[EMAIL_1]")])
    gone, also_gone = RequestTicket("a" * 32), RequestTicket("b" * 32)
    await _pre_call_in(guardrail, gone, "client-chosen")
    await _pre_call_in(guardrail, also_gone, "client-chosen")
    also_gone.cancelled = True

    await _cancel_in(guardrail, gone, "client-chosen")
    assert "client-chosen" in guardrail._req_state
    await _cancel_in(guardrail, also_gone, "client-chosen")

    assert "client-chosen" not in guardrail._req_state
    (record,) = sink.records
    assert (record["status"], record["redaction_count"]) == ("cancelled", 1)


async def test_a_cancel_from_its_own_request_ends_its_state() -> None:
    guardrail, sink = _build_guardrail([(EMAIL, "[EMAIL_1]")])
    ticket = RequestTicket("a" * 32)
    await _pre_call_in(guardrail, ticket, "call-1")

    await _cancel_in(guardrail, ticket, "call-1")

    assert guardrail._req_state == {}
    assert [(r["status"], r["redaction_count"]) for r in sink.records] == [("cancelled", 1)]


async def test_a_cancel_outside_any_request_ends_state_that_belongs_to_none() -> None:
    guardrail, sink = _build_guardrail([(EMAIL, "[EMAIL_1]")])
    data = _data_with_token("tok-1", content=f"write to {EMAIL}")
    data["litellm_call_id"] = "call-1"
    await guardrail.pre_call(data)
    assert guardrail._req_state["call-1"].ticket is None

    await guardrail.on_request_cancelled("call-1")

    assert guardrail._req_state == {}
    assert [r["status"] for r in sink.records] == ["cancelled"]


async def _stalled_cancel(guardrail: Any, sink: Any, *, fail: bool) -> tuple[Any, Any]:
    data = _data_with_token("tok-1", content=f"write to {EMAIL}")
    data["litellm_call_id"] = "call-1"
    await guardrail.pre_call(data)
    writing = asyncio.Event()
    proceed = asyncio.Event()
    write = sink.write

    async def slow(record: dict[str, Any]) -> None:
        writing.set()
        await proceed.wait()
        if fail:
            raise OSError("sink down")
        await write(record)

    sink.write = slow
    cancel = asyncio.create_task(guardrail.on_request_cancelled("call-1"))
    await asyncio.wait_for(writing.wait(), 2)
    return cancel, (proceed, write)


async def test_a_second_cancel_during_the_cancel_emit_adds_no_second_record() -> None:
    guardrail, sink = _build_guardrail([(EMAIL, "[EMAIL_1]")])
    cancel, (proceed, _) = await _stalled_cancel(guardrail, sink, fail=False)
    again = asyncio.create_task(guardrail.on_request_cancelled("call-1"))
    await asyncio.sleep(0.01)
    proceed.set()
    await asyncio.gather(cancel, again)

    assert [r["status"] for r in sink.records] == ["cancelled"]
    assert guardrail._cancel_emitting == {}


async def test_a_litellm_event_waiting_on_a_failed_cancel_emit_writes_the_cancelled_record() -> (
    None
):
    guardrail, sink = _build_guardrail([(EMAIL, "[EMAIL_1]")])
    cancel, (proceed, write) = await _stalled_cancel(guardrail, sink, fail=True)
    now = datetime.now(UTC)
    late = asyncio.create_task(
        guardrail.async_log_success_event({"litellm_call_id": "call-1"}, None, now, now)
    )
    await asyncio.sleep(0.01)
    assert not late.done()
    sink.write = write
    proceed.set()
    results = await asyncio.gather(cancel, late, return_exceptions=True)

    assert isinstance(results[0], OSError)
    assert results[1] is None
    (record,) = sink.records
    assert (record["status"], record["redaction_count"]) == ("cancelled", 1)
    assert guardrail._cancel_pending == {}
    assert guardrail._cancel_emitting == {}
