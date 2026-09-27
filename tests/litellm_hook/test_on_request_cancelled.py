"""`CorpLlmGuardrail.on_request_cancelled`: the one terminal record of a request
whose client left, and the request-id bridge from the route gate's ticket to
litellm's call id."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from corp_llm_gateway.litellm_hook import GuardrailHttpException
from corp_llm_gateway.route_gate.inflight import _TICKET, RequestTicket
from tests.test_litellm_hook import _build_guardrail, _data_with_token

EMAIL = "alice.secret@corp.example"


async def _pre_called(call_id: str = "call-1") -> tuple[Any, Any, dict[str, Any]]:
    guardrail, sink = _build_guardrail([(EMAIL, "[EMAIL_1]")])
    data = _data_with_token("tok-1", content=f"write to {EMAIL}")
    data["litellm_call_id"] = call_id
    await guardrail.pre_call(data)
    return guardrail, sink, data


async def test_a_cancelled_request_gets_one_record_with_counts_only() -> None:
    guardrail, sink, _ = await _pre_called()
    assert "call-1" in guardrail._req_state

    await guardrail.on_request_cancelled("call-1", latency_ms=1234)

    assert "call-1" not in guardrail._req_state
    (record,) = sink.records
    assert record["status"] == "cancelled"
    assert record["error_code"] == "E_CLIENT_DISCONNECTED"
    assert (record["user_id"], record["team_id"]) == ("alice", "t1")
    assert record["redaction_count"] == 1
    assert record["finding_label_counts"] == {"EMAIL": 1}
    assert record["latency_ms"] == 1234
    assert "placeholder_list" not in record
    assert EMAIL not in json.dumps(record)


async def test_cancelling_twice_emits_once() -> None:
    guardrail, sink, _ = await _pre_called()

    await guardrail.on_request_cancelled("call-1")
    await guardrail.on_request_cancelled("call-1")

    assert [r["status"] for r in sink.records] == ["cancelled"]


async def test_a_late_litellm_failure_event_does_not_add_a_second_record() -> None:
    guardrail, sink, _ = await _pre_called()
    await guardrail.on_request_cancelled("call-1")
    now = datetime.now(UTC)

    await guardrail.async_log_failure_event({"litellm_call_id": "call-1"}, None, now, now)
    await guardrail.async_log_success_event({"litellm_call_id": "call-1"}, None, now, now)

    assert [r["status"] for r in sink.records] == ["cancelled"]
    assert guardrail._req_state == {}


async def test_an_already_audited_request_gets_no_cancelled_record() -> None:
    guardrail, sink, _ = await _pre_called()
    now = datetime.now(UTC)
    await guardrail.async_log_success_event({"litellm_call_id": "call-1"}, None, now, now)

    await guardrail.on_request_cancelled("call-1")

    assert [r["status"] for r in sink.records] == ["ok"]


async def test_an_unknown_request_id_still_gets_one_record() -> None:
    # Cancelled before the pre-call hook ever ran: the gateway id is all there is.
    guardrail, sink = _build_guardrail()

    await guardrail.on_request_cancelled("0" * 32)

    (record,) = sink.records
    assert record["status"] == "cancelled"
    assert (record["user_id"], record["team_id"], record["redaction_count"]) == (
        "unknown",
        "unknown",
        0,
    )


async def test_a_failed_cancel_emit_keeps_the_state_for_a_later_terminal_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    guardrail, sink, _ = await _pre_called()

    async def broken(record: dict[str, Any]) -> None:
        raise OSError(f"sink down near {EMAIL}")

    sink.write = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.DEBUG), pytest.raises(OSError):
        await guardrail.on_request_cancelled("call-1")

    # Nothing was written, so nothing is marked and the state stays.
    assert "call-1" in guardrail._req_state
    assert "call-1" not in guardrail._audited_ids
    assert EMAIL not in caplog.text
    assert "OSError" in caplog.text
    now = datetime.now(UTC)
    del sink.write
    await guardrail.async_log_failure_event({"litellm_call_id": "call-1"}, None, now, now)
    await guardrail.async_log_failure_event({"litellm_call_id": "call-1"}, None, now, now)

    (record,) = sink.records
    assert record["status"] == "failed"
    assert (record["user_id"], record["team_id"], record["redaction_count"]) == ("alice", "t1", 1)
    assert guardrail._req_state == {}
    await guardrail.on_request_cancelled("call-1")
    assert len(sink.records) == 1


async def test_a_failed_cancel_emit_lets_a_late_event_inside_the_cancelled_request_write() -> None:
    # litellm's failure event for a cancelled call runs in the request's context,
    # where an audit normally stands down for the cancel record — which never landed.
    guardrail, sink, _ = await _pre_called()

    async def broken(record: dict[str, Any]) -> None:
        raise OSError("sink down")

    sink.write = broken  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await guardrail.on_request_cancelled("call-1")
    del sink.write
    ticket = RequestTicket("gateway-id")
    ticket.cancelled = True
    token = _TICKET.set(ticket)
    try:
        now = datetime.now(UTC)
        await guardrail.async_log_failure_event({"litellm_call_id": "call-1"}, None, now, now)
    finally:
        _TICKET.reset(token)

    assert [r["status"] for r in sink.records] == ["failed"]
    assert guardrail._req_state == {}


async def test_a_cancel_emit_cut_short_by_a_timeout_keeps_the_state() -> None:
    guardrail, sink, _ = await _pre_called()

    async def stalled(record: dict[str, Any]) -> None:
        await asyncio.sleep(3600)

    sink.write = stalled  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(guardrail.on_request_cancelled("call-1"), 0.05)

    assert "call-1" in guardrail._req_state
    del sink.write
    now = datetime.now(UTC)
    await guardrail.async_log_failure_event({"litellm_call_id": "call-1"}, None, now, now)
    assert [r["status"] for r in sink.records] == ["failed"]


async def test_an_ambiguous_cancel_emit_counts_as_written() -> None:
    from corp_llm_gateway.audit import AuditWriteAmbiguousError

    guardrail, sink, _ = await _pre_called()

    async def ambiguous(record: dict[str, Any]) -> None:
        raise AuditWriteAmbiguousError("ack lost")

    sink.write = ambiguous  # type: ignore[method-assign]
    with pytest.raises(AuditWriteAmbiguousError):
        await guardrail.on_request_cancelled("call-1")

    assert guardrail._req_state == {}
    del sink.write
    now = datetime.now(UTC)
    await guardrail.async_log_failure_event({"litellm_call_id": "call-1"}, None, now, now)
    assert sink.records == []


async def test_a_pre_call_reached_after_the_request_was_cancelled_is_refused_without_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A downstream that outlived the cancel grace: its call id would bind to a
    # ticket whose cancel record is already written, and its state would never go.
    guardrail, sink = _build_guardrail([(EMAIL, "[EMAIL_1]")])
    ticket = RequestTicket("gateway-id")
    ticket.cancelled = True
    token = _TICKET.set(ticket)
    try:
        data = _data_with_token("tok-1", content=f"write to {EMAIL}")
        data["litellm_call_id"] = "call-late"
        with caplog.at_level(logging.DEBUG), pytest.raises(GuardrailHttpException) as exc:
            await guardrail.pre_call(data)
        now = datetime.now(UTC)
        await guardrail.async_log_failure_event({"litellm_call_id": "call-late"}, None, now, now)
    finally:
        _TICKET.reset(token)

    assert exc.value.error_code == "E_CLIENT_DISCONNECTED"
    assert ticket.call_ids == []
    assert guardrail._req_state == {}
    assert sink.records == []
    assert "route_gate_cancel_incomplete" in caplog.text
    assert EMAIL not in caplog.text


async def test_pre_call_binds_litellms_call_id_to_the_current_request() -> None:
    guardrail, _ = _build_guardrail()
    ticket = RequestTicket("gateway-id")
    token = _TICKET.set(ticket)
    try:
        data = _data_with_token("tok-1")
        data["litellm_call_id"] = "call-7"
        await asyncio.create_task(guardrail.pre_call(data))
    finally:
        _TICKET.reset(token)

    assert ticket.call_ids == ["call-7"]
    assert ticket.request_ids() == ["call-7"]


async def test_a_refused_pre_call_still_binds_its_call_id() -> None:
    guardrail, _ = _build_guardrail()
    ticket = RequestTicket("gateway-id")
    token = _TICKET.set(ticket)
    try:
        data = _data_with_token("wrong-token")
        data["litellm_call_id"] = "call-8"
        with pytest.raises(GuardrailHttpException):
            await guardrail.pre_call(data)
    finally:
        _TICKET.reset(token)

    assert ticket.call_ids == ["call-8"]


async def test_an_audit_inside_a_cancelled_request_stands_down_for_the_cancel_record() -> None:
    # litellm's partial-stream billing fires a success event while the request
    # unwinds; the one terminal record must stay the `cancelled` one.
    guardrail, sink, _ = await _pre_called()
    ticket = RequestTicket("gateway-id")
    ticket.cancelled = True
    token = _TICKET.set(ticket)
    try:
        now = datetime.now(UTC)
        await guardrail.async_log_success_event({"litellm_call_id": "call-1"}, None, now, now)
        await guardrail.async_log_failure_event({"litellm_call_id": "call-1"}, None, now, now)
    finally:
        _TICKET.reset(token)

    assert sink.records == []
    await guardrail.on_request_cancelled("call-1")
    assert [r["status"] for r in sink.records] == ["cancelled"]
    assert guardrail._req_state == {}


async def test_an_audit_inside_a_live_request_is_written() -> None:
    guardrail, sink, _ = await _pre_called()
    token = _TICKET.set(RequestTicket("gateway-id"))
    try:
        now = datetime.now(UTC)
        await guardrail.async_log_success_event({"litellm_call_id": "call-1"}, None, now, now)
    finally:
        _TICKET.reset(token)

    assert [r["status"] for r in sink.records] == ["ok"]
