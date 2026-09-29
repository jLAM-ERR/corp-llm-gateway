"""The terminal-audit contract: facts deposited on the ticket, one record published by
whoever ends the response, never by a callback awaiting it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import pytest

from corp_llm_gateway.audit import AuditLogger, AuditWriteAmbiguousError, ListSink
from corp_llm_gateway.metrics import NoopExporter
from corp_llm_gateway.route_gate.inflight import (
    CANCEL_CLIENT,
    CANCEL_SERVER,
    InflightLimiter,
    RequestTicket,
    current_ticket,
    install_task_factory,
)
from corp_llm_gateway.route_gate.terminal_audit import (
    E_CLIENT_DISCONNECTED,
    E_INTERNAL,
    E_SERVER_SHUTDOWN,
    AuditFacts,
    TerminalAudit,
    TerminalRecord,
    deposit,
    deposit_usage,
    emit_to,
)

CANARY = "CANARY-terminal-7c1"


def _facts(**overrides: object) -> AuditFacts:
    values: dict[str, object] = {
        "request_id": "call-1",
        "user_id": "alice",
        "team_id": "t1",
        "provider": "anthropic",
        "model": "claude",
        "redaction_count": 2,
        "finding_label_counts": {"EMAIL": 1, "NAME": 1},
        "profile_ids": ("base",),
    }
    values.update(overrides)
    return AuditFacts(**values)  # type: ignore[arg-type]


class _Sink:
    def __init__(self, *failures: BaseException) -> None:
        self.records: list[TerminalRecord] = []
        self.failures = list(failures)

    async def __call__(self, record: TerminalRecord) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.records.append(record)

    @property
    def outcomes(self) -> list[tuple[str, str | None]]:
        return [(r.outcome, r.error_code) for r in self.records]


def _ticket(facts: AuditFacts | None = None) -> RequestTicket:
    ticket = RequestTicket("a" * 32)
    if facts is not None:
        assert deposit(ticket, facts)
    return ticket


async def test_one_record_whatever_the_number_of_publishes() -> None:
    sink = _Sink()
    terminal = TerminalAudit(sink)
    ticket = _ticket(_facts())
    terminal.bind(ticket)

    await terminal.publish(ticket, "ok")
    await terminal.publish(ticket, "failed", error_code=E_INTERNAL)
    ticket.close()
    await terminal.drain()

    assert sink.outcomes == [("ok", None)]


async def test_the_record_keeps_the_counts_and_identity_the_facts_carry() -> None:
    sink = ListSink()
    terminal = TerminalAudit(emit_to(AuditLogger(sink, gateway_version="t")))
    ticket = _ticket(_facts())

    await terminal.publish(ticket, "ok")

    (record,) = sink.records
    assert record["request_id"] == "call-1" and record["user_id"] == "alice"
    assert record["redaction_count"] == 2
    assert record["finding_label_counts"] == {"EMAIL": 1, "NAME": 1}
    assert list(record["profile_ids"]) == ["base"]
    assert record["status"] == "ok"


async def test_a_restoration_failure_is_failed_with_the_internal_code() -> None:
    """Same status and code as the guardrail's own F8 safety net writes."""
    sink = _Sink()
    ticket = _ticket(_facts())

    await TerminalAudit(sink).publish(ticket, "failed", error_code=E_INTERNAL)

    assert sink.outcomes == [("failed", "E_INTERNAL")]


async def test_an_answer_published_after_the_client_left_is_cancelled() -> None:
    """litellm's own disconnect watcher can end the request with an error response the
    gone client never reads: that is the client's cancel, not a failure."""
    sink = _Sink()
    ticket = _ticket(_facts())
    ticket.mark_cancelled(CANCEL_CLIENT)

    await TerminalAudit(sink).publish(ticket, "failed")

    assert sink.outcomes == [("cancelled", E_CLIENT_DISCONNECTED)]


async def test_an_error_answer_published_after_a_server_cancel_stays_failed() -> None:
    """The client is still there to read it."""
    sink = _Sink()
    ticket = _ticket(_facts())
    ticket.mark_cancelled(CANCEL_SERVER)

    await TerminalAudit(sink).publish(ticket, "failed")

    assert sink.outcomes == [("failed", None)]


async def test_ok_on_a_cancelled_ticket_is_cancelled() -> None:
    sink = _Sink()
    ticket = _ticket(_facts())
    ticket.cancelled = True

    await TerminalAudit(sink).publish(ticket, "ok")

    assert sink.outcomes == [("cancelled", E_CLIENT_DISCONNECTED)]


async def test_the_first_outcome_decided_sticks_across_a_cancel() -> None:
    """A restoration failure decided first stays the record even if the client then left."""
    sink = _Sink(RuntimeError("sink down"))
    terminal = TerminalAudit(sink)
    ticket = _ticket(_facts())
    terminal.bind(ticket)

    await terminal.publish(ticket, "failed", error_code=E_INTERNAL)
    ticket.cancelled = True
    ticket.close()
    await terminal.drain()

    assert sink.outcomes == [("failed", E_INTERNAL)]


async def test_a_close_before_any_final_body_publishes_cancelled_for_a_gone_client() -> None:
    sink = _Sink()
    terminal = TerminalAudit(sink)
    ticket = _ticket(_facts())
    terminal.bind(ticket)
    ticket.cancelled = True

    ticket.close()
    await terminal.drain()

    assert sink.outcomes == [("cancelled", E_CLIENT_DISCONNECTED)]


async def test_a_close_without_a_final_body_or_a_cancel_is_a_failure() -> None:
    sink = _Sink()
    terminal = TerminalAudit(sink)
    ticket = _ticket(_facts())
    terminal.bind(ticket)

    ticket.close()
    await terminal.drain()

    assert sink.outcomes == [("failed", E_INTERNAL)]


@pytest.mark.parametrize("late", ["failed", "ok"])
async def test_a_publish_between_the_close_and_its_write_keeps_the_closes_outcome(
    late: str,
) -> None:
    """The close decides before it schedules the write: a publish that gets the loop first
    writes the close's outcome, not its own."""
    sink = _Sink()
    terminal = TerminalAudit(sink)
    ticket = _ticket(_facts())
    terminal.bind(ticket)
    ticket.cancelled = True

    ticket.close()
    await terminal.publish(ticket, late, error_code=E_INTERNAL if late == "failed" else None)  # type: ignore[arg-type]
    await terminal.drain()

    assert sink.outcomes == [("cancelled", E_CLIENT_DISCONNECTED)]
    assert ticket.audit_facts.attempts == 1


async def test_the_close_decides_even_when_its_write_never_runs() -> None:
    sink = _Sink()
    terminal = TerminalAudit(sink)
    ticket = _ticket(_facts())
    terminal.bind(ticket)
    ticket.cancel_origin = "server"

    ticket.close()

    assert ticket.audit_facts.decided == ("cancelled", E_SERVER_SHUTDOWN)
    await terminal.drain()
    assert sink.outcomes == [("cancelled", E_SERVER_SHUTDOWN)]


class _Failures(NoopExporter):
    def __init__(self) -> None:
        self.failures: list[str] = []

    def record_failure(self, component: str) -> None:
        self.failures.append(component)


class _Fatal(BaseException):
    """Escapes the write's own ``except Exception``."""


async def test_a_close_write_that_escapes_is_logged_by_type_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics = _Failures()
    terminal = TerminalAudit(_Sink(_Fatal(CANARY)), metrics=metrics)
    ticket = _ticket(_facts())
    terminal.bind(ticket)

    with caplog.at_level(logging.DEBUG):
        ticket.close()
        await terminal.drain()

    assert metrics.failures == ["desanitize"]
    assert f"terminal_audit_publish_failed request_id={ticket.gateway_id} error=_Fatal" in (
        caplog.text
    )
    assert CANARY not in caplog.text


async def test_the_close_write_has_no_retry_of_its_own(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics = _Failures()
    sink = _Sink(RuntimeError(CANARY), RuntimeError(CANARY))
    terminal = TerminalAudit(sink, metrics=metrics)
    ticket = _ticket(_facts())
    terminal.bind(ticket)

    with caplog.at_level(logging.DEBUG):
        ticket.close()
        await terminal.drain()
        await asyncio.sleep(0)

    assert len(sink.failures) == 1 and sink.records == []
    assert not ticket.audit_facts.published
    assert metrics.failures == ["desanitize"]
    assert caplog.text.count("gateway_terminal_audit_lost") == 1
    assert "error=RuntimeError" in caplog.text and CANARY not in caplog.text


async def test_a_failed_write_is_retried_once_at_close_with_the_same_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = _Sink(RuntimeError(CANARY))
    terminal = TerminalAudit(sink)
    ticket = _ticket(_facts())
    terminal.bind(ticket)

    with caplog.at_level(logging.DEBUG):
        await terminal.publish(ticket, "ok")
        assert sink.records == []
        ticket.close()
        await terminal.drain()

    assert sink.outcomes == [("ok", None)]
    assert "gateway_terminal_audit_failed" in caplog.text and "RuntimeError" in caplog.text
    assert CANARY not in caplog.text


async def test_an_ambiguous_write_is_never_retried() -> None:
    sink = _Sink(AuditWriteAmbiguousError("maybe"))
    terminal = TerminalAudit(sink)
    ticket = _ticket(_facts())
    terminal.bind(ticket)

    await terminal.publish(ticket, "ok")
    ticket.close()
    await terminal.drain()

    assert sink.records == []
    assert ticket.audit_facts.published


async def test_a_publish_while_one_is_in_flight_writes_nothing_more() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    records: list[TerminalRecord] = []

    async def slow(record: TerminalRecord) -> None:
        entered.set()
        await release.wait()
        records.append(record)

    terminal = TerminalAudit(slow)
    ticket = _ticket(_facts())
    first = asyncio.create_task(terminal.publish(ticket, "ok"))
    await entered.wait()

    await terminal.publish(ticket, "failed", error_code=E_INTERNAL)
    release.set()
    await first

    assert [r.outcome for r in records] == ["ok"]


class _Gated:
    """The first write parks until released, then fails or lands; later writes land unless
    ``later_fail``. ``owned`` says, per write, whether the task writing belongs to ``ticket``."""

    def __init__(
        self, *, first_fails: bool, later_fail: bool = False, ticket: RequestTicket | None = None
    ) -> None:
        self.first_fails = first_fails
        self.later_fail = later_fail
        self.ticket = ticket
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.records: list[TerminalRecord] = []
        self.owned: list[bool] = []
        self.calls = 0

    async def __call__(self, record: TerminalRecord) -> None:
        self.calls += 1
        if self.ticket is not None:
            self.owned.append(asyncio.current_task() in self.ticket.tasks)
        if self.calls == 1:
            self.entered.set()
            while not self.release.is_set():
                # Stubborn: cancelling the request does not end this write.
                with contextlib.suppress(asyncio.CancelledError):
                    await self.release.wait()
            if self.first_fails:
                raise RuntimeError(CANARY)
        elif self.later_fail:
            raise RuntimeError(CANARY)
        self.records.append(record)

    @property
    def outcomes(self) -> list[tuple[str, str | None]]:
        return [(r.outcome, r.error_code) for r in self.records]


@pytest.mark.parametrize("first_fails", [True, False])
async def test_a_close_during_an_in_flight_write_writes_only_if_that_write_fails(
    first_fails: bool,
) -> None:
    """The client leaves while the ``ok`` write is in flight: a failed write is retried once
    with its own outcome (the client got the response), a landed one leaves nothing to
    write. The sink never decides the outcome."""
    expected = [("ok", None)]
    sink = _Gated(first_fails=first_fails)
    terminal = TerminalAudit(sink)
    ticket = _ticket(_facts())
    terminal.bind(ticket)
    publishing = asyncio.create_task(terminal.publish(ticket, "ok"))
    await asyncio.wait_for(sink.entered.wait(), 2)

    ticket.mark_cancelled(CANCEL_CLIENT)
    ticket.close()
    sink.release.set()
    await publishing
    await terminal.drain()

    assert sink.outcomes == expected
    assert sink.calls == len(expected) + int(first_fails)
    assert ticket.audit_facts.published


async def test_the_close_behind_an_in_flight_write_is_the_last_attempt() -> None:
    metrics = _Failures()
    sink = _Gated(first_fails=True, later_fail=True)
    terminal = TerminalAudit(sink, metrics=metrics)
    ticket = _ticket(_facts())
    terminal.bind(ticket)
    publishing = asyncio.create_task(terminal.publish(ticket, "ok"))
    await asyncio.wait_for(sink.entered.wait(), 2)

    ticket.close()
    sink.release.set()
    await publishing
    await terminal.drain()
    await asyncio.sleep(0)

    assert sink.calls == 2 and sink.records == []
    assert not ticket.audit_facts.published
    assert metrics.failures == ["desanitize"]


async def test_a_server_cancel_leaves_an_ok_published_before_the_close_ok() -> None:
    """Unlike a client that left, a server cancel does not turn ``ok`` into ``cancelled``:
    the response completed."""
    sink = _Sink()
    terminal = TerminalAudit(sink)
    ticket = _ticket(_facts())
    terminal.bind(ticket)
    ticket.mark_cancelled(CANCEL_SERVER)

    await terminal.publish(ticket, "ok")
    ticket.close()
    await terminal.drain()

    assert sink.outcomes == [("ok", None)]


async def test_a_server_cancel_closed_before_any_publish_is_the_servers_cancel() -> None:
    sink = _Sink()
    terminal = TerminalAudit(sink)
    ticket = _ticket(_facts())
    terminal.bind(ticket)
    ticket.mark_cancelled(CANCEL_SERVER)

    ticket.close()
    await terminal.publish(ticket, "ok")
    await terminal.drain()

    assert sink.outcomes == [("cancelled", E_SERVER_SHUTDOWN)]


async def test_without_facts_nothing_is_published() -> None:
    """A request whose pre-call refused it deposits nothing; its inline audit is its record."""
    sink = _Sink()
    terminal = TerminalAudit(sink)
    ticket = _ticket()
    terminal.bind(ticket)

    await terminal.publish(ticket, "ok")
    ticket.close()
    await terminal.drain()

    assert sink.records == []


@pytest.mark.parametrize("state", ["cancelled", "closed", "none"])
def test_facts_are_refused_for_a_cancelled_closed_or_missing_ticket(state: str) -> None:
    ticket: RequestTicket | None = RequestTicket("b" * 32)
    assert ticket is not None
    if state == "cancelled":
        ticket.cancelled = True
    elif state == "closed":
        ticket.close()
    else:
        ticket = None

    assert deposit(ticket, _facts()) is False
    if ticket is not None:
        assert ticket.audit_facts is None


async def test_usage_from_a_callback_counts_only_while_the_record_is_open() -> None:
    """Callback before the response: its token counts are in the record. Callback after:
    it neither changes the record nor writes another."""
    sink = _Sink()
    terminal = TerminalAudit(sink)
    early = _ticket(_facts(request_id="early"))
    late = _ticket(_facts(request_id="late"))

    assert deposit_usage(early, 7, 3)
    await terminal.publish(early, "ok")
    await terminal.publish(late, "ok")
    assert not deposit_usage(late, 7, 3)

    by_id = {r.facts.request_id: r.event() for r in sink.records}
    assert (by_id["early"].prompt_token_count, by_id["early"].completion_token_count) == (7, 3)
    assert (by_id["late"].prompt_token_count, by_id["late"].completion_token_count) == (0, 0)
    assert len(sink.records) == 2


def test_the_cancelled_code_is_the_guardrails_own() -> None:
    from corp_llm_gateway import litellm_hook

    assert litellm_hook.E_CLIENT_DISCONNECTED == E_CLIENT_DISCONNECTED


def test_the_record_passes_the_never_fields_gate_with_nothing_but_facts() -> None:
    event = TerminalRecord(_facts(), "ok", None, 12).event()

    assert event.placeholder_list is None
    assert event.latency_ms == 12


# ── the guardrail's deposit ──────────────────────────────────────────────────


async def _pre_call_in(ticket: RequestTicket, data: dict[str, object], guardrail: object) -> None:
    from corp_llm_gateway.route_gate.inflight import _TICKET

    token = _TICKET.set(ticket)
    try:
        await guardrail.pre_call(data)  # type: ignore[attr-defined]
    finally:
        _TICKET.reset(token)


async def test_the_terminal_record_carries_every_field_todays_record_carried() -> None:
    """The golden record: the same request, once outside the route gate (no ticket, so
    the guardrail's ``audit()`` still writes the record, as every request did before the
    desanitiser was wired) and once through a ticket (pre-call deposit, the log event's
    token counts, the terminal record). Same keys, same values, but the call id, the
    timestamp and the latency, which are present in both."""
    from datetime import UTC, datetime, timedelta

    from tests.test_litellm_hook import _build_guardrail, _data_with_token

    pairs = [("alice@corp.example", "[EMAIL_1]"), ("Bob", "[NAME_1]")]
    # Two guardrails built alike: one Cache A would make the second request a hit.
    golden_guardrail, golden_sink = _build_guardrail(pairs)
    guardrail, sink = _build_guardrail(pairs)
    usage = {"usage": {"prompt_tokens": 7, "completion_tokens": 2}}
    end = datetime.now(UTC)
    begin = end - timedelta(milliseconds=40)

    golden = _data_with_token("tok-1", content="mail alice@corp.example for Bob")
    golden["litellm_call_id"] = "call-golden"
    await golden_guardrail.pre_call(golden)
    await golden_guardrail.async_log_success_event(
        {"litellm_call_id": "call-golden"}, usage, begin, end
    )
    (today,) = golden_sink.records

    terminal_sink = ListSink()
    terminal = TerminalAudit(emit_to(AuditLogger(terminal_sink, gateway_version="0.0.1")))
    ticket = RequestTicket("c" * 32)
    data = _data_with_token("tok-1", content="mail alice@corp.example for Bob")
    data["litellm_call_id"] = "call-9"
    await _pre_call_in(ticket, data, guardrail)
    await guardrail.async_log_success_event({"litellm_call_id": "call-9"}, usage, begin, end)
    assert sink.records == [], "the guardrail wrote a record for a ticketed request"
    await terminal.publish(ticket, "ok")

    (record,) = terminal_sink.records
    varying = {"request_id", "timestamp", "latency_ms"}
    assert set(record) == set(today)
    assert {k: v for k, v in record.items() if k not in varying} == {
        k: v for k, v in today.items() if k not in varying
    }
    assert record["request_id"] == "call-9"
    assert isinstance(record["latency_ms"], int) and record["latency_ms"] >= 0
    assert (record["prompt_token_count"], record["completion_token_count"]) == (7, 2)
    assert record["redaction_count"] == 2 and record["status"] == "ok"
    facts = ticket.audit_facts
    assert "alice@corp.example" not in repr(facts) and "Bob" not in repr(facts)


async def test_a_refused_pre_call_deposits_nothing() -> None:
    from corp_llm_gateway.litellm_hook import GuardrailHttpException
    from tests.test_litellm_hook import _build_guardrail

    guardrail, _ = _build_guardrail()
    ticket = RequestTicket("d" * 32)

    with pytest.raises(GuardrailHttpException):
        await _pre_call_in(ticket, {"messages": [], "headers": {}}, guardrail)

    assert ticket.audit_facts is None


# ── through the limiter, under the production task factory ───────────────────


@pytest.mark.parametrize(
    ("who", "expected"),
    [
        ("server", ("cancelled", E_SERVER_SHUTDOWN)),
        ("client", ("cancelled", E_CLIENT_DISCONNECTED)),
    ],
)
async def test_a_cancelled_stream_is_cancelled_with_the_code_of_who_cancelled_it(
    who: str, expected: tuple[str, str]
) -> None:
    """A server-side cancel (shutdown, pod drain) is not a restoration failure."""
    restore = install_task_factory(asyncio.get_running_loop())
    sink = _Sink()
    terminal = TerminalAudit(sink)
    streaming = asyncio.Event()
    gone = asyncio.Event()
    served = {"n": 0}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        ticket = current_ticket()
        assert deposit(ticket, _facts()) and ticket is not None
        terminal.bind(ticket)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send({"type": "http.response.body", "body": b"data: x\n\n", "more_body": True})
        streaming.set()
        await asyncio.sleep(10)

    async def receive() -> dict[str, Any]:
        served["n"] += 1
        if served["n"] == 1:
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        return None

    async def refuse(reason: str) -> None:
        raise AssertionError(reason)

    limiter = InflightLimiter(0, metrics=NoopExporter(), cancel_grace_s=0.2)
    try:
        task = asyncio.create_task(
            limiter.run({"type": "http", "headers": []}, receive, send, app, refuse=refuse)
        )
        await asyncio.wait_for(streaming.wait(), 2)
        if who == "server":
            task.cancel()
        else:
            gone.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        await terminal.drain()
    finally:
        restore()

    assert sink.outcomes == [expected]


@pytest.mark.parametrize("after", ["final_body", "protocol_breach"])
async def test_a_server_cancel_after_the_watcher_returned_is_the_servers_cancel(
    after: str,
) -> None:
    """The limiter is past watching the client and waits for the downstream to finish (the
    response went out and the client hung up, or the client broke the protocol): a server
    cancel there is still marked before the downstream sees it, and before the close."""
    restore = install_task_factory(asyncio.get_running_loop())
    sink = _Sink()
    terminal = TerminalAudit(sink)
    watcher_done = asyncio.Event()
    seen_by_downstream: list[str | None] = []
    served = {"n": 0}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        ticket = current_ticket()
        assert deposit(ticket, _facts()) and ticket is not None
        terminal.bind(ticket)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        more = after != "final_body"
        await send({"type": "http.response.body", "body": b"data: x\n\n", "more_body": more})
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            seen_by_downstream.append(ticket.cancel_origin)
            raise

    async def receive() -> dict[str, Any]:
        served["n"] += 1
        if served["n"] == 1:
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await asyncio.sleep(0.01)
        watcher_done.set()
        if after == "final_body":
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Any) -> None:
        return None

    async def refuse(reason: str) -> None:
        raise AssertionError(reason)

    limiter = InflightLimiter(0, metrics=NoopExporter(), cancel_grace_s=0.2)
    try:
        task = asyncio.create_task(
            limiter.run({"type": "http", "headers": []}, receive, send, app, refuse=refuse)
        )
        await asyncio.wait_for(watcher_done.wait(), 2)
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        await terminal.drain()
    finally:
        restore()

    assert sink.outcomes == [("cancelled", E_SERVER_SHUTDOWN)]
    assert seen_by_downstream == [CANCEL_SERVER]


@pytest.mark.parametrize("first_fails", [True, False])
async def test_a_write_still_in_flight_when_the_limiter_lets_go_is_not_lost(
    first_fails: bool,
) -> None:
    """The client leaves while the response path's ``ok`` write awaits the sink and ignores
    the cancel; the limiter closes the ticket after the grace. A write that then fails is
    retried once with its own outcome, by a task the request does not own."""
    expected = [("ok", None)]
    restore = install_task_factory(asyncio.get_running_loop())
    tickets: list[RequestTicket] = []
    downstreams: list[asyncio.Task[Any]] = []
    sink = _Gated(first_fails=first_fails)
    terminal = TerminalAudit(sink)
    gone = asyncio.Event()
    served = {"n": 0}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await receive()
        ticket = current_ticket()
        assert deposit(ticket, _facts()) and ticket is not None
        tickets.append(ticket)
        sink.ticket = ticket
        task = asyncio.current_task()
        assert task is not None
        downstreams.append(task)
        terminal.bind(ticket)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: x\n\n", "more_body": True})
        await terminal.publish(ticket, "ok")

    async def receive() -> dict[str, Any]:
        served["n"] += 1
        if served["n"] == 1:
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(message: Any) -> None:
        return None

    async def refuse(reason: str) -> None:
        raise AssertionError(reason)

    limiter = InflightLimiter(0, metrics=NoopExporter(), cancel_grace_s=0.1)
    try:
        task = asyncio.create_task(
            limiter.run({"type": "http", "headers": []}, receive, send, app, refuse=refuse)
        )
        await asyncio.wait_for(sink.entered.wait(), 2)
        gone.set()
        await asyncio.wait_for(task, 2)
        (ticket,) = tickets
        assert ticket.closed and ticket.audit_facts.publishing
        sink.release.set()
        await asyncio.wait_for(asyncio.wait(downstreams), 2)
        await terminal.drain()
    finally:
        restore()

    assert sink.outcomes == expected
    assert sink.owned == [True, False] if first_fails else [True]


# ── the outcome policy, interleaving by interleaving ─────────────────────────
# The request decides the outcome, never the sink: a restoration failure stands, an
# outcome the response path published stands against a later cancel, otherwise the
# close decides; a publish after the close is refused; one retry, then the record is
# counted lost. Each case runs under the production task factory.


@pytest.fixture
async def task_factory() -> Any:
    restore = install_task_factory(asyncio.get_running_loop())
    try:
        yield
    finally:
        restore()


class _Scripted:
    """Each write parks on its own gate (when given), then fails or lands as scripted.
    ``shared`` says, per write, whether a task no request owns ran it."""

    def __init__(self, *outcomes: str, gated: tuple[int, ...] = ()) -> None:
        self.outcomes_script = list(outcomes)
        self.gates = {n: asyncio.Event() for n in gated}
        self.entered = {n: asyncio.Event() for n in gated}
        self.records: list[TerminalRecord] = []
        self.shared: list[bool] = []
        self.calls = 0

    async def __call__(self, record: TerminalRecord) -> None:
        from corp_llm_gateway.route_gate.inflight import _SHARED

        self.calls += 1
        n = self.calls
        self.shared.append(asyncio.current_task() in _SHARED)
        if n in self.gates:
            self.entered[n].set()
            await self.gates[n].wait()
        await asyncio.sleep(0)
        step = self.outcomes_script[n - 1] if n <= len(self.outcomes_script) else "land"
        if step == "fail":
            raise RuntimeError(CANARY)
        if step == "cancel":
            raise asyncio.CancelledError
        self.records.append(record)

    @property
    def outcomes(self) -> list[tuple[str, str | None]]:
        return [(r.outcome, r.error_code) for r in self.records]


def _bound(sink: Any, metrics: _Failures | None = None) -> tuple[TerminalAudit, RequestTicket]:
    terminal = TerminalAudit(sink, metrics=metrics or _Failures())
    ticket = _ticket(_facts())
    terminal.bind(ticket)
    return terminal, ticket


@pytest.mark.usefixtures("task_factory")
@pytest.mark.parametrize("first", ["fail", "land"])
async def test_a_publish_after_the_close_is_refused_and_the_close_writes(first: str) -> None:
    """Probe case 1 / pre-emption: the close schedules its write, a publish gets the loop
    first. The publish writes nothing, so whatever the sink does to a request-task write
    cannot touch the record: the close's write, from a shared task, is the only one."""
    sink = _Scripted(first)
    terminal, ticket = _bound(sink)
    ticket.mark_cancelled(CANCEL_CLIENT)

    ticket.close()
    await terminal.publish(ticket, "ok")
    await terminal.drain()

    assert sink.shared == [True]
    if first == "land":
        assert sink.outcomes == [("cancelled", E_CLIENT_DISCONNECTED)]
    else:
        assert sink.records == [] and not ticket.audit_facts.published


@pytest.mark.usefixtures("task_factory")
@pytest.mark.parametrize("retry", ["land", "fail"])
async def test_the_retry_behind_a_failed_in_flight_write_keeps_its_outcome(
    retry: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Probe cases 2 and 3: the ``ok`` write in flight fails after the close, so the retry
    starts from a shared task; a publish that gets the loop first is refused. The retry
    writes ``ok``; if it fails too, the record is lost: one line and one count."""
    metrics = _Failures()
    sink = _Scripted("fail", retry, gated=(1,))
    terminal, ticket = _bound(sink, metrics)
    publishing = asyncio.create_task(terminal.publish(ticket, "ok"))
    await asyncio.wait_for(sink.entered[1].wait(), 2)

    ticket.mark_cancelled(CANCEL_CLIENT)
    ticket.close()
    sink.gates[1].set()
    with caplog.at_level(logging.DEBUG):
        await publishing
        await terminal.publish(ticket, "ok")
        await terminal.drain()

    assert sink.calls == 2 and sink.shared == [False, True]
    if retry == "land":
        assert sink.outcomes == [("ok", None)] and metrics.failures == []
    else:
        assert sink.records == [] and metrics.failures == ["desanitize"]
        assert caplog.text.count("gateway_terminal_audit_lost") == 1
    assert CANARY not in caplog.text


@pytest.mark.usefixtures("task_factory")
async def test_a_second_close_and_a_late_publish_add_nothing() -> None:
    """Probe case 4."""
    sink = _Scripted("fail", gated=(1,))
    terminal, ticket = _bound(sink)
    publishing = asyncio.create_task(terminal.publish(ticket, "ok"))
    await asyncio.wait_for(sink.entered[1].wait(), 2)

    ticket.mark_cancelled(CANCEL_CLIENT)
    ticket.close()
    ticket.close()
    sink.gates[1].set()
    await publishing
    await terminal.drain()
    await terminal.publish(ticket, "failed", error_code=E_INTERNAL)
    await terminal.drain()

    assert sink.outcomes == [("ok", None)] and sink.calls == 2


@pytest.mark.usefixtures("task_factory")
@pytest.mark.parametrize("who", [CANCEL_SERVER, CANCEL_CLIENT])
async def test_an_ok_whose_write_fails_after_a_later_cancel_stays_ok(who: str) -> None:
    """Probe case 5 (server) and its client twin: ``ok`` was published at the final body,
    the cancel came later, the write failed. The retry writes ``ok``."""
    sink = _Scripted("fail", gated=(1,))
    terminal, ticket = _bound(sink)
    publishing = asyncio.create_task(terminal.publish(ticket, "ok"))
    await asyncio.wait_for(sink.entered[1].wait(), 2)

    ticket.mark_cancelled(who)
    ticket.close()
    sink.gates[1].set()
    await publishing
    await terminal.drain()

    assert sink.outcomes == [("ok", None)]


@pytest.mark.usefixtures("task_factory")
@pytest.mark.parametrize("who", [CANCEL_SERVER, CANCEL_CLIENT])
async def test_a_restoration_failure_whose_write_fails_stays_a_restoration_failure(
    who: str,
) -> None:
    """Probe case 6: ``failed`` + ``E_INTERNAL`` wins whatever happens afterwards."""
    sink = _Scripted("fail", gated=(1,))
    terminal, ticket = _bound(sink)
    publishing = asyncio.create_task(terminal.publish(ticket, "failed", error_code=E_INTERNAL))
    await asyncio.wait_for(sink.entered[1].wait(), 2)

    ticket.mark_cancelled(who)
    ticket.close()
    sink.gates[1].set()
    await publishing
    await terminal.drain()

    assert sink.outcomes == [("failed", E_INTERNAL)]


@pytest.mark.usefixtures("task_factory")
async def test_a_cancelled_write_is_retried_by_the_close_with_its_outcome() -> None:
    """Probe case 7: the request task writing ``ok`` is cancelled mid-write (a server
    cancel), then the limiter closes the ticket: the close retries ``ok``."""
    sink = _Scripted("cancel")
    terminal, ticket = _bound(sink)

    with contextlib.suppress(asyncio.CancelledError):
        await terminal.publish(ticket, "ok")
    ticket.mark_cancelled(CANCEL_SERVER)
    ticket.close()
    await terminal.drain()

    assert sink.outcomes == [("ok", None)] and sink.shared == [False, True]


@pytest.mark.usefixtures("task_factory")
async def test_drain_waits_for_a_write_in_flight_and_the_retry_it_leaves() -> None:
    """Probe case 9: ``drain`` does not return while a publish is still writing."""
    sink = _Scripted("fail", gated=(1,))
    terminal, ticket = _bound(sink)
    publishing = asyncio.create_task(terminal.publish(ticket, "ok"))
    await asyncio.wait_for(sink.entered[1].wait(), 2)
    ticket.close()

    draining = asyncio.create_task(terminal.drain())
    await asyncio.sleep(0.02)
    assert not draining.done() and terminal.pending == 1

    sink.gates[1].set()
    await asyncio.wait_for(draining, 2)
    await publishing

    assert sink.outcomes == [("ok", None)] and terminal.pending == 0


@pytest.mark.usefixtures("task_factory")
@pytest.mark.parametrize("then", ["close", "publish_ok", "client_cancel"])
async def test_a_restoration_failure_decided_before_its_answer_stands(then: str) -> None:
    """The middleware decides ``failed`` + ``E_INTERNAL`` the moment restoration fails,
    before it sends anything: if that send fails, whatever publishes or closes next
    writes the decided outcome."""
    sink = _Scripted()
    terminal, ticket = _bound(sink)

    terminal.decide(ticket, "failed", error_code=E_INTERNAL)
    if then == "publish_ok":
        await terminal.publish(ticket, "ok")
    elif then == "client_cancel":
        ticket.mark_cancelled(CANCEL_CLIENT)
    ticket.close()
    await terminal.drain()

    assert sink.outcomes == [("failed", E_INTERNAL)]


async def test_decide_never_overrides_an_outcome_already_decided() -> None:
    terminal, ticket = _bound(_Scripted())

    terminal.decide(ticket, "ok")
    terminal.decide(ticket, "failed", error_code=E_INTERNAL)

    assert ticket.audit_facts.decided == ("ok", None)
