"""`CorpLlmGuardrail.on_request_cancelled` at its edges: many unknown ids, a
cancel beside a live request, and a guardrail rebuilt after a failed emit."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime

from corp_llm_gateway import litellm_hook
from corp_llm_gateway.route_gate.inflight import _TICKET, RequestTicket
from tests.litellm_hook.test_on_request_cancelled import EMAIL, _pre_called
from tests.test_litellm_hook import _build_guardrail, _data_with_token


async def test_unknown_ids_leave_no_state_behind_and_the_dedup_set_stays_capped() -> None:
    guardrail, sink = _build_guardrail()
    count = litellm_hook._AUDIT_DEDUP_CAP + 100

    for index in range(count):
        await guardrail.on_request_cancelled(f"{index:032x}")

    assert len(sink.records) == count
    assert {r["status"] for r in sink.records} == {"cancelled"}
    assert guardrail._req_state == {}
    assert guardrail._cancel_pending == {}
    assert len(guardrail._audited_ids) == litellm_hook._AUDIT_DEDUP_CAP


async def test_cancelling_one_request_leaves_a_live_one_untouched() -> None:
    guardrail, sink, _ = await _pre_called("call-1")
    other = _data_with_token("tok-1", content=f"also {EMAIL}")
    other["litellm_call_id"] = "call-2"
    await guardrail.pre_call(other)
    live = guardrail._req_state["call-2"]

    await guardrail.on_request_cancelled("call-1")

    assert guardrail._req_state == {"call-2": live}
    now = datetime.now(UTC)
    await guardrail.async_log_success_event({"litellm_call_id": "call-2"}, None, now, now)
    assert [(r["request_id"], r["status"]) for r in sink.records] == [
        ("call-1", "cancelled"),
        ("call-2", "ok"),
    ]
    assert guardrail._req_state == {}


async def test_a_cancel_inside_a_ticket_writes_the_same_single_record() -> None:
    # The limiter runs the hook from its own task; a ticket in the context,
    # cancelled or not, must not make the cancel record stand down.
    guardrail, sink, _ = await _pre_called()
    ticket = RequestTicket("gateway-id")
    ticket.cancelled = True
    token = _TICKET.set(ticket)
    try:
        await guardrail.on_request_cancelled("call-1")
    finally:
        _TICKET.reset(token)

    assert [r["status"] for r in sink.records] == ["cancelled"]
    assert guardrail._req_state == {}


async def test_a_rebuilt_guardrail_writes_one_content_free_record_for_a_lost_cancel() -> None:
    old, old_sink, _ = await _pre_called()

    async def broken(record: dict[str, object]) -> None:
        raise OSError("sink down")

    old_sink.write = broken  # type: ignore[method-assign]
    with contextlib.suppress(OSError):
        await old.on_request_cancelled("call-1")
    assert "call-1" in old._cancel_pending

    fresh, sink = _build_guardrail()
    now = datetime.now(UTC)
    await fresh.async_log_failure_event({"litellm_call_id": "call-1"}, None, now, now)
    await fresh.async_log_failure_event({"litellm_call_id": "call-1"}, None, now, now)

    (record,) = sink.records
    assert record["request_id"] == "call-1"
    assert EMAIL not in json.dumps(record)
    assert fresh._req_state == {}
    assert fresh._cancel_pending == {}
