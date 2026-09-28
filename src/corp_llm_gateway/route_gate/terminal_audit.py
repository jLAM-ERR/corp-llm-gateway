"""The terminal audit record of a ticketed request: facts in, one record out.

Callbacks deposit content-free facts on the request's ``RequestTicket`` (the guardrail's
pre-call: identity, counts, the pre-call's own outcome, the latency start; a success log
may add token counts while the record is still open). The one terminal record is
published from them by whoever ends the response: the response-restoring middleware at
its final body or restoration failure, or the ticket's close when the limiter lets go of
a request that never got that far (cancelled, or ended without a final body). A callback
awaiting the response never publishes, so litellm running its success log before or
after ``http.response.start`` cannot decide the record.

The first outcome decided is the record's; ``ok`` on a ticket whose client left is
``cancelled``. The close decides before it schedules its write, so nothing published after
the close changes the outcome. A cancelled request is ``cancelled`` with the code of who
cancelled it (``E_CLIENT_DISCONNECTED``, ``E_SERVER_SHUTDOWN``); ``failed`` + ``E_INTERNAL``
is a restoration failure or a request that ended with no final body. A failed write before
the close keeps the record open and the close writes it once more; the close's own write is
the last attempt, never retried.
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
from corp_llm_gateway.route_gate.inflight import CANCEL_SERVER, RequestTicket

logger = logging.getLogger(__name__)

Outcome = Literal["ok", "failed", "cancelled"]

E_INTERNAL = "E_INTERNAL"
# Same code as the guardrail's own cancelled record (litellm_hook.E_CLIENT_DISCONNECTED).
E_CLIENT_DISCONNECTED = "E_CLIENT_DISCONNECTED"
# The server cancelled the request (shutdown, pod drain): not a restoration failure.
E_SERVER_SHUTDOWN = "E_SERVER_SHUTDOWN"
# gateway_failure{component} of a close's record write that raised past its own handling.
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
    status: Outcome = "ok"
    started: float = field(default_factory=time.monotonic)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    decided: tuple[Outcome, str | None] | None = field(default=None, init=False)
    published: bool = field(default=False, init=False)
    publishing: bool = field(default=False, init=False)


@dataclass(frozen=True)
class TerminalRecord:
    facts: AuditFacts
    outcome: Outcome
    error_code: str | None
    latency_ms: int

    def event(self) -> AuditEvent:
        facts = self.facts
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
    """Token counts from a success log; kept only while the record is still open."""
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

    def bind(self, ticket: RequestTicket) -> None:
        """Publish at the ticket's close whatever the response path did not."""
        ticket.on_close(self._on_close)

    async def publish(
        self, ticket: RequestTicket, outcome: Outcome, *, error_code: str | None = None
    ) -> None:
        """Write the record if still open; the first outcome decided sticks. Never raises
        an ``Exception``: a failed write is logged by type and left open for a retry."""
        facts = ticket.audit_facts
        if not isinstance(facts, AuditFacts) or facts.published or facts.publishing:
            return
        if facts.decided is None:
            if outcome == "ok" and ticket.cancelled:
                outcome, error_code = "cancelled", E_CLIENT_DISCONNECTED
            facts.decided = (outcome, error_code if error_code is not None else facts.error_code)
        await self._write(ticket, facts)

    async def drain(self) -> None:
        """Wait for the records the ticket closes started."""
        while self._closing:
            await asyncio.wait(set(self._closing))

    async def _write(self, ticket: RequestTicket, facts: AuditFacts) -> None:
        if facts.published or facts.publishing or facts.decided is None:
            return
        decided, code = facts.decided
        latency_ms = max(0, int((time.monotonic() - facts.started) * 1000))
        record = TerminalRecord(facts, decided, code, latency_ms)
        facts.publishing = True
        try:
            await self._emit(record)
        except AuditWriteAmbiguousError:
            # It may have landed: a second write could duplicate it.
            facts.published = True
            self._failed(ticket, decided, AuditWriteAmbiguousError)
        except Exception as exc:
            self._failed(ticket, decided, type(exc))
        else:
            facts.published = True
        finally:
            facts.publishing = False

    def _on_close(self, ticket: RequestTicket) -> None:
        facts = ticket.audit_facts
        if not isinstance(facts, AuditFacts) or facts.published or facts.publishing:
            return
        if facts.decided is None:
            facts.decided = _closing_outcome(ticket)
        task = asyncio.get_running_loop().create_task(self._write(ticket, facts))
        self._closing.add(task)
        task.add_done_callback(lambda done: self._closed(ticket, done))

    def _closed(self, ticket: RequestTicket, task: asyncio.Task[None]) -> None:
        self._closing.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        # Type only: an exception message can quote request content.
        logger.error(
            "terminal_audit_publish_failed request_id=%s error=%s",
            ticket.gateway_id,
            type(exc).__name__,
        )
        try:
            (self._metrics or get_exporter()).record_failure(COMPONENT)
        except Exception as metrics_exc:
            logger.error("terminal_audit_metrics_error error=%s", type(metrics_exc).__name__)

    @staticmethod
    def _failed(ticket: RequestTicket, outcome: str, error: type[BaseException]) -> None:
        logger.error(
            "gateway_terminal_audit_failed request_id=%s outcome=%s error=%s",
            ticket.gateway_id,
            outcome,
            error.__name__,
        )


def _closing_outcome(ticket: RequestTicket) -> tuple[Outcome, str]:
    """The outcome of a request the limiter let go of before any final body was published."""
    if ticket.cancelled:
        return "cancelled", E_CLIENT_DISCONNECTED
    if ticket.cancel_origin == CANCEL_SERVER:
        return "cancelled", E_SERVER_SHUTDOWN
    # The response never completed and nobody cancelled it.
    return "failed", E_INTERNAL
