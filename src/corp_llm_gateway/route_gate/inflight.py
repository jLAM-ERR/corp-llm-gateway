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

The body is read BEFORE a slot is taken, under a deadline
(``CORP_LLM_BODY_READ_SECONDS``, 408 past it), and the number of requests
reading a body at once has its own, larger cap (``CORP_LLM_MAX_DRAINING``, 429
past it without reading): a client that never finishes its body holds no slot.
The bytes buffered for bodies, being read or replayed to an admitted request
until it is released, share one budget (``CORP_LLM_MAX_DRAINING_BYTES``): a
declared ``Content-Length``, or a chunk as it arrives, that would take the sum
past it gets 429 and gives back what the request held.

Tasks belong to a request through :class:`RequestTicket`, carried in a
contextvar. The task factory (:func:`install_task_factory`) tags a new task only
when the task creating it already belongs to the request, so a long-lived task
that merely runs in a copy of the request's context (litellm's logging worker
runs callbacks that way) is never cancelled with it.

A task whose result more than one request awaits (a single-flight lookup, a
shared key fetch) MUST be started with :func:`spawn_shared`. Started from inside
a request, it would belong to that request, and that request's disconnect would
cancel it under every other waiter. litellm's logging worker is recognised by
its module and never tagged either, wherever it is (re)started.
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
from contextvars import Context, ContextVar, copy_context
from typing import Any, Protocol

from corp_llm_gateway.metrics import MetricsExporter

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
Refuse = Callable[[str], Awaitable[None]]
# The block_reason to refuse a complete body with, or None to serve it.
BodyCheck = Callable[[list[bytes]], str | None]

# block_reason / error code of the capacity refusal.
ROUTE_GATE_CAPACITY = "capacity"
E_CAPACITY = "E_CAPACITY"
# block_reason / error code of a body not complete within the body-read deadline.
ROUTE_GATE_BODY_TIMEOUT = "body_timeout"
E_BODY_TIMEOUT = "E_BODY_TIMEOUT"
# The oversize outcome the hook already gives a leaf past its threshold.
OVERSIZE_BLOCKED = "oversize:blocked"
# Every block_reason the gate answers with on the limiter's behalf.
LIMITER_BLOCK_REASONS: frozenset[str] = frozenset(
    {ROUTE_GATE_CAPACITY, ROUTE_GATE_BODY_TIMEOUT, OVERSIZE_BLOCKED}
)

# nginx's client_max_body_size. The 100 KiB payload threshold is per text leaf,
# not a body cap: a real Claude Code body is routinely larger.
MAX_BODY_BYTES = 25 * 1024 * 1024
DEFAULT_CANCEL_GRACE_S = 5.0
DEFAULT_BODY_READ_S = 30.0
# Concurrent body reads per admitted slot, when no draining cap is given.
DRAINING_PER_SLOT = 4
DEFAULT_MAX_DRAINING_BYTES = 512 * 1024 * 1024

_COMPONENT = "route_gate"

# RequestTicket.cancel_origin: who cancelled the request.
CANCEL_CLIENT = "client"
CANCEL_SERVER = "server"


class CancelHook(Protocol):
    def __call__(self, request_id: str, *, latency_ms: int = ...) -> Awaitable[None]: ...


class RequestTicket:
    """One admitted request: its gateway id, the call ids bound to it, its tasks.

    ``cancelled`` is set before the downstream is cancelled, so any audit the
    unwinding request (or a litellm callback run in its context) attempts can
    stand down for the one ``cancelled`` record written after (by the ticket's
    terminal record once the pre-call deposited its facts, by the guardrail's
    cancel hook before that). ``cancel_origin`` says who cancelled it: ``client``
    with ``cancelled`` (the client left), ``server`` without it (the server
    cancelled the request, e.g. at shutdown; the guardrail is not told).

    ``close()`` is the limiter letting go of the request: normally when the
    downstream returns, on a disconnect after the grace even if the downstream
    never unwound. State a request leaves outside its own frames (a response
    mapping, a restorer) is dropped by a hook registered with ``on_close``.
    ``audit_facts`` holds the content-free facts its terminal audit record is
    written from.
    """

    __slots__ = (
        "__weakref__",
        "_on_close",
        "audit_facts",
        "call_ids",
        "cancel_origin",
        "cancelled",
        "closed",
        "gateway_id",
        "tasks",
    )

    def __init__(self, gateway_id: str) -> None:
        self.gateway_id = gateway_id
        self.call_ids: list[str] = []
        self.cancelled = False
        self.cancel_origin: str | None = None
        self.closed = False
        self.tasks: weakref.WeakSet[asyncio.Task[Any]] = weakref.WeakSet()
        self.audit_facts: Any = None
        self._on_close: list[Callable[[RequestTicket], None]] = []

    def mark_cancelled(self, origin: str) -> None:
        """Record who cancelled the request; the first origin stays."""
        if self.cancel_origin is None:
            self.cancel_origin = origin
            if origin == CANCEL_CLIENT:
                self.cancelled = True

    def request_ids(self) -> list[str]:
        return list(self.call_ids) or [self.gateway_id]

    def pending(self) -> list[asyncio.Task[Any]]:
        return [task for task in list(self.tasks) if not task.done()]

    def on_close(self, hook: Callable[[RequestTicket], None]) -> bool:
        """Run ``hook(ticket)`` once, at ``close()``; refused (False) once closed."""
        if self.closed:
            return False
        if hook not in self._on_close:
            self._on_close.append(hook)
        return True

    def close(self) -> int:
        """Run every hook once, each guarded; the number that raised."""
        if self.closed:
            return 0
        self.closed = True
        hooks, self._on_close = self._on_close, []
        failed = 0
        for hook in hooks:
            try:
                hook(self)
            except Exception as exc:
                failed += 1
                # Type only: a hook's message can quote request content.
                logger.error(
                    "route_gate_ticket_close_hook_failed request_id=%s error=%s",
                    self.gateway_id,
                    type(exc).__name__,
                )
        return failed


_TICKET: ContextVar[RequestTicket | None] = ContextVar("corp_llm_gateway_request", default=None)
_TAGGED: weakref.WeakKeyDictionary[asyncio.Task[Any], RequestTicket] = weakref.WeakKeyDictionary()
# Tasks no request owns: a request's straggler sweep never cancels one.
_SHARED: weakref.WeakSet[asyncio.Task[Any]] = weakref.WeakSet()
# Modules whose tasks serve every request (litellm's logging worker and its
# queue-overflow helpers), even when a request's task starts them.
_UNOWNED_MODULES = frozenset({"litellm.litellm_core_utils.logging_worker"})


def current_ticket() -> RequestTicket | None:
    return _TICKET.get()


def bind_call_id(call_id: str) -> RequestTicket | None:
    """Record litellm's per-call id on the request being served; that request's ticket."""
    ticket = _TICKET.get()
    if ticket is not None and call_id not in ticket.call_ids:
        ticket.call_ids.append(call_id)
    return ticket


def pending_request_tasks() -> list[asyncio.Task[Any]]:
    """Every still-pending task tagged to some request."""
    return [task for task in list(_TAGGED.keys()) if not task.done()]


def spawn_shared[T](coro: Coroutine[Any, Any, T], *, name: str | None = None) -> asyncio.Task[T]:
    """Start a task that no request owns, for a result several requests await."""
    context = copy_context()
    context.run(_TICKET.set, None)
    task = asyncio.get_running_loop().create_task(coro, name=name, context=context)
    _SHARED.add(task)
    return task


def _unowned(coro: Any) -> bool:
    frame = getattr(coro, "cr_frame", None)
    return frame is not None and frame.f_globals.get("__name__") in _UNOWNED_MODULES


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
        if _unowned(coro):
            _SHARED.add(task)
            return task
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

    def complete(self) -> None:
        # uvicorn answers http.disconnect from receive once the app has returned.
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
_BODY_TIMEOUT = object()
_OVER_BUDGET = object()


class _Held:
    """The body bytes one request holds of the limiter's byte budget."""

    __slots__ = ("bytes",)

    def __init__(self) -> None:
        self.bytes = 0


class InflightLimiter:
    """Cap on concurrent admitted requests (0 = no cap) with disconnect-aware release."""

    def __init__(
        self,
        max_inflight: int,
        *,
        metrics: MetricsExporter,
        cancel_grace_s: float = DEFAULT_CANCEL_GRACE_S,
        max_body_bytes: int = MAX_BODY_BYTES,
        body_read_s: float = DEFAULT_BODY_READ_S,
        max_draining: int | None = None,
        max_draining_bytes: int = DEFAULT_MAX_DRAINING_BYTES,
    ) -> None:
        if not _count(max_inflight):
            raise ValueError("max_inflight must be a non-negative integer")
        if not (math.isfinite(cancel_grace_s) and cancel_grace_s > 0):
            raise ValueError("cancel_grace_s must be a positive finite number")
        if not (math.isfinite(body_read_s) and body_read_s > 0):
            raise ValueError("body_read_s must be a positive finite number")
        if max_draining is None:
            max_draining = DRAINING_PER_SLOT * max_inflight
        if not _count(max_draining) or max_draining < max_inflight:
            raise ValueError("max_draining must be an integer no smaller than max_inflight")
        if max_inflight and not max_draining:
            raise ValueError("max_draining cannot be 0 while the in-flight cap is on")
        if not _count(max_draining_bytes) or max_draining_bytes < max(max_body_bytes, 1):
            raise ValueError("max_draining_bytes must be an integer no smaller than the body cap")
        self._max = max_inflight
        self._metrics = metrics
        self._grace = float(cancel_grace_s)
        self._max_body = max_body_bytes
        self._body_read_s = float(body_read_s)
        self._max_draining = max_draining
        self._inflight = 0
        self._draining = 0
        self._max_bytes = max_draining_bytes
        self._buffered = 0
        self._cancel_hook: CancelHook | None = None
        self._ticket_hook: Callable[[RequestTicket], None] | None = None

    @property
    def max_inflight(self) -> int:
        return self._max

    @property
    def cancel_grace_s(self) -> float:
        return self._grace

    @property
    def inflight(self) -> int:
        return self._inflight

    @property
    def max_draining(self) -> int:
        return self._max_draining

    @property
    def body_read_s(self) -> float:
        return self._body_read_s

    @property
    def draining(self) -> int:
        return self._draining

    @property
    def max_draining_bytes(self) -> int:
        return self._max_bytes

    @property
    def buffered_bytes(self) -> int:
        return self._buffered

    def bind_cancel_hook(self, hook: CancelHook | None) -> None:
        self._cancel_hook = hook

    def bind_ticket_hook(self, hook: Callable[[RequestTicket], None] | None) -> None:
        """Run ``hook(ticket)`` on every ticket as it is made (the terminal audit binds
        its close-time record there)."""
        self._ticket_hook = hook

    def try_acquire(self) -> bool:
        if self._max and self._inflight >= self._max:
            return False
        self._inflight += 1
        self._metric("set_inflight", self._inflight)
        return True

    def _release(self) -> None:
        self._inflight -= 1
        self._metric("set_inflight", self._inflight)

    def _metric(self, name: str, *args: Any) -> None:
        """An exporter call that never raises: a slot, a release or a hook never hangs on it."""
        try:
            getattr(self._metrics, name)(*args)
        except Exception as exc:
            logger.error("route_gate_metrics_error class=%s", type(exc).__name__)

    def _hold(self, held: _Held, size: int) -> bool:
        """Grow what ``held`` holds to ``size`` bytes, if the budget has room."""
        more = size - held.bytes
        if more <= 0:
            return True
        if self._buffered + more > self._max_bytes:
            return False
        self._buffered += more
        held.bytes = size
        self._metric("set_draining_bytes", self._buffered)
        return True

    def _give_back(self, held: _Held) -> None:
        if held.bytes:
            self._buffered -= held.bytes
            held.bytes = 0
            self._metric("set_draining_bytes", self._buffered)

    async def run(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        app: ASGIApp,
        *,
        refuse: Refuse,
        check: BodyCheck | None = None,
    ) -> None:
        """Admit one request: read its body, then take a slot and free it exactly once.
        ``check`` sees the complete body before any slot is taken; a reason it returns
        is refused, and the downstream never runs."""
        full = bool(self._max) and self._inflight >= self._max
        if full or (self._max_draining and self._draining >= self._max_draining):
            await refuse(ROUTE_GATE_CAPACITY)
            return
        held = _Held()
        try:
            await self._admit(held, scope, receive, send, app, refuse, check)
        finally:
            self._give_back(held)

    async def _admit(
        self,
        held: _Held,
        scope: Scope,
        receive: Receive,
        send: Send,
        app: ASGIApp,
        refuse: Refuse,
        check: BodyCheck | None,
    ) -> None:
        declared = _declared_length(scope)
        if declared is not None and not self._hold(held, min(declared, self._max_body)):
            await refuse(ROUTE_GATE_CAPACITY)
            return
        started = time.monotonic()
        ticket = RequestTicket(uuid.uuid4().hex)
        self._on_ticket(ticket)
        try:
            await self._admit_ticket(
                ticket, started, held, scope, receive, send, app, refuse, check
            )
        finally:
            self._close(ticket)

    async def _admit_ticket(
        self,
        ticket: RequestTicket,
        started: float,
        held: _Held,
        scope: Scope,
        receive: Receive,
        send: Send,
        app: ASGIApp,
        refuse: Refuse,
        check: BodyCheck | None,
    ) -> None:
        self._draining += 1
        try:
            try:
                async with asyncio.timeout(self._body_read_s):
                    drained = await self._drain(receive, held)
            except TimeoutError:
                drained = _BODY_TIMEOUT
        finally:
            self._draining -= 1
        if not isinstance(drained, list):
            # Nothing buffered is kept past a refusal: give it back before answering.
            self._give_back(held)
        if drained is _OVERSIZE:
            await refuse(OVERSIZE_BLOCKED)
            return
        if drained is _OVER_BUDGET:
            await refuse(ROUTE_GATE_CAPACITY)
            return
        if drained is _BODY_TIMEOUT:
            await refuse(ROUTE_GATE_BODY_TIMEOUT)
            return
        if drained is _DISCONNECTED:
            await self._cancelled(ticket, started, downstream=None)
            return
        assert isinstance(drained, list)
        reason = check(drained) if check is not None else None
        if reason is not None:
            drained.clear()
            self._give_back(held)
            await refuse(reason)
            return
        if not self.try_acquire():
            drained.clear()
            self._give_back(held)
            await refuse(ROUTE_GATE_CAPACITY)
            return
        try:
            await self._serve(ticket, started, scope, receive, send, app, drained)
        finally:
            try:
                # Before the slot frees: what the request left behind goes first.
                self._close(ticket)
            finally:
                self._release()

    def _on_ticket(self, ticket: RequestTicket) -> None:
        hook = self._ticket_hook
        if hook is None:
            return
        try:
            hook(ticket)
        except Exception as exc:
            self._metric("record_failure", _COMPONENT)
            logger.error(
                "route_gate_ticket_hook_failed request_id=%s error=%s",
                ticket.gateway_id,
                type(exc).__name__,
            )

    def _close(self, ticket: RequestTicket) -> None:
        for _ in range(ticket.close()):
            self._metric("record_failure", _COMPONENT)

    async def _drain(self, receive: Receive, held: _Held) -> object:
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
            if not self._hold(held, total):
                return _OVER_BUDGET
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
                ticket.mark_cancelled(CANCEL_CLIENT)
            replay.disconnect()

        watcher = asyncio.create_task(self._watch(receive, client_gone))
        try:
            await asyncio.wait({downstream, watcher}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            await self._server_cancelled(ticket, downstream, watcher)
            raise
        if replay.disconnected and not response_complete:
            await self._cancelled(ticket, started, downstream=downstream)
            await _settle(watcher)
            return
        watcher.cancel()
        try:
            try:
                await _settle(watcher)
                # Not `await downstream`: that would hand the cancel to the downstream
                # before the ticket says who cancelled it.
                if not downstream.done():
                    await asyncio.wait({downstream})
            except asyncio.CancelledError:
                await self._server_cancelled(ticket, downstream, watcher)
                raise
            downstream.result()
        finally:
            replay.complete()

    async def _server_cancelled(
        self, ticket: RequestTicket, downstream: asyncio.Task[None], watcher: asyncio.Task[None]
    ) -> None:
        """The server is cancelling the request: mark it before the downstream sees it,
        then give the downstream the grace to unwind."""
        ticket.mark_cancelled(CANCEL_SERVER)
        watcher.cancel()
        downstream.cancel()
        try:
            await asyncio.wait({downstream, watcher}, timeout=self._grace)
        finally:
            # Also when a second cancel cuts the grace short.
            _retrieve_when_done(downstream)
            _retrieve_when_done(watcher)
        if not downstream.done():
            # The slot is freed anyway, as on the client path; say so the same way.
            logger.error(
                "route_gate_cancel_incomplete request_id=%s downstream_unwound=%s "
                "pending_tasks=%d origin=server",
                ticket.gateway_id,
                False,
                len(_stragglers(ticket, downstream)),
            )
            self._metric("record_failure", _COMPONENT)

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
        ticket.mark_cancelled(CANCEL_CLIENT)
        loop = asyncio.get_running_loop()
        unwound = True
        if downstream is not None:
            downstream.cancel()
            try:
                await asyncio.wait({downstream}, timeout=self._grace)
            except asyncio.CancelledError:
                # The server is cancelling us too: nothing is awaited any more,
                # so every task the request left behind is retrieved when it ends.
                _retrieve_when_done(downstream)
                for task in _stragglers(ticket, downstream):
                    task.cancel()
                    _retrieve_when_done(task)
                raise
            unwound = downstream.done()
            if unwound and not downstream.cancelled():
                # The client is gone: whatever the downstream raised on its way
                # out has no one to answer to.
                exc = downstream.exception()
                if exc is not None:
                    logger.info(
                        "route_gate_cancelled_downstream_error error=%s", type(exc).__name__
                    )
            elif not unwound:
                downstream.add_done_callback(_retrieve)
        # One budget for the stragglers AND the guardrail: a slot is held at most
        # 2 x grace after a disconnect.
        deadline = loop.time() + self._grace
        stragglers = _stragglers(ticket, downstream)
        for task in stragglers:
            task.cancel()
        try:
            if stragglers:
                await asyncio.wait(stragglers, timeout=self._grace)
        finally:
            for task in stragglers:
                _retrieve_straggler(task)
        left = _stragglers(ticket, downstream)
        for task in left:
            _retrieve_when_done(task)
        incomplete = not unwound or bool(left)
        if incomplete:
            logger.error(
                "route_gate_cancel_incomplete request_id=%s downstream_unwound=%s pending_tasks=%d",
                ticket.gateway_id,
                unwound,
                len(left),
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "route_gate_request_cancelled request_id=%s phase=%s stragglers=%d latency_ms=%d",
            ticket.gateway_id,
            "body" if downstream is None else "downstream",
            len(stragglers),
            latency_ms,
        )
        # The hook first: it is what frees the request's content.
        try:
            await self._notify(ticket, latency_ms, deadline)
        finally:
            if incomplete:
                self._metric("record_failure", _COMPONENT)
            self._metric("record_cancelled")

    async def _notify(self, ticket: RequestTicket, latency_ms: int, deadline: float) -> None:
        hook = self._cancel_hook
        if hook is None:
            return
        loop = asyncio.get_running_loop()
        # In the request's context: the guardrail ends only state that request owns.
        calls = [
            loop.create_task(
                _call_hook(hook, request_id, latency_ms), context=_ticket_context(ticket)
            )
            for request_id in ticket.request_ids()
        ]
        # Started even with no budget left: each runs up to its first suspension.
        try:
            _, pending = await asyncio.wait(calls, timeout=max(0.0, deadline - loop.time()))
        except asyncio.CancelledError:
            for call in calls:
                call.cancel()
                _retrieve_when_done(call)
            raise
        for call in calls:
            if call in pending:
                call.cancel()
                call.add_done_callback(_retrieve)
                error = "TimeoutError"
            else:
                exc = call.exception()
                if exc is None:
                    continue
                error = type(exc).__name__
            # Type only: an exception message can quote request content.
            self._metric("record_failure", _COMPONENT)
            logger.error(
                "route_gate_cancel_hook_failed request_id=%s error=%s", ticket.gateway_id, error
            )


def _ticket_context(ticket: RequestTicket) -> Context:
    context = copy_context()
    context.run(_TICKET.set, ticket)
    return context


async def _call_hook(hook: CancelHook, request_id: str, latency_ms: int) -> None:
    await hook(request_id, latency_ms=latency_ms)


def _declared_length(scope: Scope) -> int | None:
    """The request's ``Content-Length``, when it names exactly one byte count."""
    headers = scope.get("headers") or ()
    values = [value for name, value in headers if bytes(name).lower() == b"content-length"]
    if len(values) != 1:
        return None
    raw = bytes(values[0]).strip()
    if not raw or not raw.isdigit():
        return None
    return int(raw)


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _stragglers(
    ticket: RequestTicket, downstream: asyncio.Task[None] | None
) -> list[asyncio.Task[Any]]:
    return [task for task in ticket.pending() if task is not downstream and task not in _SHARED]


def _retrieve(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def _retrieve_straggler(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.add_done_callback(_retrieve)
        return
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # The class name only: the task's message can quote request content.
        logger.info("route_gate_straggler_error error=%s", type(exc).__name__)


def _retrieve_when_done(task: asyncio.Task[Any]) -> None:
    if task.done():
        _retrieve(task)
    else:
        task.add_done_callback(_retrieve)


async def _settle(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.wait({task})
    if not task.cancelled():
        task.exception()
