"""The in-flight cap behind the route gate: admission, body replay, disconnect-aware
cancellation and exactly-once slot release. The served-stack proof of the same
behaviour on a real socket is ``tests/test_inflight_served_stack.py``."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from importlib.util import find_spec
from typing import Any

import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.metrics import MetricsExporter
from corp_llm_gateway.route_gate import RouteGateMiddleware
from corp_llm_gateway.route_gate.inflight import (
    CANCEL_CLIENT,
    CANCEL_SERVER,
    DEFAULT_MAX_DRAINING_BYTES,
    E_BODY_TIMEOUT,
    E_CAPACITY,
    MAX_BODY_BYTES,
    ROUTE_GATE_BODY_TIMEOUT,
    ROUTE_GATE_CAPACITY,
    InflightLimiter,
    RequestTicket,
    bind_call_id,
    current_ticket,
    install_task_factory,
    pending_request_tasks,
)
from corp_llm_gateway.route_gate.middleware import COMPONENT

CANARY = "AKIAIOSFODNN7EXAMPLE"
Message = dict[str, Any]


class _Metrics(MetricsExporter):
    def __init__(self) -> None:
        self.blocks: list[str] = []
        self.failures: list[str] = []
        self.inflight: list[int] = []
        self.cancelled = 0
        self.draining_bytes: list[int] = []

    def record_block(self, block_reason: str) -> None:
        self.blocks.append(block_reason)

    def record_failure(self, component: str) -> None:
        self.failures.append(component)

    def observe_request_latency(self, seconds: float, *, status: str) -> None:
        return None

    def set_inflight(self, count: int) -> None:
        self.inflight.append(count)

    def record_cancelled(self) -> None:
        self.cancelled += 1

    def set_draining_bytes(self, count: int) -> None:
        self.draining_bytes.append(count)


class _Client:
    """The server side of one connection, as uvicorn presents it to an app."""

    def __init__(self, chunks: tuple[bytes, ...] = (b"{}",)) -> None:
        self.incoming: asyncio.Queue[Message] = asyncio.Queue()
        for index, chunk in enumerate(chunks):
            self.incoming.put_nowait(
                {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
            )
        self.sent: list[Message] = []
        self.receive_calls = 0

    async def receive(self) -> Message:
        self.receive_calls += 1
        return await self.incoming.get()

    async def send(self, message: Message) -> None:
        self.sent.append(message)

    def disconnect(self) -> None:
        self.incoming.put_nowait({"type": "http.disconnect"})

    @property
    def status(self) -> int | None:
        return next((m["status"] for m in self.sent if m["type"] == "http.response.start"), None)

    def json(self) -> Any:
        return json.loads(b"".join(m.get("body", b"") for m in self.sent if "body" in m))


async def _read_body(receive: Callable[[], Awaitable[Message]]) -> bytes:
    body = b""
    while True:
        message = await receive()
        assert message["type"] == "http.request", message
        body += message.get("body", b"")
        if not message.get("more_body", False):
            return body


async def _respond(send: Callable[[Message], Awaitable[None]], body: bytes = b"ok") -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


class _Holding:
    """Reads the body, then holds its slot until released."""

    def __init__(self) -> None:
        self.entered = asyncio.Semaphore(0)
        self.release = asyncio.Event()
        self.calls = 0
        self.bodies: list[bytes] = []
        self.cancelled = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        self.bodies.append(await _read_body(receive))
        self.entered.release()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        await _respond(send)


def _scope(method: str = "POST", path: str = "/v1/messages") -> dict[str, Any]:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "headers": [(b"content-type", b"application/json")],
    }


def _stack(
    app: Any,
    *,
    max_inflight: int = 2,
    grace: float = 1.0,
    metrics: _Metrics | None = None,
    sink: ListSink | None = None,
    max_body_bytes: int = MAX_BODY_BYTES,
    **limits: Any,
) -> tuple[RouteGateMiddleware, InflightLimiter, _Metrics, ListSink]:
    metrics = metrics if metrics is not None else _Metrics()
    sink = sink if sink is not None else ListSink()
    limiter = InflightLimiter(
        max_inflight,
        metrics=metrics,
        cancel_grace_s=grace,
        max_body_bytes=max_body_bytes,
        **limits,
    )
    gate = RouteGateMiddleware(
        app, metrics=metrics, audit_logger=AuditLogger(sink, "test"), limiter=limiter
    )
    gate.arm()
    return gate, limiter, metrics, sink


class _CancelHook:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, request_id: str, *, latency_ms: int = 0) -> None:
        self.calls.append((request_id, latency_ms))


@pytest.fixture
async def task_factory() -> AsyncIterator[None]:
    restore = install_task_factory(asyncio.get_running_loop())
    yield
    restore()


# ── admission ────────────────────────────────────────────────────────────────


async def test_n_are_admitted_and_the_next_gets_429_before_the_downstream_runs() -> None:
    app = _Holding()
    gate, limiter, metrics, sink = _stack(app, max_inflight=2)
    first, second, third = _Client(), _Client(), _Client((CANARY.encode(),))

    running = [asyncio.create_task(gate(_scope(), c.receive, c.send)) for c in (first, second)]
    for _ in range(2):
        await asyncio.wait_for(app.entered.acquire(), 2)
    await gate(_scope(), third.receive, third.send)

    assert third.status == 429
    assert third.json() == {
        "error": {
            "type": "capacity",
            "code": E_CAPACITY,
            "route": "POST /v1/messages",
            "reason": ROUTE_GATE_CAPACITY,
        }
    }
    # Refused before the body was read and before litellm (the downstream) ran.
    assert third.receive_calls == 0
    assert app.calls == 2
    assert metrics.blocks == [ROUTE_GATE_CAPACITY]
    assert COMPONENT not in metrics.failures
    assert [r["block_reason"] for r in sink.records] == [ROUTE_GATE_CAPACITY]
    assert sink.records[0]["error_code"] == E_CAPACITY
    assert limiter.inflight == 2

    app.release.set()
    await asyncio.gather(*running)
    assert (first.status, second.status) == (200, 200)
    assert limiter.inflight == 0
    assert metrics.inflight[-1] == 0


async def test_a_released_slot_admits_the_next_request() -> None:
    app = _Holding()
    gate, limiter, _, _ = _stack(app, max_inflight=1)
    app.release.set()

    for _ in range(3):
        client = _Client()
        await gate(_scope(), client.receive, client.send)
        assert client.status == 200

    assert limiter.inflight == 0
    assert app.calls == 3


async def test_the_slot_is_held_past_the_final_body_until_the_downstream_returns() -> None:
    # uvicorn closes a Connection: close socket inside the final send, so a client can
    # read the whole response while the app is still unwinding (the served stack).
    responded = asyncio.Event()
    finish = asyncio.Event()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await _respond(send)
        responded.set()
        await finish.wait()

    gate, limiter, metrics, _ = _stack(app, max_inflight=1)
    first, second = _Client(), _Client()
    running = asyncio.create_task(gate(_scope(), first.receive, first.send))
    await asyncio.wait_for(responded.wait(), 2)

    assert first.status == 200
    assert limiter.inflight == 1
    await gate(_scope(), second.receive, second.send)
    assert second.status == 429
    assert metrics.blocks == [ROUTE_GATE_CAPACITY]

    finish.set()
    await running
    assert limiter.inflight == 0
    third = _Client()
    await gate(_scope(), third.receive, third.send)
    assert third.status == 200


# The loop uvicorn serves on in production, when installed.
LOOPS = ["asyncio"] + (["uvloop"] if find_spec("uvloop") is not None else [])
# From the app's return: 2 hops for the first wait to see it, 3 to cancel and settle
# the idle watcher. A finished downstream must cost nothing more.
RELEASE_HOPS = 5


@pytest.mark.parametrize("loop", LOOPS)
def test_a_finished_downstream_frees_the_slot_without_extra_loop_hops(loop: str) -> None:
    async def run() -> int:
        responded = asyncio.Event()

        async def app(scope: Any, receive: Any, send: Any) -> None:
            await _read_body(receive)
            await _respond(send)
            responded.set()

        gate, limiter, _, _ = _stack(app, max_inflight=1)
        client = _Client()
        running = asyncio.create_task(gate(_scope(), client.receive, client.send))
        await asyncio.wait_for(responded.wait(), 2)
        hops = 0
        while limiter.inflight and hops < 100:
            await asyncio.sleep(0)
            hops += 1
        await running
        assert client.status == 200
        return hops

    factory = None
    if loop == "uvloop":
        import uvloop

        factory = uvloop.new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        hops = runner.run(run())

    # Each extra hop is a window in which a sequential client at the cap gets 429.
    assert hops <= RELEASE_HOPS


async def test_passthrough_routes_never_count() -> None:
    app = _Holding()
    gate, limiter, metrics, _ = _stack(app, max_inflight=1)
    held = _Client()
    running = asyncio.create_task(gate(_scope(), held.receive, held.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    passthrough_hits: list[str] = []

    async def plain(scope: Any, receive: Any, send: Any) -> None:
        passthrough_hits.append(scope["path"])
        await _respond(send)

    gate.app = plain  # the same gate and limiter, a PASSTHROUGH route
    probe = _Client((b"",))
    await gate(_scope("GET", "/healthz/live"), probe.receive, probe.send)

    assert probe.status == 200
    assert passthrough_hits == ["/healthz/live"]
    assert limiter.inflight == 1
    assert ROUTE_GATE_CAPACITY not in metrics.blocks

    gate.app = app
    app.release.set()
    await running


async def test_an_unarmed_rewritten_route_is_503_and_takes_no_slot() -> None:
    app = _Holding()
    gate, limiter, _, _ = _stack(app, max_inflight=1)
    gate.armed = False
    client = _Client()

    await gate(_scope(), client.receive, client.send)

    assert client.status == 503
    assert app.calls == 0
    assert limiter.inflight == 0


async def test_zero_turns_the_cap_off() -> None:
    app = _Holding()
    gate, limiter, metrics, _ = _stack(app, max_inflight=0)
    clients = [_Client() for _ in range(5)]
    running = [asyncio.create_task(gate(_scope(), c.receive, c.send)) for c in clients]
    for _ in clients:
        await asyncio.wait_for(app.entered.acquire(), 2)

    assert limiter.inflight == 5
    assert ROUTE_GATE_CAPACITY not in metrics.blocks
    app.release.set()
    await asyncio.gather(*running)
    assert limiter.inflight == 0


@pytest.mark.parametrize("bad", [-1, True, 1.5])
def test_the_limiter_refuses_a_bad_cap(bad: Any) -> None:
    with pytest.raises(ValueError):
        InflightLimiter(bad, metrics=_Metrics())


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), float("inf")])
def test_the_limiter_refuses_a_bad_grace(bad: float) -> None:
    with pytest.raises(ValueError):
        InflightLimiter(1, metrics=_Metrics(), cancel_grace_s=bad)


# ── release, exactly once ────────────────────────────────────────────────────


async def test_the_slot_is_released_when_the_downstream_raises() -> None:
    async def failing(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        raise RuntimeError("upstream exploded")

    gate, limiter, metrics, _ = _stack(failing, max_inflight=1)
    client = _Client()

    with pytest.raises(RuntimeError):
        await gate(_scope(), client.receive, client.send)

    assert limiter.inflight == 0
    assert metrics.inflight == [1, 0]
    assert metrics.cancelled == 0


async def test_the_slot_is_released_when_send_raises() -> None:
    async def responding(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await _respond(send)

    gate, limiter, metrics, _ = _stack(responding, max_inflight=1)
    client = _Client()

    async def broken_send(message: Message) -> None:
        raise OSError("socket gone")

    with pytest.raises(OSError):
        await gate(_scope(), client.receive, broken_send)

    assert limiter.inflight == 0
    assert metrics.inflight == [1, 0]


async def test_the_slot_is_released_when_the_server_cancels_the_request() -> None:
    app = _Holding()
    gate, limiter, metrics, _ = _stack(app, max_inflight=1)
    client = _Client()
    running = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert app.cancelled == 1
    assert limiter.inflight == 0
    assert metrics.inflight == [1, 0]


# ── the body: drained once, replayed byte for byte ───────────────────────────


@pytest.mark.parametrize(
    "chunks",
    [(b"",), (b'{"a": 1}',), (b'{"messages": [', b'{"role": "user"}', b"]}"), (b"", b"x", b"")],
    ids=["empty", "single", "chunked", "empty-chunks"],
)
async def test_the_body_is_replayed_byte_exact(chunks: tuple[bytes, ...]) -> None:
    seen: list[Message] = []

    async def recording(scope: Any, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            seen.append(message)
            if not message.get("more_body", False):
                break
        await _respond(send)

    gate, _, _, _ = _stack(recording)
    client = _Client(chunks)

    await gate(_scope(), client.receive, client.send)

    assert client.status == 200
    assert b"".join(m["body"] for m in seen) == b"".join(chunks)
    assert all(m["type"] == "http.request" for m in seen)
    assert seen[-1]["more_body"] is False


async def test_the_body_is_read_before_the_downstream_starts() -> None:
    order: list[str] = []
    client = _Client((b"a", b"b"))
    real_receive = client.receive

    async def receive() -> Message:
        order.append("read")
        return await real_receive()

    async def app(scope: Any, receive_: Any, send: Any) -> None:
        order.append("downstream")
        await _read_body(receive_)
        await _respond(send)

    gate, _, _, _ = _stack(app)
    task = asyncio.create_task(gate(_scope(), receive, client.send))
    await asyncio.sleep(0.05)
    client.disconnect()  # after the response: not a cancellation
    await task

    assert order[:3] == ["read", "read", "downstream"]


async def test_an_oversize_body_gets_the_oversize_refusal_and_frees_the_slot() -> None:
    app = _Holding()
    gate, limiter, metrics, sink = _stack(app, max_inflight=1, max_body_bytes=8)
    client = _Client((b"12345", b"6789", CANARY.encode()))

    await gate(_scope(), client.receive, client.send)

    assert client.status == 422
    assert client.json()["error"]["code"] == "E_OVERSIZE_BLOCKED"
    assert client.json()["error"]["reason"] == "oversize:blocked"
    assert CANARY not in json.dumps(client.json())
    assert app.calls == 0
    assert limiter.inflight == 0
    assert metrics.blocks == ["oversize:blocked"]
    assert metrics.failures == ["oversize"]
    assert sink.records[0]["block_reason"] == "oversize:blocked"


async def test_a_body_exactly_at_the_cap_is_admitted() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await _respond(send)

    gate, _, _, _ = _stack(app, max_body_bytes=8)
    client = _Client((b"1234", b"5678"))

    await gate(_scope(), client.receive, client.send)

    assert client.status == 200


def test_the_body_cap_matches_the_documented_edge_limit() -> None:
    # nginx's client_max_body_size (docs/plans/20260806-nginx-profile-tls.md); the
    # 100 KiB payload threshold is per text leaf, not a body cap.
    assert MAX_BODY_BYTES == 25 * 1024 * 1024


# ── disconnects ──────────────────────────────────────────────────────────────


async def test_a_disconnect_is_forwarded_to_the_replay_receive() -> None:
    seen: list[str] = []
    waiting = asyncio.Event()

    async def watching(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        waiting.set()
        seen.append((await receive())["type"])

    gate, limiter, _, _ = _stack(watching)
    hook = _CancelHook()
    limiter.bind_cancel_hook(hook)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(waiting.wait(), 2)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert seen == ["http.disconnect"]
    assert len(hook.calls) == 1
    assert limiter.inflight == 0


async def test_the_replay_receive_keeps_answering_disconnect_and_never_raises() -> None:
    seen: list[str] = []
    waiting = asyncio.Event()

    async def polling(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        waiting.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Code under cancellation still polls receive (litellm's is_disconnected()).
            for _ in range(3):
                seen.append((await receive())["type"])
            raise

    gate, limiter, _, _ = _stack(polling)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(waiting.wait(), 2)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert seen == ["http.disconnect"] * 3
    assert limiter.inflight == 0


async def test_a_disconnect_cancels_a_stalled_downstream_within_the_grace() -> None:
    app = _Holding()
    gate, limiter, metrics, _ = _stack(app, max_inflight=1, grace=1.0)
    hook = _CancelHook()
    limiter.bind_cancel_hook(hook)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    loop = asyncio.get_running_loop()
    start = loop.time()
    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert loop.time() - start < 0.5
    assert app.cancelled == 1
    assert client.status is None
    assert metrics.cancelled == 1
    assert len(hook.calls) == 1
    assert limiter.inflight == 0
    assert metrics.inflight == [1, 0]
    # The next request is admitted.
    app.release.set()
    nxt = _Client()
    await gate(_scope(), nxt.receive, nxt.send)
    assert nxt.status == 200


async def test_the_cancel_hook_gets_the_call_id_the_hook_bound() -> None:
    async def binding(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        bind_call_id("litellm-call-1")
        await asyncio.sleep(3600)

    gate, limiter, _, _ = _stack(binding)
    hook = _CancelHook()
    limiter.bind_cancel_hook(hook)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.sleep(0.05)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert [call[0] for call in hook.calls] == ["litellm-call-1"]


async def test_without_a_bound_call_id_the_hook_gets_the_gateway_id() -> None:
    app = _Holding()
    gate, limiter, _, _ = _stack(app)
    hook = _CancelHook()
    limiter.bind_cancel_hook(hook)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    ((request_id, latency_ms),) = hook.calls
    assert len(request_id) == 32 and int(request_id, 16) >= 0
    assert latency_ms >= 0


def test_bind_call_id_outside_a_request_is_a_no_op() -> None:
    assert current_ticket() is None
    bind_call_id("anything")
    assert current_ticket() is None


async def test_a_disconnect_while_the_body_is_read_never_starts_the_downstream() -> None:
    app = _Holding()
    gate, limiter, metrics, _ = _stack(app, max_inflight=1)
    hook = _CancelHook()
    limiter.bind_cancel_hook(hook)
    client = _Client((b"partial",))
    client.incoming = asyncio.Queue()
    client.incoming.put_nowait({"type": "http.request", "body": b"part", "more_body": True})
    client.disconnect()

    await asyncio.wait_for(gate(_scope(), client.receive, client.send), 2)

    assert app.calls == 0
    assert client.status is None
    assert metrics.cancelled == 1
    assert len(hook.calls) == 1
    assert limiter.inflight == 0


async def test_a_disconnect_after_the_response_completed_cancels_nothing() -> None:
    finished = asyncio.Event()
    proceed = asyncio.Event()

    async def trailing(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await _respond(send)
        await proceed.wait()  # post-response work, like a background task
        finished.set()

    gate, limiter, metrics, _ = _stack(trailing)
    hook = _CancelHook()
    limiter.bind_cancel_hook(hook)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.sleep(0.05)

    client.disconnect()  # uvicorn answers disconnect once the response is complete
    await asyncio.sleep(0.05)
    assert not task.done()
    proceed.set()
    await asyncio.wait_for(task, 2)

    assert finished.is_set()
    assert client.status == 200
    assert metrics.cancelled == 0
    assert hook.calls == []
    assert limiter.inflight == 0


async def test_a_mid_stream_disconnect_cancels_the_stream() -> None:
    streaming = asyncio.Event()
    cancelled: list[bool] = []

    async def sse(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True})
        streaming.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    gate, limiter, metrics, _ = _stack(sse)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(streaming.wait(), 2)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert cancelled == [True]
    assert metrics.cancelled == 1
    assert limiter.inflight == 0


async def test_request_tagged_stragglers_are_cancelled(task_factory: None) -> None:
    stranger_started = asyncio.Event()
    straggler_box: list[asyncio.Task[Any]] = []

    async def forever() -> None:
        await asyncio.sleep(3600)

    unrelated = asyncio.create_task(forever())

    async def leaky(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        # litellm's first-chunk helper: a CancelledError thrown into the wait
        # leaves the fetch task pending.
        chunk_task = asyncio.ensure_future(forever())
        straggler_box.append(chunk_task)
        stranger_started.set()
        await asyncio.wait({chunk_task})

    gate, limiter, _, _ = _stack(leaky)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(stranger_started.wait(), 2)
    assert straggler_box[0] in pending_request_tasks()

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert straggler_box[0].cancelled()
    assert not unrelated.done()
    assert pending_request_tasks() == []
    assert limiter.inflight == 0
    unrelated.cancel()


async def test_a_task_run_in_the_request_context_by_a_foreign_task_is_not_tagged(
    task_factory: None,
) -> None:
    # litellm's logging worker runs callbacks with `context.run(create_task, ...)`
    # from its own long-lived task: those belong to the worker, not the request.
    captured: list[contextvars.Context] = []
    entered = asyncio.Event()

    async def capturing(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        captured.append(contextvars.copy_context())
        entered.set()
        await asyncio.sleep(3600)

    gate, _, _, _ = _stack(capturing)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(entered.wait(), 2)

    async def worker_callback() -> None:
        await asyncio.sleep(3600)

    foreign = captured[0].run(asyncio.create_task, worker_callback())
    assert foreign not in pending_request_tasks()

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert not foreign.done()
    foreign.cancel()


async def test_the_task_factory_restores_the_previous_one() -> None:
    loop = asyncio.get_running_loop()
    before = loop.get_task_factory()
    restore = install_task_factory(loop)
    assert loop.get_task_factory() is not before
    restore()
    assert loop.get_task_factory() is before


async def test_a_downstream_that_ignores_cancellation_cannot_hold_the_slot() -> None:
    stop = asyncio.Event()
    entered = asyncio.Event()

    async def stubborn(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        entered.set()
        while not stop.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue

    gate, limiter, metrics, _ = _stack(stubborn, grace=0.2)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(entered.wait(), 2)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert limiter.inflight == 0
    assert COMPONENT in metrics.failures
    stop.set()
    await asyncio.sleep(0.05)


async def test_a_server_cancel_of_a_stubborn_downstream_is_bounded_by_the_grace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The server cancels (shutdown) a downstream that ignores cancellation: ``run`` ends
    after the grace, the slot is freed and the ticket closed, and the downstream left
    running is logged and counted as on the client path."""
    stop = asyncio.Event()
    entered = asyncio.Event()
    tickets: list[RequestTicket] = []

    async def stubborn(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        ticket = current_ticket()
        assert ticket is not None
        tickets.append(ticket)
        entered.set()
        while not stop.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await stop.wait()

    gate, limiter, metrics, _ = _stack(stubborn, max_inflight=1, grace=0.2)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(entered.wait(), 2)

    loop = asyncio.get_running_loop()
    started = loop.time()
    task.cancel()
    with caplog.at_level(logging.INFO), contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    elapsed = loop.time() - started

    assert 0.2 <= elapsed < 1.0
    assert limiter.inflight == 0 and tickets[0].closed
    assert tickets[0].cancel_origin == "server"
    assert metrics.failures == [COMPONENT]
    (line,) = [
        r.getMessage() for r in caplog.records if "route_gate_cancel_incomplete" in r.getMessage()
    ]
    assert "downstream_unwound=False" in line and "origin=server" in line
    stop.set()
    await asyncio.sleep(0.05)


async def test_a_server_cancel_of_a_downstream_that_unwinds_counts_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _Holding()
    gate, limiter, metrics, _ = _stack(app, max_inflight=1, grace=0.5)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    task.cancel()
    with caplog.at_level(logging.INFO), contextlib.suppress(asyncio.CancelledError):
        await task

    assert metrics.failures == [] and limiter.inflight == 0
    assert "route_gate_cancel_incomplete" not in caplog.text


async def test_the_ticket_hook_sees_every_ticket_before_the_body_is_read() -> None:
    seen: list[tuple[RequestTicket, bool]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await _respond(send)

    gate, limiter, _, _ = _stack(app)
    limiter.bind_ticket_hook(lambda ticket: seen.append((ticket, ticket.closed)))
    client = _Client()

    await gate(_scope(), client.receive, client.send)

    ((ticket, closed_then),) = seen
    assert closed_then is False and ticket.closed and client.status == 200


async def test_a_failing_ticket_hook_is_counted_and_the_request_still_served(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await _respond(send)

    gate, limiter, metrics, _ = _stack(app)

    def failing(ticket: RequestTicket) -> None:
        raise RuntimeError(CANARY)

    limiter.bind_ticket_hook(failing)
    client = _Client()
    with caplog.at_level(logging.INFO):
        await gate(_scope(), client.receive, client.send)

    assert client.status == 200 and metrics.failures == [COMPONENT]
    assert "route_gate_ticket_hook_failed" in caplog.text and CANARY not in caplog.text


async def test_a_failing_cancel_hook_still_releases_the_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _Holding()
    gate, limiter, metrics, _ = _stack(app)

    async def failing(request_id: str, *, latency_ms: int = 0) -> None:
        raise RuntimeError(CANARY)

    limiter.bind_cancel_hook(failing)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    with caplog.at_level(logging.DEBUG):
        client.disconnect()
        await asyncio.wait_for(task, 2)

    assert limiter.inflight == 0
    assert COMPONENT in metrics.failures
    assert CANARY not in caplog.text


async def test_a_stalled_cancel_hook_is_bounded_by_the_grace() -> None:
    app = _Holding()
    gate, limiter, metrics, _ = _stack(app, grace=0.2)

    async def stalled(request_id: str, *, latency_ms: int = 0) -> None:
        await asyncio.sleep(3600)

    limiter.bind_cancel_hook(stalled)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert limiter.inflight == 0
    assert COMPONENT in metrics.failures


async def test_no_body_byte_reaches_the_log_on_any_limiter_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = json.dumps({"messages": [{"role": "user", "content": CANARY}]}).encode()
    app = _Holding()
    gate, limiter, _, sink = _stack(app, max_inflight=1, max_body_bytes=len(body) + 10)
    limiter.bind_cancel_hook(_CancelHook())

    with caplog.at_level(logging.DEBUG):
        held = _Client((body,))
        task = asyncio.create_task(gate(_scope(), held.receive, held.send))
        await asyncio.wait_for(app.entered.acquire(), 2)
        refused = _Client((body,))
        await gate(_scope(), refused.receive, refused.send)  # capacity
        held.disconnect()  # cancellation
        await asyncio.wait_for(task, 2)
        big = _Client((body, body))
        await gate(_scope(), big.receive, big.send)  # oversize

    assert (refused.status, big.status) == (429, 422)
    assert CANARY not in caplog.text
    assert CANARY not in json.dumps(sink.records)


async def test_a_server_that_breaks_the_protocol_after_the_body_is_not_spun_on() -> None:
    # After the body ASGI allows only http.disconnect; a receive that keeps
    # answering the body must not become a busy loop.
    reads = 0

    async def endless_body() -> Message:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await asyncio.sleep(0.05)
        await _respond(send)

    gate, limiter, metrics, _ = _stack(app)
    client = _Client()

    await asyncio.wait_for(gate(_scope(), endless_body, client.send), 2)

    assert client.status == 200
    assert reads == 2
    assert metrics.cancelled == 0
    assert limiter.inflight == 0


async def test_an_unexpected_message_while_the_body_is_read_ends_the_request() -> None:
    app = _Holding()
    gate, limiter, metrics, _ = _stack(app)
    client = _Client()
    client.incoming = asyncio.Queue()
    client.incoming.put_nowait({"type": "websocket.connect"})

    await asyncio.wait_for(gate(_scope(), client.receive, client.send), 2)

    assert app.calls == 0
    assert metrics.cancelled == 1
    assert limiter.inflight == 0


async def test_the_request_is_marked_cancelled_before_the_replay_wakes_the_downstream() -> None:
    # litellm reacts to the replayed disconnect (and audits) before the limiter
    # itself runs again; the flag must already be up by then.
    flags: list[bool] = []
    waiting = asyncio.Event()

    async def watching(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        waiting.set()
        await receive()
        ticket = current_ticket()
        assert ticket is not None
        flags.append(ticket.cancelled)

    gate, _, _, _ = _stack(watching)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(waiting.wait(), 2)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert flags == [True]


async def test_a_disconnect_after_the_response_leaves_the_request_uncancelled() -> None:
    flags: list[bool] = []

    async def trailing(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await _respond(send)
        await receive()
        ticket = current_ticket()
        assert ticket is not None
        flags.append(ticket.cancelled)

    gate, _, metrics, _ = _stack(trailing)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.sleep(0.05)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert flags == [False]
    assert metrics.cancelled == 0


# ── shared tasks survive a request's straggler sweep ─────────────────────────


async def test_a_shared_auth_lookup_survives_the_disconnect_of_the_request_that_started_it(
    task_factory: None,
) -> None:
    from datetime import UTC, datetime

    from tests.hook_fixtures import _build_guardrail, _data_with_token

    guardrail, sink = _build_guardrail()
    store = guardrail._auth._store
    real_lookup = store.lookup
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_lookup(corp_token: str) -> Any:
        entered.set()
        await release.wait()
        return await real_lookup(corp_token)

    store.lookup = slow_lookup  # type: ignore[method-assign]

    async def litellm_like(scope: Any, receive: Any, send: Any) -> None:
        call_id = json.loads(await _read_body(receive))["id"]
        data = _data_with_token("tok-1")
        data["litellm_call_id"] = call_id
        await guardrail.pre_call(data)
        now = datetime.now(UTC)
        await guardrail.async_log_success_event({"litellm_call_id": call_id}, None, now, now)
        await _respond(send)

    from corp_llm_gateway.route_gate.desanitize_middleware import (
        DesanitizeMiddleware,
        ResponseMappings,
    )
    from corp_llm_gateway.route_gate.terminal_audit import TerminalAudit, emit_to

    # Wired as the entrypoint wires it: a request the pre-call handed to its ticket is
    # recorded by the ticket's terminal record, through the same audit logger.
    mappings = ResponseMappings()
    guardrail.bind_response_mappings(mappings)
    terminal = TerminalAudit(emit_to(guardrail._audit))
    served = DesanitizeMiddleware(litellm_like, mappings, terminal=terminal)
    gate, limiter, metrics, _ = _stack(served, max_inflight=2)
    limiter.bind_cancel_hook(guardrail.on_request_cancelled)
    limiter.bind_ticket_hook(terminal.bind)
    first, second = _Client((b'{"id": "call-a"}',)), _Client((b'{"id": "call-b"}',))
    first_task = asyncio.create_task(gate(_scope(), first.receive, first.send))
    await asyncio.wait_for(entered.wait(), 2)
    second_task = asyncio.create_task(gate(_scope(), second.receive, second.send))
    await asyncio.sleep(0.05)
    (shared,) = guardrail._auth._inflight.values()
    assert limiter.inflight == 2

    first.disconnect()
    await asyncio.wait_for(first_task, 2)

    assert not shared.cancelled()
    assert shared not in pending_request_tasks()
    assert limiter.inflight == 1
    release.set()
    await asyncio.wait_for(second_task, 2)
    await terminal.drain()
    assert second.status == 200
    assert {r["request_id"]: r["status"] for r in sink.records} == {
        "call-a": "cancelled",
        "call-b": "ok",
    }
    assert COMPONENT not in metrics.failures
    assert limiter.inflight == 0


async def test_a_shared_task_is_never_tagged_and_outlives_the_sweep(task_factory: None) -> None:
    from corp_llm_gateway.route_gate.inflight import spawn_shared

    shared_box: list[asyncio.Task[Any]] = []
    entered = asyncio.Event()

    async def forever() -> None:
        await asyncio.sleep(3600)

    async def sharing(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        shared = spawn_shared(forever(), name="shared-fetch")
        shared_box.append(shared)
        entered.set()
        await asyncio.wait({shared})

    gate, limiter, _, _ = _stack(sharing)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(entered.wait(), 2)
    (shared,) = shared_box
    assert shared not in pending_request_tasks()
    assert shared.get_name() == "shared-fetch"

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert not shared.done()
    assert limiter.inflight == 0
    shared.cancel()


async def test_the_sweep_skips_a_shared_task_even_if_it_was_tagged(task_factory: None) -> None:
    from corp_llm_gateway.route_gate import inflight

    shared_box: list[asyncio.Task[Any]] = []
    entered = asyncio.Event()

    async def forever() -> None:
        await asyncio.sleep(3600)

    async def sharing(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        shared = inflight.spawn_shared(forever())
        ticket = current_ticket()
        assert ticket is not None
        inflight._tag(shared, ticket)  # a path that tags it anyway
        shared_box.append(shared)
        entered.set()
        await asyncio.wait({shared})

    gate, _, metrics, _ = _stack(sharing)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(entered.wait(), 2)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert not shared_box[0].done()
    assert COMPONENT not in metrics.failures
    shared_box[0].cancel()


async def test_litellms_logging_worker_never_belongs_to_a_request(task_factory: None) -> None:
    pytest.importorskip("litellm")
    from litellm.litellm_core_utils.logging_worker import LoggingWorker

    worker = LoggingWorker()
    entered = asyncio.Event()

    async def restarting(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        # The worker (re)starts lazily on the first callback, inside a request.
        worker.start()
        entered.set()
        await asyncio.sleep(3600)

    gate, _, _, _ = _stack(restarting)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(entered.wait(), 2)
    worker_task = worker._worker_task
    assert worker_task is not None
    assert worker_task not in pending_request_tasks()

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert not worker_task.done()
    await worker.stop()


# ── the body: read under a deadline, before any slot is taken ────────────────


def _stalled_client(first: bytes = b"{") -> _Client:
    client = _Client()
    client.incoming = asyncio.Queue()
    client.incoming.put_nowait({"type": "http.request", "body": first, "more_body": True})
    return client


def _headers(client: _Client) -> dict[bytes, bytes]:
    start = next(m for m in client.sent if m["type"] == "http.response.start")
    return dict(start["headers"])


async def test_no_slot_is_held_while_a_body_is_still_arriving() -> None:
    app = _Holding()
    app.release.set()
    gate, limiter, metrics, _ = _stack(app, max_inflight=1, body_read_s=5.0)
    stalled = _stalled_client()
    stalling = asyncio.create_task(gate(_scope(), stalled.receive, stalled.send))
    await asyncio.sleep(0.05)

    assert limiter.inflight == 0
    assert limiter.draining == 1
    assert metrics.inflight == []
    normal = _Client()
    await asyncio.wait_for(gate(_scope(), normal.receive, normal.send), 2)
    assert normal.status == 200

    stalled.incoming.put_nowait({"type": "http.request", "body": b"}", "more_body": False})
    await asyncio.wait_for(stalling, 2)
    assert stalled.status == 200
    assert limiter.inflight == limiter.draining == 0


async def test_a_body_not_complete_within_the_deadline_gets_408_and_never_a_slot() -> None:
    app = _Holding()
    gate, limiter, metrics, sink = _stack(app, max_inflight=1, body_read_s=0.1)
    client = _stalled_client(CANARY.encode())
    loop = asyncio.get_running_loop()
    start = loop.time()

    await asyncio.wait_for(gate(_scope(), client.receive, client.send), 2)

    assert 0.1 <= loop.time() - start < 1.0
    assert client.status == 408
    assert client.json() == {
        "error": {
            "type": "body_timeout",
            "code": E_BODY_TIMEOUT,
            "route": "POST /v1/messages",
            "reason": ROUTE_GATE_BODY_TIMEOUT,
        }
    }
    assert app.calls == 0
    assert metrics.inflight == []
    assert (limiter.inflight, limiter.draining) == (0, 0)
    assert metrics.blocks == [ROUTE_GATE_BODY_TIMEOUT]
    assert metrics.failures == []
    assert metrics.cancelled == 0
    assert [r["block_reason"] for r in sink.records] == [ROUTE_GATE_BODY_TIMEOUT]
    assert sink.records[0]["error_code"] == E_BODY_TIMEOUT
    assert CANARY not in json.dumps(sink.records)


async def test_over_the_draining_cap_the_next_request_gets_429_unread() -> None:
    app = _Holding()
    app.release.set()
    gate, limiter, metrics, _ = _stack(app, max_inflight=1, max_draining=1, body_read_s=5.0)
    stalled = _stalled_client()
    stalling = asyncio.create_task(gate(_scope(), stalled.receive, stalled.send))
    await asyncio.sleep(0.05)
    assert limiter.draining == 1

    refused = _Client()
    await gate(_scope(), refused.receive, refused.send)

    assert refused.status == 429
    assert refused.json()["error"]["code"] == E_CAPACITY
    assert refused.receive_calls == 0
    assert _headers(refused)[b"retry-after"] == b"1"
    assert metrics.blocks == [ROUTE_GATE_CAPACITY]
    stalled.incoming.put_nowait({"type": "http.request", "body": b"}", "more_body": False})
    await asyncio.wait_for(stalling, 2)
    admitted = _Client()
    await gate(_scope(), admitted.receive, admitted.send)
    assert admitted.status == 200


async def test_the_capacity_refusal_asks_the_client_to_retry_after_a_second() -> None:
    app = _Holding()
    gate, _, _, _ = _stack(app, max_inflight=1)
    held = _Client()
    running = asyncio.create_task(gate(_scope(), held.receive, held.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    refused = _Client()
    await gate(_scope(), refused.receive, refused.send)

    assert refused.status == 429
    assert _headers(refused)[b"retry-after"] == b"1"
    app.release.set()
    await running
    assert b"retry-after" not in _headers(held)


async def test_a_request_that_loses_the_slot_after_its_body_arrived_gets_429() -> None:
    app = _Holding()
    gate, limiter, metrics, _ = _stack(app, max_inflight=1, body_read_s=5.0)
    slow = _stalled_client()
    slow_task = asyncio.create_task(gate(_scope(), slow.receive, slow.send))
    await asyncio.sleep(0.05)
    fast = _Client()
    fast_task = asyncio.create_task(gate(_scope(), fast.receive, fast.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    slow.incoming.put_nowait({"type": "http.request", "body": b"}", "more_body": False})
    await asyncio.wait_for(slow_task, 2)

    assert slow.status == 429
    assert app.calls == 1
    assert metrics.blocks == [ROUTE_GATE_CAPACITY]
    app.release.set()
    await fast_task
    assert fast.status == 200
    assert (limiter.inflight, limiter.draining) == (0, 0)


async def test_a_server_cancel_while_the_body_is_read_frees_the_draining_count() -> None:
    gate, limiter, metrics, _ = _stack(_Holding(), body_read_s=5.0)
    stalled = _stalled_client()
    task = asyncio.create_task(gate(_scope(), stalled.receive, stalled.send))
    await asyncio.sleep(0.05)
    assert limiter.draining == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (limiter.inflight, limiter.draining) == (0, 0)
    assert metrics.inflight == []


def test_the_draining_cap_defaults_to_four_times_the_inflight_cap() -> None:
    assert InflightLimiter(16, metrics=_Metrics()).max_draining == 64
    assert InflightLimiter(0, metrics=_Metrics()).max_draining == 0


@pytest.mark.parametrize(
    "limits",
    [
        {"max_draining": 3},
        {"max_draining": 0},
        {"max_draining": -1},
        {"max_draining": True},
        {"body_read_s": 0},
        {"body_read_s": -1.0},
        {"body_read_s": float("nan")},
        {"body_read_s": float("inf")},
        {"max_draining_bytes": MAX_BODY_BYTES - 1},
        {"max_draining_bytes": 0},
        {"max_draining_bytes": -1},
        {"max_draining_bytes": True},
        {"max_draining_bytes": 1.5e9},
    ],
)
def test_the_limiter_refuses_bad_drain_settings(limits: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        InflightLimiter(4, metrics=_Metrics(), **limits)


# ── the byte budget: body bytes read or held for replay ──────────────────────


def _sized_scope(length: int | bytes) -> dict[str, Any]:
    scope = _scope()
    value = length if isinstance(length, bytes) else str(length).encode()
    scope["headers"] = [*scope["headers"], (b"content-length", value)]
    return scope


def _budget_stack(app: Any, **limits: Any) -> tuple[RouteGateMiddleware, InflightLimiter, _Metrics]:
    limits.setdefault("max_inflight", 8)
    limits.setdefault("max_body_bytes", 10)
    limits.setdefault("max_draining_bytes", 16)
    limits.setdefault("body_read_s", 5.0)
    gate, limiter, metrics, _ = _stack(app, **limits)
    return gate, limiter, metrics


async def _hold(gate: Any, app: _Holding, chunks: tuple[bytes, ...], scope: Any = None) -> Any:
    client = _Client(chunks)
    task = asyncio.create_task(gate(scope or _scope(), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)
    return client, task


async def test_the_budget_counts_admitted_bodies_until_they_are_released() -> None:
    app = _Holding()
    gate, limiter, metrics = _budget_stack(app, max_draining_bytes=20)

    first, first_task = await _hold(gate, app, (b"12345",))
    assert limiter.buffered_bytes == 5
    second, second_task = await _hold(gate, app, (b"123", b"4567"))
    # The replay has handed the body over; it still counts until release.
    assert limiter.buffered_bytes == 12

    app.release.set()
    await asyncio.gather(first_task, second_task)

    assert (first.status, second.status) == (200, 200)
    assert limiter.buffered_bytes == 0
    assert metrics.draining_bytes[-1] == 0
    assert max(metrics.draining_bytes) == 12


async def test_a_declared_length_over_the_budget_gets_429_unread() -> None:
    app = _Holding()
    gate, limiter, metrics = _budget_stack(app)
    _, held_task = await _hold(gate, app, (b"1234567890",), _sized_scope(10))
    assert limiter.buffered_bytes == 10

    refused = _Client((CANARY.encode()[:10],))
    await gate(_sized_scope(10), refused.receive, refused.send)

    assert refused.status == 429
    assert refused.json()["error"]["code"] == E_CAPACITY
    assert _headers(refused)[b"retry-after"] == b"1"
    assert refused.receive_calls == 0
    assert app.calls == 1
    assert metrics.blocks == [ROUTE_GATE_CAPACITY]
    assert limiter.buffered_bytes == 10
    assert limiter.draining == 0

    app.release.set()
    await held_task
    admitted = _Client((b"1234567890",))
    await gate(_sized_scope(10), admitted.receive, admitted.send)
    assert admitted.status == 200
    assert limiter.buffered_bytes == 0


async def test_a_chunked_body_that_overruns_the_budget_mid_read_gets_429() -> None:
    app = _Holding()
    gate, limiter, metrics = _budget_stack(app)
    _, held_task = await _hold(gate, app, (b"1234567890",))

    refused = _Client((b"1234", CANARY.encode()[:4]))
    await gate(_scope(), refused.receive, refused.send)

    assert refused.status == 429
    assert refused.json()["error"]["reason"] == ROUTE_GATE_CAPACITY
    assert CANARY[:4] not in json.dumps(refused.json())
    assert refused.receive_calls == 2
    assert app.calls == 1
    assert metrics.blocks == [ROUTE_GATE_CAPACITY]
    # What the refused body held so far went back; the admitted one still counts.
    assert limiter.buffered_bytes == 10
    assert (limiter.inflight, limiter.draining) == (1, 0)

    app.release.set()
    await held_task
    assert limiter.buffered_bytes == 0


async def test_a_body_that_fills_the_budget_exactly_is_admitted() -> None:
    app = _Holding()
    gate, limiter, metrics = _budget_stack(app, max_draining_bytes=20)
    _, first_task = await _hold(gate, app, (b"1234567890",), _sized_scope(10))
    second, second_task = await _hold(gate, app, (b"12345", b"67890"))

    assert limiter.buffered_bytes == 20
    assert metrics.blocks == []
    app.release.set()
    await asyncio.gather(first_task, second_task)
    assert second.status == 200
    assert limiter.buffered_bytes == 0


async def test_a_declared_length_past_the_body_cap_still_gets_the_oversize_refusal() -> None:
    app = _Holding()
    gate, limiter, metrics = _budget_stack(app, max_draining_bytes=10)
    client = _Client((b"1234567890", b"1"))

    await gate(_sized_scope(4096), client.receive, client.send)

    assert client.status == 422
    assert metrics.blocks == ["oversize:blocked"]
    assert limiter.buffered_bytes == 0


@pytest.mark.parametrize("length", [b"ten", b"-4", b"", b"1e3"])
async def test_an_unreadable_declared_length_is_accounted_chunk_by_chunk(length: bytes) -> None:
    app = _Holding()
    gate, limiter, _ = _budget_stack(app)
    client, task = await _hold(gate, app, (b"12345",), _sized_scope(length))

    assert limiter.buffered_bytes == 5
    app.release.set()
    await task
    assert client.status == 200
    assert limiter.buffered_bytes == 0


async def test_two_declared_lengths_are_accounted_chunk_by_chunk() -> None:
    app = _Holding()
    gate, limiter, _ = _budget_stack(app)
    scope = _sized_scope(10)
    scope["headers"].append((b"content-length", b"10"))
    _, task = await _hold(gate, app, (b"123",), scope)

    assert limiter.buffered_bytes == 3
    app.release.set()
    await task
    assert limiter.buffered_bytes == 0


async def test_the_budget_is_given_back_when_the_downstream_raises() -> None:
    async def failing(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        raise RuntimeError("upstream exploded")

    gate, limiter, metrics = _budget_stack(failing)
    client = _Client((b"12345",))

    with pytest.raises(RuntimeError):
        await gate(_sized_scope(5), client.receive, client.send)

    assert limiter.buffered_bytes == 0
    assert metrics.draining_bytes == [5, 0]


async def test_the_budget_is_given_back_on_a_disconnect_while_the_body_is_read() -> None:
    gate, limiter, metrics = _budget_stack(_Holding())
    client = _stalled_client(b"1234")
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.sleep(0.05)
    assert limiter.buffered_bytes == 4

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert limiter.buffered_bytes == 0
    assert metrics.cancelled == 1


async def test_the_budget_is_given_back_on_a_disconnect_while_the_downstream_runs() -> None:
    app = _Holding()
    gate, limiter, metrics = _budget_stack(app)
    client, task = await _hold(gate, app, (b"12345",))
    assert limiter.buffered_bytes == 5

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert app.cancelled == 1
    assert (limiter.inflight, limiter.buffered_bytes) == (0, 0)
    assert metrics.draining_bytes[-1] == 0


async def test_the_budget_is_given_back_when_the_server_cancels_the_request() -> None:
    app = _Holding()
    gate, limiter, _ = _budget_stack(app)
    _, draining_task = await _hold(gate, app, (b"123",))
    stalled = _stalled_client(b"12")
    stalling = asyncio.create_task(gate(_sized_scope(9), stalled.receive, stalled.send))
    await asyncio.sleep(0.05)
    assert limiter.buffered_bytes == 12

    for task in (draining_task, stalling):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert (limiter.inflight, limiter.draining, limiter.buffered_bytes) == (0, 0, 0)


async def test_the_budget_is_given_back_on_a_body_timeout_and_a_lost_slot() -> None:
    app = _Holding()
    gate, limiter, metrics = _budget_stack(app, max_inflight=1, body_read_s=0.1)
    timed_out = _stalled_client(b"123")
    await asyncio.wait_for(gate(_scope(), timed_out.receive, timed_out.send), 2)
    assert timed_out.status == 408
    assert limiter.buffered_bytes == 0

    _, held_task = await _hold(gate, app, (b"12345",))
    lost = _Client((b"1234",))
    await gate(_scope(), lost.receive, lost.send)
    assert lost.status == 429
    assert lost.receive_calls == 0
    assert limiter.buffered_bytes == 5

    app.release.set()
    await held_task
    assert limiter.buffered_bytes == 0
    assert metrics.draining_bytes[-1] == 0


async def test_a_body_that_loses_the_slot_after_it_was_read_gives_its_bytes_back() -> None:
    app = _Holding()
    gate, limiter, _ = _budget_stack(app, max_inflight=1, max_draining_bytes=20)
    slow = _stalled_client(b"123")
    slow_task = asyncio.create_task(gate(_scope(), slow.receive, slow.send))
    await asyncio.sleep(0.05)
    _, held_task = await _hold(gate, app, (b"12345",))
    assert limiter.buffered_bytes == 8

    slow.incoming.put_nowait({"type": "http.request", "body": b"4", "more_body": False})
    await asyncio.wait_for(slow_task, 2)

    assert slow.status == 429
    assert limiter.buffered_bytes == 5
    app.release.set()
    await held_task
    assert limiter.buffered_bytes == 0


def test_the_byte_budget_defaults_to_512_mib() -> None:
    assert DEFAULT_MAX_DRAINING_BYTES == 512 * 1024 * 1024
    assert InflightLimiter(16, metrics=_Metrics()).max_draining_bytes == DEFAULT_MAX_DRAINING_BYTES


# ── the cancel path's bound ──────────────────────────────────────────────────


async def test_a_cancelled_request_holds_its_slot_at_most_twice_the_grace(
    task_factory: None,
) -> None:
    stop = asyncio.Event()
    entered = asyncio.Event()

    async def ignore_cancel() -> None:
        while not stop.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue

    async def stubborn(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        bind_call_id("call-1")
        bind_call_id("call-2")
        stragglers.append(asyncio.ensure_future(ignore_cancel()))  # tagged
        entered.set()
        await ignore_cancel()

    grace = 0.5
    gate, limiter, metrics, _ = _stack(stubborn, grace=grace)
    hook_calls: list[str] = []
    stragglers: list[asyncio.Task[None]] = []

    async def stalled_hook(request_id: str, *, latency_ms: int = 0) -> None:
        hook_calls.append(request_id)
        await asyncio.sleep(3600)

    limiter.bind_cancel_hook(stalled_hook)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(entered.wait(), 2)
    loop = asyncio.get_running_loop()
    start = loop.time()

    client.disconnect()
    await asyncio.wait_for(task, 5)

    assert loop.time() - start < 2 * grace + 0.3
    assert sorted(hook_calls) == ["call-1", "call-2"]
    assert limiter.inflight == 0
    assert COMPONENT in metrics.failures
    stop.set()
    await asyncio.wait(stragglers, timeout=1)


# ── a server cancel leaves nothing for the loop's exception handler ──────────


async def test_a_server_cancel_leaves_no_unretrieved_exception() -> None:
    from tests.loop_errors import describe, loop_errors

    entered = asyncio.Event()

    async def failing_on_cancel(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        entered.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError(CANARY) from None

    gate, limiter, _, _ = _stack(failing_on_cancel)
    client = _Client()
    async with loop_errors(settle_s=0.05) as seen:
        task = asyncio.create_task(gate(_scope(), client.receive, client.send))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        del task

    assert seen == [], describe(seen)
    assert limiter.inflight == 0


async def test_a_straggler_that_fails_while_cancelled_leaves_nothing_unretrieved(
    task_factory: None, caplog: pytest.LogCaptureFixture
) -> None:
    from tests.loop_errors import describe, loop_errors

    entered = asyncio.Event()

    async def fails_on_cancel() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError(CANARY) from None

    async def leaky(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        # Tagged; held weakly, so an unretrieved failure would reach the handler.
        straggler.append(weakref.ref(asyncio.ensure_future(fails_on_cancel())))
        entered.set()
        await asyncio.sleep(3600)

    straggler: list[weakref.ref[asyncio.Task[None]]] = []
    gate, limiter, _, _ = _stack(leaky, grace=0.5)
    client = _Client()
    with caplog.at_level(logging.DEBUG):
        async with loop_errors(settle_s=0.05) as seen:
            task = asyncio.create_task(gate(_scope(), client.receive, client.send))
            await asyncio.wait_for(entered.wait(), 2)
            client.disconnect()
            await asyncio.wait_for(task, 2)
            del task

    assert straggler[0]() is None
    assert seen == [], describe(seen)
    assert CANARY not in caplog.text
    assert "RuntimeError" in caplog.text
    assert limiter.inflight == 0


async def test_a_second_server_cancel_during_the_grace_leaves_nothing_unretrieved() -> None:
    from tests.loop_errors import describe, loop_errors

    entered = asyncio.Event()
    unwinding = asyncio.Event()

    async def slow_to_fail(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        entered.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            unwinding.set()
            await asyncio.sleep(0.05)
            raise RuntimeError(CANARY) from None

    gate, limiter, _, _ = _stack(slow_to_fail, grace=1.0)
    client = _Client()
    async with loop_errors(settle_s=0.2) as seen:
        task = asyncio.create_task(gate(_scope(), client.receive, client.send))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.wait_for(unwinding.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        del task

    assert seen == [], describe(seen)
    assert limiter.inflight == 0


async def test_a_server_cancel_during_the_disconnect_grace_leaves_nothing_unretrieved(
    task_factory: None,
) -> None:
    from tests.loop_errors import describe, loop_errors

    entered = asyncio.Event()
    unwinding = asyncio.Event()

    async def fails_on_cancel() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise RuntimeError(CANARY) from None

    async def slow_to_fail(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        # Tagged; held weakly, so an unretrieved failure would reach the handler.
        straggler.append(weakref.ref(asyncio.ensure_future(fails_on_cancel())))
        entered.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            unwinding.set()
            await asyncio.sleep(0.05)
            raise RuntimeError(CANARY) from None

    straggler: list[weakref.ref[asyncio.Task[None]]] = []
    gate, limiter, _, _ = _stack(slow_to_fail, grace=1.0)
    client = _Client()
    async with loop_errors(settle_s=0.2) as seen:
        task = asyncio.create_task(gate(_scope(), client.receive, client.send))
        await asyncio.wait_for(entered.wait(), 2)
        client.disconnect()
        await asyncio.wait_for(unwinding.wait(), 2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        del task

    assert straggler[0]() is None
    assert seen == [], describe(seen)
    assert limiter.inflight == 0


# ── the ticket's end ─────────────────────────────────────────────────────────


class _Closes:
    """A close hook that records the ticket state it saw."""

    def __init__(self, limiter: InflightLimiter | None = None) -> None:
        self.limiter = limiter
        self.calls: list[tuple[str, bool, int | None]] = []

    def __call__(self, ticket: RequestTicket) -> None:
        inflight = self.limiter.inflight if self.limiter is not None else None
        self.calls.append((ticket.gateway_id, ticket.cancelled, inflight))


def _registering(hook: Callable[[RequestTicket], None], app: Any) -> Any:
    async def registered(scope: Any, receive: Any, send: Any) -> None:
        ticket = current_ticket()
        assert ticket is not None and ticket.on_close(hook)
        await app(scope, receive, send)

    return registered


async def test_the_ticket_closes_once_when_the_request_ends_while_it_holds_the_slot() -> None:
    closes = _Closes()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await _respond(send)

    gate, limiter, _, _ = _stack(_registering(closes, app))
    closes.limiter = limiter
    client = _Client()

    await gate(_scope(), client.receive, client.send)

    assert client.status == 200
    assert [(cancelled, inflight) for _, cancelled, inflight in closes.calls] == [(False, 1)]
    assert limiter.inflight == 0


async def test_a_downstream_that_ignores_cancellation_still_closes_its_ticket() -> None:
    """The grace-release path: the limiter lets go of a downstream that never unwinds, and
    closing the ticket is what frees what the request left behind."""
    stop = asyncio.Event()
    entered = asyncio.Event()
    closes = _Closes()
    tickets: list[RequestTicket] = []

    async def stubborn(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        ticket = current_ticket()
        assert ticket is not None
        tickets.append(ticket)
        ticket.on_close(closes)
        entered.set()
        while not stop.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue

    gate, limiter, _, _ = _stack(stubborn, grace=0.1)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(entered.wait(), 2)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert [cancelled for _, cancelled, _ in closes.calls] == [True]
    assert tickets[0].closed and limiter.inflight == 0
    stop.set()
    await asyncio.sleep(0.05)
    assert len(closes.calls) == 1


async def test_the_ticket_closes_when_the_server_cancels_the_request() -> None:
    closes = _Closes()
    app = _Holding()
    gate, limiter, _, _ = _stack(_registering(closes, app))
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert len(closes.calls) == 1 and limiter.inflight == 0


@pytest.mark.parametrize(
    ("end", "expected"),
    [("response", (None, False)), ("client", ("client", True)), ("server", ("server", False))],
)
async def test_the_ticket_records_who_cancelled_the_request(
    end: str, expected: tuple[str | None, bool]
) -> None:
    """``cancel_origin`` is set before the downstream is cancelled. A server cancel leaves
    ``cancelled`` alone: that flag means a client left and the guardrail writes its record."""
    at_close: list[tuple[str | None, bool]] = []
    at_cancel: list[str | None] = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        ticket = current_ticket()
        assert ticket is not None
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            at_cancel.append(ticket.cancel_origin)
            raise
        await _respond(send)

    def hook(ticket: RequestTicket) -> None:
        at_close.append((ticket.cancel_origin, ticket.cancelled))

    gate, limiter, _, _ = _stack(_registering(hook, app), grace=0.2)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(entered.wait(), 2)

    if end == "response":
        release.set()
    elif end == "client":
        client.disconnect()
    else:
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)

    assert at_close == [expected]
    assert at_cancel == ([] if end == "response" else [expected[0]])
    assert limiter.inflight == 0


async def test_a_disconnect_while_the_body_is_read_is_the_clients_cancel() -> None:
    seen: list[tuple[str | None, bool]] = []

    async def hook(request_id: str, *, latency_ms: int = 0) -> None:
        ticket = current_ticket()
        assert ticket is not None
        seen.append((ticket.cancel_origin, ticket.cancelled))

    app = _Holding()
    gate, limiter, _, _ = _stack(app)
    limiter.bind_cancel_hook(hook)
    client = _stalled_client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.sleep(0.05)
    assert limiter.draining == 1

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert seen == [(CANCEL_CLIENT, True)]
    assert app.calls == 0 and limiter.draining == 0


@pytest.mark.parametrize(
    ("first", "then", "expected"),
    [
        (CANCEL_CLIENT, CANCEL_SERVER, (CANCEL_CLIENT, True)),
        (CANCEL_SERVER, CANCEL_CLIENT, (CANCEL_SERVER, False)),
    ],
)
def test_the_first_origin_marked_stays(first: str, then: str, expected: tuple[str, bool]) -> None:
    ticket = RequestTicket("f" * 32)

    ticket.mark_cancelled(first)
    ticket.mark_cancelled(then)

    assert (ticket.cancel_origin, ticket.cancelled) == expected


async def test_a_hook_offered_to_a_closed_ticket_is_refused_and_never_run() -> None:
    ticket = RequestTicket("f" * 32)
    ran: list[RequestTicket] = []

    assert ticket.close() == 0
    assert ticket.on_close(ran.append) is False
    assert ticket.close() == 0
    assert ran == []


def test_a_hook_is_registered_once_and_run_once() -> None:
    ticket = RequestTicket("f" * 32)
    ran: list[RequestTicket] = []

    assert ticket.on_close(ran.append) and ticket.on_close(ran.append)
    ticket.close()
    ticket.close()

    assert ran == [ticket]


async def test_a_failing_close_hook_is_counted_content_free_and_the_rest_still_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    after = _Closes()

    def failing(ticket: RequestTicket) -> None:
        raise RuntimeError(CANARY)

    async def app(scope: Any, receive: Any, send: Any) -> None:
        ticket = current_ticket()
        assert ticket is not None
        ticket.on_close(failing)
        ticket.on_close(after)
        await _read_body(receive)
        await _respond(send)

    gate, limiter, metrics, _ = _stack(app)
    client = _Client()

    with caplog.at_level(logging.DEBUG):
        await gate(_scope(), client.receive, client.send)

    assert len(after.calls) == 1 and limiter.inflight == 0
    assert metrics.failures == [COMPONENT]
    assert "route_gate_ticket_close_hook_failed" in caplog.text and "RuntimeError" in caplog.text
    assert CANARY not in caplog.text
