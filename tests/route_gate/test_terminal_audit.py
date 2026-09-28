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


async def test_the_close_write_has_no_retry_of_its_own() -> None:
    sink = _Sink(RuntimeError("sink down"), RuntimeError("sink down"))
    terminal = TerminalAudit(sink)
    ticket = _ticket(_facts())
    terminal.bind(ticket)

    ticket.close()
    await terminal.drain()
    await asyncio.sleep(0)

    assert len(sink.failures) == 1 and sink.records == []
    assert not ticket.audit_facts.published


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


async def test_the_pre_call_deposits_the_counts_todays_audit_record_carries() -> None:
    from datetime import UTC, datetime

    from tests.test_litellm_hook import _build_guardrail, _data_with_token

    guardrail, sink = _build_guardrail([("alice@corp.example", "[EMAIL_1]"), ("Bob", "[NAME_1]")])
    ticket = RequestTicket("c" * 32)
    data = _data_with_token("tok-1", content="mail alice@corp.example for Bob")
    data["litellm_call_id"] = "call-9"

    await _pre_call_in(ticket, data, guardrail)
    now = datetime.now(UTC)
    await guardrail.audit(data, None, now, now, status="ok")

    facts = ticket.audit_facts
    (record,) = sink.records
    assert isinstance(facts, AuditFacts)
    assert facts.request_id == record["request_id"] == "call-9"
    assert (facts.user_id, facts.team_id) == (record["user_id"], record["team_id"])
    assert facts.redaction_count == record["redaction_count"] == 2
    assert facts.finding_label_counts == record["finding_label_counts"]
    assert facts.status == "ok" and facts.error_code is None
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
