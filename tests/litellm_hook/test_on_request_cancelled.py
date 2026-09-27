"""`CorpLlmGuardrail.on_request_cancelled`: the one terminal record of a request
whose client left, and the request-id bridge from the route gate's ticket to
litellm's call id."""

from __future__ import annotations

import asyncio
import json
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


async def test_a_cancelled_emit_failure_propagates_after_marking() -> None:
    guardrail, sink, _ = await _pre_called()

    async def broken(record: dict[str, Any]) -> None:
        raise OSError("sink down")

    sink.write = broken  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await guardrail.on_request_cancelled("call-1")

    assert guardrail._req_state == {}
    now = datetime.now(UTC)
    del sink.write
    await guardrail.async_log_failure_event({"litellm_call_id": "call-1"}, None, now, now)
    assert sink.records == []


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
