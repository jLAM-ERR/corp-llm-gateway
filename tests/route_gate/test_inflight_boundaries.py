"""The in-flight limiter at its edges: declared lengths that lie, the exact body
cap, concurrent reservations that fill the byte budget, send failures at each
point of a response, streams that end without a final chunk, a receive that
fails, many sequential requests, and task tagging through nested shared tasks,
explicit contexts and a chained task factory."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from typing import Any

import pytest

from corp_llm_gateway.route_gate.inflight import (
    MAX_BODY_BYTES,
    install_task_factory,
    pending_request_tasks,
    spawn_shared,
)
from tests.route_gate.test_inflight import (
    CANARY,
    Message,
    _budget_stack,
    _CancelHook,
    _Client,
    _Holding,
    _read_body,
    _respond,
    _scope,
    _sized_scope,
    _stack,
)


@pytest.fixture
async def task_factory() -> Any:
    restore = install_task_factory(asyncio.get_running_loop())
    yield
    restore()


async def _forever() -> None:
    await asyncio.sleep(3600)


# ── declared lengths ─────────────────────────────────────────────────────────


async def test_a_declared_length_longer_than_the_body_is_served_and_fully_given_back() -> None:
    app = _Holding()
    gate, limiter, metrics = _budget_stack(app)
    client = _Client((b"123",))
    task = asyncio.create_task(gate(_sized_scope(9), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    # The reservation stands on the declared length while the request runs.
    assert limiter.buffered_bytes == 9
    assert app.bodies == [b"123"]
    app.release.set()
    await task

    assert client.status == 200
    assert limiter.buffered_bytes == 0
    assert metrics.draining_bytes[-1] == 0


async def test_a_zero_declared_length_with_a_chunked_body_is_accounted_as_it_arrives() -> None:
    app = _Holding()
    gate, limiter, _ = _budget_stack(app)
    client = _Client((b"12", b"345", b"6"))
    task = asyncio.create_task(gate(_sized_scope(0), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    assert app.bodies == [b"123456"]
    assert limiter.buffered_bytes == 6
    app.release.set()
    await task
    assert client.status == 200
    assert limiter.buffered_bytes == 0


@pytest.mark.parametrize("length", [b"+5", b"5,5", b"0x5", b"\xd9\xa5", b"5 5"])
async def test_a_declared_length_that_is_not_plain_ascii_digits_reserves_nothing(
    length: bytes,
) -> None:
    app = _Holding()
    gate, limiter, _ = _budget_stack(app)
    client = _Client((b"1234567",))
    task = asyncio.create_task(gate(_sized_scope(length), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    assert limiter.buffered_bytes == 7
    app.release.set()
    await task
    assert client.status == 200
    assert limiter.buffered_bytes == 0


# ── the real body cap ────────────────────────────────────────────────────────


@pytest.mark.parametrize(("extra", "status"), [(0, 200), (1, 422)], ids=["at-cap", "cap+1"])
async def test_the_default_body_cap_is_exact(extra: int, status: int) -> None:
    seen: list[int] = []

    async def measuring(scope: Any, receive: Any, send: Any) -> None:
        seen.append(len(await _read_body(receive)))
        await _respond(send)

    mib = 1024 * 1024
    chunks = tuple(b"x" * mib for _ in range(MAX_BODY_BYTES // mib))
    if extra:
        chunks = (*chunks, b"y" * extra)
    gate, limiter, metrics, _ = _stack(measuring, max_body_bytes=MAX_BODY_BYTES)
    client = _Client(chunks)

    await gate(_scope(), client.receive, client.send)

    assert client.status == status
    assert seen == ([MAX_BODY_BYTES] if status == 200 else [])
    assert limiter.inflight == 0
    assert limiter.buffered_bytes == 0
    if status == 422:
        assert metrics.blocks == ["oversize:blocked"]


# ── the byte budget under concurrent reservations ────────────────────────────


async def test_concurrent_reservations_that_fill_the_budget_exactly_are_all_admitted() -> None:
    app = _Holding()
    gate, limiter, metrics = _budget_stack(app, max_body_bytes=10, max_draining_bytes=16)
    first = _Client((b"12345678",))
    second = _Client((b"abcdefgh",))
    tasks = [asyncio.create_task(gate(_sized_scope(8), c.receive, c.send)) for c in (first, second)]
    for _ in tasks:
        await asyncio.wait_for(app.entered.acquire(), 2)
    assert limiter.buffered_bytes == 16

    one_over = _Client((b"z",))
    await gate(_sized_scope(1), one_over.receive, one_over.send)
    assert one_over.status == 429
    assert one_over.receive_calls == 0
    assert limiter.buffered_bytes == 16

    app.release.set()
    await asyncio.gather(*tasks)
    assert (first.status, second.status) == (200, 200)
    assert limiter.buffered_bytes == 0
    assert metrics.draining_bytes[-1] == 0


# ── send failures at each point of a response ────────────────────────────────


@pytest.mark.parametrize("failing_at", [0, 1, 2], ids=["start", "mid-body", "final-body"])
async def test_a_send_failure_anywhere_in_the_response_frees_slot_and_budget(
    failing_at: int,
) -> None:
    async def streaming(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        await send({"type": "http.response.body", "body": b"b"})

    gate, limiter, metrics, _ = _stack(streaming, max_inflight=1)
    hook = _CancelHook()
    limiter.bind_cancel_hook(hook)
    client = _Client((b"{}",))
    sent = 0

    async def breaking(message: Message) -> None:
        nonlocal sent
        if sent == failing_at:
            raise OSError("socket gone")
        sent += 1

    with pytest.raises(OSError):
        await gate(_scope(), client.receive, breaking)

    assert limiter.inflight == 0
    assert limiter.buffered_bytes == 0
    assert metrics.cancelled == 0
    assert hook.calls == []
    nxt = _Client()
    await gate(_scope(), nxt.receive, nxt.send)
    assert nxt.status == 200


# ── responses that end oddly ─────────────────────────────────────────────────


async def test_a_stream_that_returns_without_a_final_chunk_is_not_a_cancellation() -> None:
    async def unterminated(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: 1\n\n", "more_body": True})

    gate, limiter, metrics, _ = _stack(unterminated, max_inflight=1)
    hook = _CancelHook()
    limiter.bind_cancel_hook(hook)
    client = _Client()

    await asyncio.wait_for(gate(_scope(), client.receive, client.send), 2)
    client.disconnect()  # the server's disconnect, after the app returned
    await asyncio.sleep(0.05)

    assert client.status == 200
    assert metrics.cancelled == 0
    assert hook.calls == []
    assert limiter.inflight == 0


async def test_a_downstream_that_returns_without_responding_releases_its_slot() -> None:
    async def silent(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)

    gate, limiter, metrics, _ = _stack(silent, max_inflight=1)
    client = _Client()

    await asyncio.wait_for(gate(_scope(), client.receive, client.send), 2)

    assert client.sent == []
    assert metrics.cancelled == 0
    assert limiter.inflight == 0
    assert limiter.buffered_bytes == 0


# ── a failing receive ────────────────────────────────────────────────────────


async def test_a_receive_that_fails_after_the_body_cancels_and_logs_the_class_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _Holding()
    gate, limiter, metrics, sink = _stack(app, max_inflight=1)
    hook = _CancelHook()
    limiter.bind_cancel_hook(hook)
    fail = asyncio.Event()
    calls = 0

    async def receive() -> Message:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": b"{}", "more_body": False}
        await fail.wait()
        raise OSError(f"connection reset near {CANARY}")

    client = _Client()
    with caplog.at_level(logging.DEBUG):
        task = asyncio.create_task(gate(_scope(), receive, client.send))
        await asyncio.wait_for(app.entered.acquire(), 2)
        fail.set()
        await asyncio.wait_for(task, 2)

    assert app.cancelled == 1
    assert metrics.cancelled == 1
    assert len(hook.calls) == 1
    assert limiter.inflight == 0
    assert "OSError" in caplog.text
    assert CANARY not in caplog.text
    assert CANARY not in json.dumps(sink.records)


async def test_a_downstream_that_fails_while_cancelled_logs_the_class_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.loop_errors import describe, loop_errors

    entered = asyncio.Event()

    async def failing_on_cancel(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        entered.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError(CANARY) from None

    gate, limiter, metrics, _ = _stack(failing_on_cancel)
    client = _Client()
    with caplog.at_level(logging.DEBUG):
        async with loop_errors(settle_s=0.05) as seen:
            task = asyncio.create_task(gate(_scope(), client.receive, client.send))
            await asyncio.wait_for(entered.wait(), 2)
            client.disconnect()
            await asyncio.wait_for(task, 2)
            del task

    assert seen == [], describe(seen)
    assert "RuntimeError" in caplog.text
    assert CANARY not in caplog.text
    assert metrics.cancelled == 1
    assert limiter.inflight == 0


# ── no drift ─────────────────────────────────────────────────────────────────


async def test_a_thousand_sequential_requests_leave_every_count_at_zero() -> None:
    async def echo(scope: Any, receive: Any, send: Any) -> None:
        await _respond(send, await _read_body(receive))

    gate, limiter, metrics, _ = _stack(echo, max_inflight=1, max_body_bytes=64)
    hook = _CancelHook()
    limiter.bind_cancel_hook(hook)

    for index in range(1000):
        client = _Client((str(index).encode(),))
        await gate(_sized_scope(len(str(index))), client.receive, client.send)
        assert client.status == 200

    assert limiter.inflight == 0
    assert limiter.draining == 0
    assert limiter.buffered_bytes == 0
    assert metrics.inflight[-1] == 0
    assert max(metrics.inflight) == 1
    assert metrics.draining_bytes[-1] == 0
    assert metrics.cancelled == 0
    assert metrics.blocks == []
    assert hook.calls == []
    assert pending_request_tasks() == []


async def test_a_thousand_disconnects_leave_every_count_at_zero(task_factory: None) -> None:
    app = _Holding()
    gate, limiter, metrics, _ = _stack(app, max_inflight=1, grace=0.5)
    hook = _CancelHook()
    limiter.bind_cancel_hook(hook)

    for _ in range(1000):
        client = _Client()
        task = asyncio.create_task(gate(_scope(), client.receive, client.send))
        await asyncio.wait_for(app.entered.acquire(), 2)
        client.disconnect()
        await asyncio.wait_for(task, 2)

    assert app.cancelled == 1000
    assert metrics.cancelled == 1000
    assert len(hook.calls) == 1000
    assert limiter.inflight == 0
    assert limiter.buffered_bytes == 0
    assert metrics.inflight[-1] == 0
    assert pending_request_tasks() == []


# ── task tagging ─────────────────────────────────────────────────────────────


async def test_tasks_started_inside_a_shared_task_belong_to_no_request(
    task_factory: None,
) -> None:
    started: list[asyncio.Task[Any]] = []
    entered = asyncio.Event()

    async def outer() -> None:
        started.append(spawn_shared(_forever(), name="inner-shared"))
        started.append(asyncio.ensure_future(_forever()))
        await _forever()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        started.append(spawn_shared(outer(), name="outer-shared"))
        entered.set()
        await asyncio.sleep(3600)

    gate, limiter, _, _ = _stack(app)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.sleep(0.01)
    assert len(started) == 3
    assert not set(started) & set(pending_request_tasks())

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert all(not t.done() for t in started)
    assert limiter.inflight == 0
    for t in started:
        t.cancel()
    await asyncio.gather(*started, return_exceptions=True)


async def test_a_task_the_request_starts_with_a_copy_of_its_context_is_its_own(
    task_factory: None,
) -> None:
    box: list[asyncio.Task[Any]] = []
    entered = asyncio.Event()

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        loop = asyncio.get_running_loop()
        box.append(loop.create_task(_forever(), context=contextvars.copy_context()))
        entered.set()
        await asyncio.sleep(3600)

    gate, limiter, _, _ = _stack(app)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(entered.wait(), 2)
    assert box[0] in pending_request_tasks()

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert box[0].cancelled()
    assert pending_request_tasks() == []
    assert limiter.inflight == 0


async def test_tagging_stacks_on_a_previous_task_factory() -> None:
    # uvloop, or anything else, may already own the loop's task factory.
    loop = asyncio.get_running_loop()
    made: list[asyncio.Task[Any]] = []

    def previous(loop_: asyncio.AbstractEventLoop, coro: Any, **kwargs: Any) -> Any:
        task = asyncio.Task(coro, loop=loop_, **kwargs)
        made.append(task)
        return task

    original = loop.get_task_factory()
    loop.set_task_factory(previous)
    restore = install_task_factory(loop)
    try:
        box: list[asyncio.Task[Any]] = []
        entered = asyncio.Event()

        async def app(scope: Any, receive: Any, send: Any) -> None:
            await _read_body(receive)
            box.append(asyncio.ensure_future(_forever()))
            entered.set()
            await asyncio.sleep(3600)

        gate, limiter, _, _ = _stack(app)
        client = _Client()
        task = asyncio.create_task(gate(_scope(), client.receive, client.send))
        await asyncio.wait_for(entered.wait(), 2)

        assert box[0] in made
        assert box[0] in pending_request_tasks()
        client.disconnect()
        await asyncio.wait_for(task, 2)
        assert box[0].cancelled()
        assert limiter.inflight == 0
    finally:
        restore()
        assert loop.get_task_factory() is previous
        loop.set_task_factory(original)


async def test_a_task_the_request_left_finished_is_not_touched_by_the_sweep(
    task_factory: None,
) -> None:
    box: list[asyncio.Task[Any]] = []
    entered = asyncio.Event()

    async def quick() -> str:
        return "done"

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        box.append(asyncio.ensure_future(quick()))
        await box[0]
        entered.set()
        await asyncio.sleep(3600)

    gate, limiter, metrics, _ = _stack(app)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(entered.wait(), 2)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert box[0].result() == "done"
    assert metrics.failures == []
    assert limiter.inflight == 0
