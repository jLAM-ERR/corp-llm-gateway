"""The gateway-owned in-flight cap, and what it owns besides the count.

litellm 1.101.0 has no instance-wide cap on its default limiter, so the cap sits
here, behind the route gate's verdict: only an armed REWRITTEN request takes a
slot, and it holds the slot for the whole request, stream included.

Owning the slot means owning its end. uvicorn does not cancel an app whose
client has gone (``h11_impl.py``: it marks the cycle disconnected and answers
``http.disconnect`` from ``receive``), and litellm only watches for a disconnect
once its own monitor is armed, after our pre-call hook. So the limiter is the
single active reader of the real ``receive``: it drains the body up front,
replays it to the downstream from a buffer, and watches the real ``receive`` for
the rest of the request. On a disconnect before the response completed it
cancels the downstream, waits a bounded grace, cancels any task the request
spawned that is still pending, tells the guardrail, and frees the slot.

Tasks belong to a request through :class:`RequestTicket`, carried in a
contextvar. The task factory (:func:`install_task_factory`) tags a new task only
when the task creating it already belongs to the request, so a long-lived task
that merely runs in a copy of the request's context (litellm's logging worker
runs callbacks that way) is never cancelled with it.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, MutableMapping
from contextvars import ContextVar
from typing import Any, Protocol

from corp_llm_gateway.metrics import MetricsExporter

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
Refuse = Callable[[str], Awaitable[None]]

# block_reason / error code of the capacity refusal.
ROUTE_GATE_CAPACITY = "capacity"
E_CAPACITY = "E_CAPACITY"
# The oversize outcome the hook already gives a leaf past its threshold.
OVERSIZE_BLOCKED = "oversize:blocked"
# Every block_reason the gate answers with on the limiter's behalf.
LIMITER_BLOCK_REASONS: frozenset[str] = frozenset({ROUTE_GATE_CAPACITY, OVERSIZE_BLOCKED})

# nginx's client_max_body_size. The 100 KiB payload threshold is per text leaf,
# not a body cap: a real Claude Code body is routinely larger.
MAX_BODY_BYTES = 25 * 1024 * 1024
DEFAULT_CANCEL_GRACE_S = 5.0

_COMPONENT = "route_gate"


class CancelHook(Protocol):
    def __call__(self, request_id: str, *, latency_ms: int = ...) -> Awaitable[None]: ...


class RequestTicket:
    """One admitted request: its gateway id, the call ids bound to it, its tasks.

    ``cancelled`` is set before the downstream is cancelled, so any audit the
    unwinding request (or a litellm callback run in its context) attempts can
    stand down for the one ``cancelled`` record the guardrail writes after.
    """

    __slots__ = ("__weakref__", "call_ids", "cancelled", "gateway_id", "tasks")

    def __init__(self, gateway_id: str) -> None:
        self.gateway_id = gateway_id
        self.call_ids: list[str] = []
        self.cancelled = False
        self.tasks: weakref.WeakSet[asyncio.Task[Any]] = weakref.WeakSet()

    def request_ids(self) -> list[str]:
        return list(self.call_ids) or [self.gateway_id]

    def pending(self) -> list[asyncio.Task[Any]]:
        return [task for task in list(self.tasks) if not task.done()]


_TICKET: ContextVar[RequestTicket | None] = ContextVar("corp_llm_gateway_request", default=None)
_TAGGED: weakref.WeakKeyDictionary[asyncio.Task[Any], RequestTicket] = weakref.WeakKeyDictionary()


def current_ticket() -> RequestTicket | None:
    return _TICKET.get()


def bind_call_id(call_id: str) -> None:
    """Record litellm's per-call id on the request being served; no-op outside one."""
    ticket = _TICKET.get()
    if ticket is not None and call_id not in ticket.call_ids:
        ticket.call_ids.append(call_id)


def pending_request_tasks() -> list[asyncio.Task[Any]]:
    """Every still-pending task tagged to some request."""
    return [task for task in list(_TAGGED.keys()) if not task.done()]


def _tag(task: asyncio.Task[Any], ticket: RequestTicket) -> None:
    ticket.tasks.add(task)
    _TAGGED[task] = ticket


def install_task_factory(loop: asyncio.AbstractEventLoop) -> Callable[[], None]:
    """Tag tasks created inside a request's task tree; returns the uninstaller."""
    previous = loop.get_task_factory()

    def factory(loop_: asyncio.AbstractEventLoop, coro: Any, **kwargs: Any) -> Any:
        if previous is not None:
            task = previous(loop_, coro, **kwargs)
        else:
            task = asyncio.Task(coro, loop=loop_, **kwargs)
        context = kwargs.get("context")
        ticket = context.get(_TICKET) if context is not None else _TICKET.get()
        if ticket is not None:
            creator = asyncio.current_task(loop_)
            if creator is not None and creator in ticket.tasks:
                _tag(task, ticket)
        return task

    loop.set_task_factory(factory)

    def restore() -> None:
        if loop.get_task_factory() is factory:
            loop.set_task_factory(previous)

    return restore


class _Replay:
    """The downstream's ``receive``: the drained body, then the disconnect."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = deque(chunks)
        self._body_done = False
        self._disconnected = asyncio.Event()

    @property
    def disconnected(self) -> bool:
        return self._disconnected.is_set()

    def disconnect(self) -> None:
        self._disconnected.set()

    async def receive(self) -> Message:
        if not self._body_done and not self._disconnected.is_set():
            body = self._chunks.popleft() if self._chunks else b""
            more = bool(self._chunks)
            self._body_done = not more
            return {"type": "http.request", "body": body, "more_body": more}
        await self._disconnected.wait()
        return {"type": "http.disconnect"}


_DISCONNECTED = object()
_OVERSIZE = object()


class InflightLimiter:
    """Cap on concurrent admitted requests (0 = no cap) with disconnect-aware release."""

    def __init__(
        self,
        max_inflight: int,
        *,
        metrics: MetricsExporter,
        cancel_grace_s: float = DEFAULT_CANCEL_GRACE_S,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> None:
        if isinstance(max_inflight, bool) or not isinstance(max_inflight, int) or max_inflight < 0:
            raise ValueError("max_inflight must be a non-negative integer")
        if not (math.isfinite(cancel_grace_s) and cancel_grace_s > 0):
            raise ValueError("cancel_grace_s must be a positive finite number")
        self._max = max_inflight
        self._metrics = metrics
        self._grace = float(cancel_grace_s)
        self._max_body = max_body_bytes
        self._inflight = 0
        self._cancel_hook: CancelHook | None = None

    @property
    def max_inflight(self) -> int:
        return self._max

    @property
    def cancel_grace_s(self) -> float:
        return self._grace

    @property
    def inflight(self) -> int:
        return self._inflight

    def bind_cancel_hook(self, hook: CancelHook | None) -> None:
        self._cancel_hook = hook

    def try_acquire(self) -> bool:
        if self._max and self._inflight >= self._max:
            return False
        self._inflight += 1
        self._metrics.set_inflight(self._inflight)
        return True

    def _release(self) -> None:
        self._inflight -= 1
        self._metrics.set_inflight(self._inflight)

    async def run(
        self, scope: Scope, receive: Receive, send: Send, app: ASGIApp, *, refuse: Refuse
    ) -> None:
        """Serve one request that already holds a slot; frees it exactly once."""
        started = time.monotonic()
        ticket = RequestTicket(uuid.uuid4().hex)
        try:
            drained = await self._drain(receive)
            if drained is _OVERSIZE:
                await refuse(OVERSIZE_BLOCKED)
                return
            if drained is _DISCONNECTED:
                await self._cancelled(ticket, started, downstream=None)
                return
            assert isinstance(drained, list)
            await self._serve(ticket, started, scope, receive, send, app, drained)
        finally:
            self._release()

    async def _drain(self, receive: Receive) -> object:
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            kind = message.get("type")
            if kind != "http.request":
                # http.disconnect, or a message ASGI does not allow here.
                return _DISCONNECTED
            body = message.get("body") or b""
            total += len(body)
            if total > self._max_body:
                return _OVERSIZE
            if body:
                chunks.append(bytes(body))
            if not message.get("more_body", False):
                return chunks

    async def _serve(
        self,
        ticket: RequestTicket,
        started: float,
        scope: Scope,
        receive: Receive,
        send: Send,
        app: ASGIApp,
        chunks: list[bytes],
    ) -> None:
        replay = _Replay(chunks)
        # The replay owns the body now; each chunk is freed once it is delivered.
        chunks.clear()
        response_complete = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_complete
            # Before the send: a server that yields inside it must not let the
            # watcher read the post-response disconnect as a client abort.
            if message.get("type") == "http.response.body" and not message.get("more_body", False):
                response_complete = True
            await send(message)

        downstream = self._start(ticket, app(scope, replay.receive, tracked_send))

        def client_gone() -> None:
            # Before the replay wakes anything: litellm's own watchers read the
            # replay, and the audits they trigger must already see the flag.
            if not response_complete:
                ticket.cancelled = True
            replay.disconnect()

        watcher = asyncio.create_task(self._watch(receive, client_gone))
        try:
            await asyncio.wait({downstream, watcher}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            watcher.cancel()
            downstream.cancel()
            await asyncio.wait({downstream}, timeout=self._grace)
            raise
        if replay.disconnected and not response_complete:
            await self._cancelled(ticket, started, downstream=downstream)
            await _settle(watcher)
            return
        watcher.cancel()
        await _settle(watcher)
        await downstream

    @staticmethod
    def _start(ticket: RequestTicket, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        token = _TICKET.set(ticket)
        try:
            task = asyncio.create_task(coro)
        finally:
            _TICKET.reset(token)
        _tag(task, ticket)
        return task

    @staticmethod
    async def _watch(receive: Receive, client_gone: Callable[[], None]) -> None:
        # After the body, ASGI allows exactly one more message: http.disconnect.
        try:
            message = await receive()
        except Exception as exc:
            # A server whose receive fails has lost the connection as far as
            # this request can tell; stop spending upstream on it.
            logger.warning("route_gate_receive_failed error=%s", type(exc).__name__)
            client_gone()
            return
        if message.get("type") == "http.disconnect":
            client_gone()
            return
        # Anything else breaks the protocol; stop watching rather than spin on it.
        logger.warning("route_gate_unexpected_receive_after_body")

    async def _cancelled(
        self, ticket: RequestTicket, started: float, *, downstream: asyncio.Task[None] | None
    ) -> None:
        ticket.cancelled = True
        unwound = True
        if downstream is not None:
            downstream.cancel()
            await asyncio.wait({downstream}, timeout=self._grace)
            unwound = downstream.done()
            if unwound and not downstream.cancelled():
                # The client is gone: whatever the downstream raised on its way
                # out has no one to answer to.
                exc = downstream.exception()
                if exc is not None:
                    logger.info(
                        "route_gate_cancelled_downstream_error error=%s", type(exc).__name__
                    )
        stragglers = [task for task in ticket.pending() if task is not downstream]
        for task in stragglers:
            task.cancel()
        if stragglers:
            await asyncio.wait(stragglers, timeout=self._grace)
        left = [task for task in ticket.pending() if task is not downstream]
        if not unwound or left:
            self._metrics.record_failure(_COMPONENT)
            logger.error(
                "route_gate_cancel_incomplete request_id=%s downstream_unwound=%s pending_tasks=%d",
                ticket.gateway_id,
                unwound,
                len(left),
            )
        self._metrics.record_cancelled()
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "route_gate_request_cancelled request_id=%s phase=%s stragglers=%d latency_ms=%d",
            ticket.gateway_id,
            "body" if downstream is None else "downstream",
            len(stragglers),
            latency_ms,
        )
        await self._notify(ticket, latency_ms)

    async def _notify(self, ticket: RequestTicket, latency_ms: int) -> None:
        hook = self._cancel_hook
        if hook is None:
            return
        for request_id in ticket.request_ids():
            try:
                await asyncio.wait_for(hook(request_id, latency_ms=latency_ms), self._grace)
            except Exception as exc:
                # Type only: an exception message can quote request content.
                self._metrics.record_failure(_COMPONENT)
                logger.error(
                    "route_gate_cancel_hook_failed request_id=%s error=%s",
                    ticket.gateway_id,
                    type(exc).__name__,
                )


async def _settle(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.wait({task})
    if not task.cancelled():
        task.exception()
