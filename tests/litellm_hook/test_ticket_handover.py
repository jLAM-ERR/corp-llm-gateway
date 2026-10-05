"""The guardrail has no response-side hook: the pre-call hands the mapping and the audit facts
to the request's ticket, and asks a chat stream for usage."""

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
from corp_llm_gateway.route_gate.terminal_audit import TerminalAudit, emit_to
from tests.hook_fixtures import (
    _build_guardrail,
    _data_with_token,
    _in_context,
    _RecordingMetrics,
    _ticketed_pre_call,
)

# ── no reversal in the callback: the pre-call hands the request to its ticket ──

_RESPONSE_SIDE_HOOKS = (
    "async_post_call_success_hook",
    "async_post_call_streaming_iterator_hook",
    "async_post_call_streaming_hook",
    "async_post_call_response_headers_hook",
    "post_call_unary",
    "post_call_stream",
)


@pytest.mark.parametrize("name", _RESPONSE_SIDE_HOOKS)
def test_the_guardrail_defines_no_response_side_hook(name: str) -> None:
    """litellm dispatches a hook when the leaf class defines it (proxy/utils.py); ours
    defines none, so nothing of ours touches a response inside litellm. The reversal is
    the ASGI desanitiser, the one place a response is restored."""
    assert name not in vars(CorpLlmGuardrail)


async def test_a_ticketed_pre_call_hands_the_mapping_and_the_record_to_the_ticket() -> None:
    from corp_llm_gateway.route_gate.desanitize_middleware import ResponseMappings
    from corp_llm_gateway.route_gate.inflight import _TICKET, RequestTicket
    from corp_llm_gateway.route_gate.terminal_audit import AuditFacts

    g, sink = _build_guardrail([("alice", "[N1]")])
    mappings = ResponseMappings()
    g.bind_response_mappings(mappings)
    ticket = RequestTicket("e" * 32)
    data = _data_with_token("tok-1", content="hi alice")
    data["litellm_call_id"] = "call-7"
    token = _TICKET.set(ticket)
    try:
        await g.pre_call(data)
    finally:
        _TICKET.reset(token)

    mapping = mappings.get(ticket)
    assert mapping is not None and mapping.pairs == (("alice", "[N1]"),)
    assert isinstance(ticket.audit_facts, AuditFacts)
    assert ticket.audit_facts.request_id == "call-7"
    # The guardrail keeps neither the content nor the record.
    assert g._req_state == {}
    now = datetime.now(UTC)
    await g.async_log_success_event({"litellm_call_id": "call-7"}, None, now, now)
    await g.async_log_failure_event({"litellm_call_id": "call-7"}, None, now, now)
    await g.on_request_cancelled("call-7")
    assert sink.records == []


async def test_an_unticketed_pre_call_keeps_todays_record() -> None:
    """Outside the route gate (no ticket) nothing restores the response and the
    guardrail still writes the request's record from its log event."""
    g, sink = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    data["litellm_call_id"] = "call-8"

    await g.pre_call(data)
    now = datetime.now(UTC)
    await g.async_log_success_event({"litellm_call_id": "call-8"}, None, now, now)

    assert [(r["status"], r["redaction_count"]) for r in sink.records] == [("ok", 1)]
    assert g._req_state == {}


async def test_a_log_event_in_its_requests_context_adds_to_the_ticket_past_the_fifo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """litellm's logging worker runs each event in the request's copied context
    (``logging_worker.py``): the ticket there owns the record, however many requests
    were handed over since. One record, with the request's identity and the log's counts."""
    import corp_llm_gateway.litellm_hook as hook_mod
    from corp_llm_gateway.route_gate.desanitize_middleware import ResponseMappings
    from corp_llm_gateway.route_gate.inflight import RequestTicket

    monkeypatch.setattr(hook_mod, "_AUDIT_DEDUP_CAP", 1)
    g, sink = _build_guardrail([("alice", "[N1]")])
    g.bind_response_mappings(ResponseMappings())
    terminal = TerminalAudit(emit_to(g._audit))
    tickets = []
    for i in range(2):
        ticket = RequestTicket(f"{i:032x}")
        terminal.bind(ticket)
        data = _data_with_token("tok-1", content="hi alice")
        data["litellm_call_id"] = f"call-{i}"
        await _ticketed_pre_call(g, data, ticket)
        tickets.append(ticket)
    assert "call-0" not in g._terminal_owned

    now = datetime.now(UTC)
    usage = {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}
    await _in_context(
        tickets[0], g.async_log_success_event({"litellm_call_id": "call-0"}, usage, now, now)
    )
    await _in_context(tickets[0], g.on_request_cancelled("call-0"))
    await terminal.publish(tickets[0], "ok")
    tickets[0].close()
    await terminal.drain()

    (record,) = sink.records
    assert (record["request_id"], record["user_id"], record["team_id"]) == ("call-0", "alice", "t1")
    assert (record["status"], record["redaction_count"]) == ("ok", 1)
    assert (record["prompt_token_count"], record["completion_token_count"]) == (3, 1)


async def test_a_closed_tickets_id_leaves_the_hand_over_fifo() -> None:
    """The ticket's close wrote (or lost) its record: its id leaves the FIFO, and a later
    event for it outside any request writes nothing."""
    from corp_llm_gateway.route_gate.desanitize_middleware import ResponseMappings
    from corp_llm_gateway.route_gate.inflight import RequestTicket

    g, sink = _build_guardrail([("alice", "[N1]")])
    g.bind_response_mappings(ResponseMappings())
    ticket = RequestTicket("c" * 32)
    data = _data_with_token("tok-1", content="hi alice")
    data["litellm_call_id"] = "call-9"
    await _ticketed_pre_call(g, data, ticket)
    assert "call-9" in g._terminal_owned

    ticket.close()

    assert "call-9" not in g._terminal_owned
    now = datetime.now(UTC)
    await g.async_log_success_event({"litellm_call_id": "call-9"}, None, now, now)
    await g.on_request_cancelled("call-9")
    assert sink.records == []


@pytest.mark.parametrize("event", ["success", "failure"])
async def test_a_log_event_for_an_evicted_id_outside_its_request_writes_no_record(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, event: str
) -> None:
    """A handed-over id pushed out of the FIFO, logged in no request's context: its state
    went to the ticket, so the guardrail writes no ``unknown`` record. It logs the orphan
    by type and counts it; the ticket still writes the request's one record."""
    import corp_llm_gateway.litellm_hook as hook_mod
    from corp_llm_gateway.route_gate.desanitize_middleware import ResponseMappings
    from corp_llm_gateway.route_gate.inflight import RequestTicket

    monkeypatch.setattr(hook_mod, "_AUDIT_DEDUP_CAP", 1)
    g, sink = _build_guardrail([("alice", "[N1]")])
    metrics = _RecordingMetrics()
    g._metrics = metrics
    g.bind_response_mappings(ResponseMappings())
    terminal = TerminalAudit(emit_to(g._audit))
    tickets = []
    for i in range(2):
        ticket = RequestTicket(f"{i:032x}")
        terminal.bind(ticket)
        data = _data_with_token("tok-1", content="hi alice")
        data["litellm_call_id"] = f"call-{i}"
        await _ticketed_pre_call(g, data, ticket)
        tickets.append(ticket)
    assert "call-0" not in g._terminal_owned

    now = datetime.now(UTC)
    usage = {"usage": {"prompt_tokens": 3, "completion_tokens": 1}}
    log = g.async_log_success_event if event == "success" else g.async_log_failure_event
    with caplog.at_level(logging.DEBUG):
        await log({"litellm_call_id": "call-0"}, usage, now, now)

    status = "ok" if event == "success" else "failed"
    assert sink.records == []
    assert f"litellm_audit_orphan_event request_id=call-0 status={status}" in caplog.text
    assert metrics.failures == ["audit"]
    assert metrics.latencies == []
    await terminal.publish(tickets[0], "ok")
    tickets[0].close()
    await terminal.drain()
    (record,) = sink.records
    assert (record["request_id"], record["user_id"], record["team_id"]) == ("call-0", "alice", "t1")
    assert (record["status"], record["redaction_count"]) == ("ok", 1)


@pytest.mark.parametrize("ticketed", [False, True], ids=["unticketed", "ticketed"])
async def test_a_pre_call_refusal_still_writes_its_record_inline(ticketed: bool) -> None:
    """Behind the desanitiser too, a refusal before any state exists is not an orphan: its
    record is the inline one, and litellm's failure log for it is a duplicate."""
    from corp_llm_gateway.route_gate.desanitize_middleware import ResponseMappings
    from corp_llm_gateway.route_gate.inflight import RequestTicket

    g, sink = _build_guardrail()
    metrics = _RecordingMetrics()
    g._metrics = metrics
    g.bind_response_mappings(ResponseMappings())
    data = {"model": "claude", "messages": [], "headers": {}, "litellm_call_id": "call-r"}

    with pytest.raises(GuardrailHttpException) as ei:
        if ticketed:
            await _ticketed_pre_call(g, data, RequestTicket("d" * 32))
        else:
            await g.pre_call(data)

    assert ei.value.error_code == "E_MISSING_TOKEN"
    (record,) = sink.records
    assert (record["request_id"], record["user_id"], record["status"]) == (
        "call-r",
        "unknown",
        "failed",
    )
    assert metrics.failures == ["auth"]
    now = datetime.now(UTC)
    await g.async_log_failure_event({"litellm_call_id": "call-r"}, None, now, now)
    assert len(sink.records) == 1
    assert metrics.failures == ["auth"]


# ── chat streams ask the provider for usage; the desanitiser drops what the client
# did not ask for ──


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        (None, {"include_usage": True}),
        ({"include_usage": False}, {"include_usage": True}),
        ({"include_obfuscation": False}, {"include_obfuscation": False, "include_usage": True}),
    ],
    ids=["absent", "declined", "other-key"],
)
async def test_a_ticketed_chat_stream_asks_for_usage_the_client_did_not(
    options: dict[str, Any] | None, expected: dict[str, Any]
) -> None:
    from corp_llm_gateway.route_gate.inflight import RequestTicket

    g, _ = _build_guardrail([("alice", "[N1]")])
    ticket = RequestTicket("a" * 32)
    data = _data_with_token("tok-1", content="hi alice", model="gpt-4o")
    data["stream"] = True
    if options is not None:
        data["stream_options"] = options

    class _LoggingObj:
        def __init__(self) -> None:
            self.stream_options = options

    data["litellm_logging_obj"] = _LoggingObj()

    await _ticketed_pre_call(g, data, ticket, call_type="acompletion")

    assert data["stream_options"] == expected
    # litellm's stream wrapper reads them off its logging object, set before any pre-call.
    assert data["litellm_logging_obj"].stream_options == expected
    assert ticket.audit_facts.client_asked_usage is False


async def test_litellms_own_usage_strip_is_handed_to_the_desanitiser() -> None:
    """litellm 1.101.0 asks a chat stream for usage itself when the client did not, and
    strips the chunk before the response leaves its app; the desanitiser must see it."""
    from corp_llm_gateway.route_gate.inflight import RequestTicket

    g, _ = _build_guardrail([("alice", "[N1]")])
    ticket = RequestTicket("e" * 32)
    data = _data_with_token("tok-1", content="hi alice", model="gpt-4o")
    data["stream"] = True
    data["stream_options"] = {"include_usage": True}
    data["_litellm_strip_stream_usage"] = True

    class _LoggingObj:
        def __init__(self) -> None:
            self.stream_options = {"include_usage": True}

    data["litellm_logging_obj"] = _LoggingObj()

    await _ticketed_pre_call(g, data, ticket, call_type="acompletion")

    assert data["_litellm_strip_stream_usage"] is False
    assert data["stream_options"] == {"include_usage": True}
    assert data["litellm_logging_obj"].stream_options == {"include_usage": True}
    assert ticket.audit_facts.client_asked_usage is False


async def test_a_chat_stream_whose_client_asked_for_usage_is_left_alone() -> None:
    from corp_llm_gateway.route_gate.inflight import RequestTicket

    g, _ = _build_guardrail([("alice", "[N1]")])
    ticket = RequestTicket("b" * 32)
    data = _data_with_token("tok-1", content="hi alice", model="gpt-4o")
    data["stream"] = True
    data["stream_options"] = {"include_usage": True}

    await _ticketed_pre_call(g, data, ticket, call_type="acompletion")

    assert data["stream_options"] == {"include_usage": True}
    assert ticket.audit_facts.client_asked_usage is True


@pytest.mark.parametrize(
    ("call_type", "stream", "ticketed"),
    [
        ("acompletion", False, True),
        ("anthropic_messages", True, True),
        ("aresponses", True, True),
        ("acompletion", True, False),
    ],
    ids=["chat-unary", "messages-sse", "responses-sse", "no-ticket"],
)
async def test_nothing_else_is_asked_for_usage(
    call_type: str, stream: bool, ticketed: bool
) -> None:
    """Only a ticketed chat stream: nothing outside the route gate would drop the chunk."""
    from corp_llm_gateway.route_gate.inflight import RequestTicket

    g, _ = _build_guardrail([("alice", "[N1]")])
    ticket = RequestTicket("d" * 32)
    data = _data_with_token("tok-1", content="hi alice", model="gpt-4o")
    data["stream"] = stream

    if ticketed:
        await _ticketed_pre_call(g, data, ticket, call_type=call_type)
        assert ticket.audit_facts.client_asked_usage is True
    else:
        await g.pre_call(data, call_type=call_type)
    assert "stream_options" not in data
