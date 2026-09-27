"""The in-flight cap behind the route gate: admission, body replay, disconnect-aware
cancellation and exactly-once slot release. The served-stack proof of the same
behaviour on a real socket is ``tests/test_inflight_served_stack.py``."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.metrics import MetricsExporter
from corp_llm_gateway.route_gate import RouteGateMiddleware
from corp_llm_gateway.route_gate.inflight import (
    E_CAPACITY,
    MAX_BODY_BYTES,
    ROUTE_GATE_CAPACITY,
    InflightLimiter,
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
) -> tuple[RouteGateMiddleware, InflightLimiter, _Metrics, ListSink]:
    metrics = metrics if metrics is not None else _Metrics()
    sink = sink if sink is not None else ListSink()
    limiter = InflightLimiter(
        max_inflight, metrics=metrics, cancel_grace_s=grace, max_body_bytes=max_body_bytes
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
