"""The terminal audit record of a ticketed request: facts in, one record out.

The guardrail's pre-call deposits content-free facts on the request's ``RequestTicket``
(identity, counts, the pre-call's own outcome, the latency start); token counts are added
while the record is still open, from the response or from a success log. A callback
never publishes. The one record is written from the facts by the response-restoring
middleware (``publish``) or by the ticket's close.

The request decides the outcome, never the sink, in this order:

1. ``failed`` + ``E_INTERNAL`` published for a restoration failure stands, whatever
   happens afterwards.
2. An outcome the response path published (``ok`` at the final body; ``failed`` for a
   non-2xx response or a stream that carried an error event) stands against any later
   cancel, client or server: the client got the response. One published once the client
   had left is ``cancelled`` + ``E_CLIENT_DISCONNECTED``: the client got nothing.
3. Otherwise the close decides: ``cancelled`` + ``E_CLIENT_DISCONNECTED`` or
   ``E_SERVER_SHUTDOWN`` by who cancelled, ``failed`` + ``E_INTERNAL`` when nothing was
   published and nobody cancelled.

A publish after the close is refused, so the close's write is the last attempt. A failed
write never changes the decided outcome; it gets exactly one retry. A write the response
path started that fails before the close is retried by the close; one still in flight
when the close lands is retried, if it fails, by a write the close leaves behind for it.
The close's own write is not retried. A record lost after its last attempt is logged by
exception type and counted as ``gateway_failure{component="desanitize"}``. A write that
may have landed (``AuditWriteAmbiguousError``) is never retried.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from corp_llm_gateway.audit import AuditEvent, AuditLogger, AuditWriteAmbiguousError
from corp_llm_gateway.audit.event import Provider
from corp_llm_gateway.metrics import MetricsExporter, get_exporter
from corp_llm_gateway.route_gate.inflight import CANCEL_SERVER, RequestTicket, spawn_shared

logger = logging.getLogger(__name__)

Outcome = Literal["ok", "failed", "cancelled"]

E_INTERNAL = "E_INTERNAL"
# Same code as the guardrail's own cancelled record (litellm_hook.E_CLIENT_DISCONNECTED).
E_CLIENT_DISCONNECTED = "E_CLIENT_DISCONNECTED"
# The server cancelled the request (shutdown, pod drain): not a restoration failure.
E_SERVER_SHUTDOWN = "E_SERVER_SHUTDOWN"
# gateway_failure{component} of a terminal record lost after its last attempt.
COMPONENT = "desanitize"


@dataclass
class AuditFacts:
    """What a terminal record is written from: identity, counts, codes. Never content."""

    request_id: str
    user_id: str
    team_id: str
    provider: Provider
    model: str
    redaction_count: int = 0
    finding_label_counts: dict[str, int] = field(default_factory=dict)
    cache_a_hit: bool = False
    block_reason: str | None = None
    error_code: str | None = None
    profile_ids: tuple[str, ...] = ()
    # The request's placeholder tokens (e.g. ``[EMAIL_1]``), never an original.
    placeholders: tuple[str, ...] = ()
    status: Outcome = "ok"
    started: float = field(default_factory=time.monotonic)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    decided: tuple[Outcome, str | None] | None = field(default=None, init=False)
    published: bool = field(default=False, init=False)
    publishing: bool = field(default=False, init=False)
    attempts: int = field(default=0, init=False)
    # The close landed while a write was in flight: that write's failure is retried.
    retry_on_failure: bool = field(default=False, init=False)


@dataclass(frozen=True)
class TerminalRecord:
    facts: AuditFacts
    outcome: Outcome
    error_code: str | None
    latency_ms: int

    def event(self) -> AuditEvent:
        facts = self.facts
        # A cancelled record carries counts only, as the guardrail's own does.
        placeholders = facts.placeholders if self.outcome != "cancelled" else ()
        return AuditEvent(
            timestamp=datetime.now(UTC),
            request_id=facts.request_id,
            user_id=facts.user_id,
            team_id=facts.team_id,
            provider=facts.provider,
            model=facts.model,
            latency_ms=self.latency_ms,
            prompt_token_count=facts.prompt_tokens,
            completion_token_count=facts.completion_tokens,
            redaction_count=facts.redaction_count,
            finding_label_counts=dict(facts.finding_label_counts),
            cache_a_hit=facts.cache_a_hit,
            status=self.outcome,
            placeholder_list=tuple(sorted(placeholders)) if placeholders else None,
            error_code=self.error_code,
            block_reason=facts.block_reason,
            profile_ids=facts.profile_ids,
        )


Emit = Callable[[TerminalRecord], Awaitable[None]]


def emit_to(audit_logger: AuditLogger) -> Emit:
    """An ``Emit`` writing through the audit logger (and so its NEVER-fields gate)."""

    async def emit(record: TerminalRecord) -> None:
        await audit_logger.emit(record.event())

    return emit


def deposit(ticket: RequestTicket | None, facts: AuditFacts) -> bool:
    """Put ``facts`` on the ticket; refused (False, content-free log) once it is closed
    or cancelled, or when there is no ticket. Never raises."""
    if ticket is None:
        return False
    if ticket.closed or ticket.cancelled:
        logger.info(
            "gateway_audit_facts_refused request_id=%s reason=%s",
            ticket.gateway_id,
            "closed" if ticket.closed else "cancelled",
        )
        return False
    ticket.audit_facts = facts
    return True


def deposit_usage(ticket: RequestTicket | None, prompt_tokens: int, completion_tokens: int) -> bool:
    """Token counts for the record; kept only while it is still open. Each deposit
    replaces the last: counts are never summed."""
    facts = ticket.audit_facts if ticket is not None else None
    if not isinstance(facts, AuditFacts) or facts.published or facts.publishing:
        return False
    facts.prompt_tokens = max(0, int(prompt_tokens))
    facts.completion_tokens = max(0, int(completion_tokens))
    return True


class TerminalAudit:
    """Publishes each ticket's terminal record exactly once."""

    def __init__(self, emit: Emit, *, metrics: MetricsExporter | None = None) -> None:
        self._emit = emit
        self._metrics = metrics
        self._closing: set[asyncio.Task[None]] = set()
        self._writing: set[asyncio.Event] = set()

    @property
    def pending(self) -> int:
        """Writes not finished yet: the response path's, and those the closes started
        (a retry included)."""
        return len(self._closing) + len(self._writing)

    def bind(self, ticket: RequestTicket) -> None:
        """Publish at the ticket's close whatever the response path did not."""
        ticket.on_close(self._on_close)

    def decide(
        self, ticket: RequestTicket, outcome: Outcome, *, error_code: str | None = None
    ) -> None:
        """Decide the record's outcome now, if nothing has, and leave the write to a
        ``publish`` or the close: a restoration failure is the outcome even when the
        answer to it never reaches the client."""
        facts = ticket.audit_facts
        if isinstance(facts, AuditFacts) and facts.decided is None and not ticket.closed:
            facts.decided = (outcome, error_code if error_code is not None else facts.error_code)

    async def publish(
        self, ticket: RequestTicket, outcome: Outcome, *, error_code: str | None = None
    ) -> None:
        """Decide the record's outcome if nothing has, then write it; refused once the
        ticket is closed. Never raises an ``Exception``: a failed write is left for the
        close to retry."""
        facts = ticket.audit_facts
        if not isinstance(facts, AuditFacts) or ticket.closed:
            return
        if facts.decided is None:
            restoration_failure = outcome == "failed" and error_code == E_INTERNAL
            if ticket.cancelled and not restoration_failure:
                outcome, error_code = "cancelled", E_CLIENT_DISCONNECTED
            facts.decided = (outcome, error_code if error_code is not None else facts.error_code)
        if facts.attempts == 0:
            await self._write(ticket, facts, last=False)

    async def drain(self) -> None:
        """Wait for every write started so far, and the retries they leave behind."""
        while self._closing or self._writing:
            if self._closing:
                await asyncio.wait(set(self._closing))
            else:
                await next(iter(self._writing)).wait()

    async def _write(self, ticket: RequestTicket, facts: AuditFacts, *, last: bool) -> None:
        if facts.published or facts.publishing or facts.decided is None:
            return
        decided, code = facts.decided
        latency_ms = max(0, int((time.monotonic() - facts.started) * 1000))
        record = TerminalRecord(facts, decided, code, latency_ms)
        facts.publishing = True
        facts.attempts += 1
        # A close's write is tracked by its task; the response path's by this event.
        writing = asyncio.Event()
        if not last:
            self._writing.add(writing)
        failure: type[BaseException] | None = None
        try:
            await self._emit(record)
        except AuditWriteAmbiguousError:
            # It may have landed: a second write could duplicate it.
            facts.published = True
            logger.error(
                "gateway_terminal_audit_ambiguous request_id=%s outcome=%s",
                ticket.gateway_id,
                decided,
            )
        except Exception as exc:
            failure = type(exc)
        except BaseException as exc:
            failure = type(exc)
            raise
        else:
            facts.published = True
        finally:
            facts.publishing = False
            retry, facts.retry_on_failure = facts.retry_on_failure, False
            try:
                if not facts.published:
                    self._not_written(ticket, facts, decided, failure, last=last, retry=retry)
            finally:
                self._writing.discard(writing)
                writing.set()

    def _not_written(
        self,
        ticket: RequestTicket,
        facts: AuditFacts,
        outcome: str,
        failure: type[BaseException] | None,
        *,
        last: bool,
        retry: bool,
    ) -> None:
        error = failure.__name__ if failure is not None else "unknown"
        if last:
            if failure is not None and not issubclass(failure, (Exception, asyncio.CancelledError)):
                # Escapes the task: ``_closed`` logs and counts it.
                return
            self._lost(ticket, outcome, error)
            return
        # Type only: an exception message can quote request content.
        logger.error(
            "gateway_terminal_audit_failed request_id=%s outcome=%s error=%s",
            ticket.gateway_id,
            outcome,
            error,
        )
        if retry:
            # The close already came: the retry cannot wait for it.
            self._schedule(ticket, facts)

    def _lost(self, ticket: RequestTicket, outcome: str, error: str) -> None:
        # Type only: an exception message can quote request content.
        logger.error(
            "gateway_terminal_audit_lost request_id=%s outcome=%s error=%s",
            ticket.gateway_id,
            outcome,
            error,
        )
        self._count_failure()

    def _count_failure(self) -> None:
        try:
            (self._metrics or get_exporter()).record_failure(COMPONENT)
        except Exception as metrics_exc:
            logger.error("terminal_audit_metrics_error error=%s", type(metrics_exc).__name__)

    def _on_close(self, ticket: RequestTicket) -> None:
        facts = ticket.audit_facts
        if not isinstance(facts, AuditFacts) or facts.published:
            return
        if facts.decided is None:
            facts.decided = _closing_outcome(ticket)
        if facts.publishing:
            # The write in flight is the response path's; its failure gets the retry.
            facts.retry_on_failure = True
            return
        self._schedule(ticket, facts)

    def _schedule(self, ticket: RequestTicket, facts: AuditFacts) -> None:
        # Owned by no request: the write starts from the request's own task.
        task = spawn_shared(self._write(ticket, facts, last=True))
        self._closing.add(task)
        task.add_done_callback(lambda done: self._closed(ticket, done))

    def _closed(self, ticket: RequestTicket, task: asyncio.Task[None]) -> None:
        self._closing.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None or isinstance(exc, Exception):
            return
        # Type only: an exception message can quote request content.
        logger.error(
            "terminal_audit_publish_failed request_id=%s error=%s",
            ticket.gateway_id,
            type(exc).__name__,
        )
        self._count_failure()


def _closing_outcome(ticket: RequestTicket) -> tuple[Outcome, str]:
    """The outcome of a request the limiter let go of before any final body was published."""
    if ticket.cancelled:
        return "cancelled", E_CLIENT_DISCONNECTED
    if ticket.cancel_origin == CANCEL_SERVER:
        return "cancelled", E_SERVER_SHUTDOWN
    # The response never completed and nobody cancelled it.
    return "failed", E_INTERNAL
