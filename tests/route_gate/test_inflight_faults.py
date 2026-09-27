"""The in-flight limiter when something it depends on misbehaves: a metrics
exporter that raises, and a receive still waiting after the downstream ended."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import pytest

from corp_llm_gateway.route_gate.inflight import RequestTicket, bind_call_id, current_ticket
from tests.route_gate.test_inflight import (
    _CancelHook,
    _Client,
    _Holding,
    _Metrics,
    _read_body,
    _respond,
    _scope,
    _sized_scope,
    _stack,
)


class _FlakyMetrics(_Metrics):
    """Raises once from the named method, then behaves."""

    def __init__(self, failing: str) -> None:
        super().__init__()
        self.failing = failing

    def _maybe_fail(self, name: str) -> None:
        if self.failing == name:
            self.failing = ""
            raise RuntimeError("exporter down")

    def set_inflight(self, count: int) -> None:
        self._maybe_fail("set_inflight")
        super().set_inflight(count)

    def record_cancelled(self) -> None:
        self._maybe_fail("record_cancelled")
        super().record_cancelled()


async def test_an_exporter_failure_on_admission_never_leaks_the_slot() -> None:
    async def ok(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await _respond(send)

    metrics = _FlakyMetrics("set_inflight")
    gate, limiter, _, _ = _stack(ok, max_inflight=1, metrics=metrics)
    first = _Client()
    with contextlib.suppress(RuntimeError):
        await gate(_scope(), first.receive, first.send)

    assert limiter.inflight == 0
    nxt = _Client()
    await gate(_scope(), nxt.receive, nxt.send)
    assert nxt.status == 200


async def test_an_exporter_failure_on_a_disconnect_still_tells_the_guardrail() -> None:
    # The guardrail's cancel hook is what frees the request's content.
    app = _Holding()
    metrics = _FlakyMetrics("record_cancelled")
    gate, limiter, _, _ = _stack(app, max_inflight=1, metrics=metrics)
    hook = _CancelHook()
    limiter.bind_cancel_hook(hook)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    client.disconnect()
    await asyncio.gather(task, return_exceptions=True)

    assert len(hook.calls) == 1
    assert limiter.inflight == 0


async def test_a_receive_left_waiting_after_the_downstream_returned_gets_disconnect() -> None:
    # uvicorn answers http.disconnect from receive once the response is complete;
    # the replay must not leave a waiter hanging once its watcher is gone.
    leftover: list[asyncio.Future[Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await _respond(send)
        leftover.append(asyncio.ensure_future(receive()))

    gate, _, _, _ = _stack(app)
    client = _Client()
    await gate(_scope(), client.receive, client.send)
    client.disconnect()

    try:
        message = await asyncio.wait_for(leftover[0], 1)
    finally:
        leftover[0].cancel()
    assert message["type"] == "http.disconnect"


class _DeadExporter(_Metrics):
    """Every gauge and counter the limiter touches raises, every time."""

    def set_inflight(self, count: int) -> None:
        raise RuntimeError("exporter down: secret-9c1d")

    def set_draining_bytes(self, count: int) -> None:
        raise RuntimeError("exporter down: secret-9c1d")

    def record_cancelled(self) -> None:
        raise RuntimeError("exporter down: secret-9c1d")

    def record_failure(self, component: str) -> None:
        raise RuntimeError("exporter down: secret-9c1d")


async def test_a_dead_exporter_never_fails_a_request_or_holds_its_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def ok(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await _respond(send)

    gate, limiter, _, _ = _stack(ok, max_inflight=1, metrics=_DeadExporter())
    with caplog.at_level(logging.ERROR):
        for _ in range(2):
            client = _Client()
            await gate(_sized_scope(2), client.receive, client.send)
            assert client.status == 200

    assert (limiter.inflight, limiter.buffered_bytes) == (0, 0)
    assert "route_gate_metrics_error class=RuntimeError" in caplog.text
    assert "secret-9c1d" not in caplog.text


async def test_a_dead_exporter_on_a_failing_cancel_hook_still_frees_the_slot() -> None:
    app = _Holding()
    gate, limiter, _, _ = _stack(app, max_inflight=1, metrics=_DeadExporter())
    calls: list[str] = []

    async def failing(request_id: str, *, latency_ms: int = 0) -> None:
        calls.append(request_id)
        raise RuntimeError("hook down")

    limiter.bind_cancel_hook(failing)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    await asyncio.wait_for(app.entered.acquire(), 2)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    assert len(calls) == 1
    assert (limiter.inflight, limiter.buffered_bytes) == (0, 0)


async def test_the_cancel_hook_runs_in_the_cancelled_requests_context() -> None:
    # The guardrail tells its own request's state from another's by the ticket.
    tickets: list[RequestTicket | None] = []

    async def binding(scope: Any, receive: Any, send: Any) -> None:
        bind_call_id("call-1")
        tickets.append(current_ticket())
        await _read_body(receive)
        await asyncio.Event().wait()

    seen: list[tuple[str, RequestTicket | None]] = []

    async def hook(request_id: str, *, latency_ms: int = 0) -> None:
        seen.append((request_id, current_ticket()))

    gate, limiter, _, _ = _stack(binding, max_inflight=1)
    limiter.bind_cancel_hook(hook)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    while not tickets:
        await asyncio.sleep(0)

    client.disconnect()
    await asyncio.wait_for(task, 2)

    (ticket,) = tickets
    assert ticket is not None and ticket.cancelled
    assert seen == [("call-1", ticket)]
    assert current_ticket() is None


async def test_a_receive_while_the_response_is_still_streaming_waits_for_the_client() -> None:
    waiting: list[asyncio.Future[Any]] = []
    release = asyncio.Event()

    async def streaming(scope: Any, receive: Any, send: Any) -> None:
        await _read_body(receive)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        waiting.append(asyncio.ensure_future(receive()))
        await release.wait()
        await send({"type": "http.response.body", "body": b"b"})

    gate, _, _, _ = _stack(streaming)
    client = _Client()
    task = asyncio.create_task(gate(_scope(), client.receive, client.send))
    while not waiting:
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)

    assert not waiting[0].done()
    release.set()
    await asyncio.wait_for(task, 2)
    assert (await asyncio.wait_for(waiting[0], 1))["type"] == "http.disconnect"
