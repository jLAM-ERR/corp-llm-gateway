"""Child process for ``tests/test_inflight_served_stack.py``: the real entrypoint
(route gate + in-flight limiter + litellm + the guardrail) served by uvicorn on a
real socket, in front of an upstream stub on another real socket. Runs every
disconnect case, prints one ``@@RESULT@@`` JSON line.

Run as ``python tests/inflight_served_script.py <asyncio|uvloop>``; importing
``corp_llm_gateway.asgi`` IS the boot, so this cannot share the test process.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SENTINEL = "@@RESULT@@"
TOKEN = "served-dev-token"
BOUND_S = 30.0

CONFIG = """
model_list:
  - model_name: "corp-*"
    litellm_params:
      model: "hosted_vllm/stub"
      api_base: "http://127.0.0.1:{port}/v1"
      api_key: "stub"
litellm_settings:
  callbacks: ["corp_llm_gateway.bootstrap.guardrail"]
  drop_params: true
general_settings:
  cancel_on_disconnect: true
"""


def _listening_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    sock.setblocking(False)
    return sock


STUB_SOCK = _listening_socket()
_config_dir = Path(tempfile.mkdtemp(prefix="inflight-served-"))
(_config_dir / "config.yaml").write_text(CONFIG.format(port=STUB_SOCK.getsockname()[1]))
os.environ["CORP_LLM_LITELLM_CONFIG"] = str(_config_dir / "config.yaml")

import uvicorn  # noqa: E402

import corp_llm_gateway.asgi as asgi  # noqa: E402
from corp_llm_gateway.audit import AuditLogger, ListSink  # noqa: E402
from corp_llm_gateway.route_gate import inflight  # noqa: E402

_CHUNK = {
    "id": "c1",
    "object": "chat.completion.chunk",
    "created": 1,
    "model": "stub",
    "choices": [
        {"index": 0, "delta": {"role": "assistant", "content": "hi"}, "finish_reason": None}
    ],
}
_DONE_CHUNK = {**_CHUNK, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
_COMPLETION = {
    "id": "c1",
    "object": "chat.completion",
    "created": 1,
    "model": "stub",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_SSE_HEAD = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n"
    b"Connection: close\r\n\r\n"
)


def _sse(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


class Stub:
    """The upstream provider. ``mode`` decides how it answers the next request."""

    def __init__(self) -> None:
        self.requests = 0
        self.reset("ok")

    def reset(self, mode: str) -> None:
        self.mode = mode
        self.connected = asyncio.Event()
        self.closed = asyncio.Event()
        self.closed_at: float | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        mode, connected, closed = self.mode, self.connected, self.closed
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = 0
            for line in head.decode("latin-1").split("\r\n"):
                name, _, value = line.partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value)
            body = json.loads(await reader.readexactly(length))
            self.requests += 1
            connected.set()
            if mode == "ok":
                if body.get("stream"):
                    writer.write(_SSE_HEAD + _sse(_CHUNK) + _sse(_DONE_CHUNK) + b"data: [DONE]\n\n")
                else:
                    payload = json.dumps(_COMPLETION).encode()
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                        + payload
                    )
                await writer.drain()
                return
            if mode in ("sse-one", "sse-zero"):
                writer.write(_SSE_HEAD + (_sse(_CHUNK) if mode == "sse-one" else b""))
                await writer.drain()
            # Stalled: hold the socket until the gateway closes it.
            while await reader.read(65536):
                pass
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if self.closed_at is None and mode != "ok":
                self.closed_at = time.monotonic()
            closed.set()
            writer.close()


class StallOnceSink(ListSink):
    """A ListSink whose next write hangs, once — an audit emit in flight."""

    def __init__(self) -> None:
        super().__init__()
        self.stall_next = False
        self.entered = asyncio.Event()

    async def write(self, record: dict[str, Any]) -> None:
        if self.stall_next:
            self.stall_next = False
            self.entered.set()
            await asyncio.Event().wait()
        self.records.append(record)


async def _open(port: int, *, stream: bool, token: str | None, close: bool = False) -> Any:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body = json.dumps(
        {
            "model": "corp-probe",
            "messages": [{"role": "user", "content": "hello there"}],
            "stream": stream,
        }
    ).encode()
    head = (
        "POST /v1/chat/completions HTTP/1.1\r\nHost: gateway\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        + (f"X-Corp-Auth: {token}\r\n" if token else "")
        + ("Connection: close\r\n" if close else "")
        + "\r\n"
    )
    writer.write(head.encode() + body)
    await writer.drain()
    return reader, writer


async def _status(reader: asyncio.StreamReader) -> int:
    line = await asyncio.wait_for(reader.readline(), BOUND_S)
    return int(line.split()[1])


async def _complete(port: int, stub: Stub, *, stream: bool) -> tuple[int, bytes]:
    stub.reset("ok")
    reader, writer = await _open(port, stream=stream, token=TOKEN, close=True)
    try:
        status = await _status(reader)
        rest = await asyncio.wait_for(reader.read(), BOUND_S)
        return status, rest
    finally:
        writer.close()


async def _until(predicate: Any, bound: float = BOUND_S) -> float:
    start = time.monotonic()
    while not predicate():
        if time.monotonic() - start > bound:
            raise TimeoutError("condition not reached")
        await asyncio.sleep(0.002)
    return time.monotonic() - start


async def main() -> None:
    stub = Stub()
    stub_server = await asyncio.start_server(stub.handle, sock=STUB_SOCK)
    gw_sock = _listening_socket()
    port = gw_sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(asgi.app, lifespan="on", log_level="warning", access_log=False)
    )
    serving = asyncio.create_task(server.serve(sockets=[gw_sock]))
    await _until(lambda: server.started or serving.done())
    if serving.done():
        serving.result()

    import litellm
    from litellm.proxy import common_request_processing as crp

    guardrail = next(cb for cb in litellm.callbacks if type(cb).__name__ == "CorpLlmGuardrail")
    sink = StallOnceSink()
    guardrail._audit = AuditLogger(sink, gateway_version="served")
    limiter = asgi.limiter

    # Which of litellm's own disconnect watchers saw the disconnect through the
    # limiter's replay receive (they only read the replay, never the socket).
    seen: list[str] = []
    real_monitor = crp._cancel_llm_call_on_client_disconnect
    real_first_chunk_wait = crp._wait_for_http_disconnect

    async def monitor(request: Any, call: Any, event: Any) -> None:
        await real_monitor(request, call, event)
        if event.is_set():
            seen.append("cancel_on_disconnect_monitor")

    async def first_chunk_wait(request: Any) -> None:
        await real_first_chunk_wait(request)
        seen.append("first_chunk_disconnect_task")

    crp._cancel_llm_call_on_client_disconnect = monitor
    crp._wait_for_http_disconnect = first_chunk_wait

    warm = await _complete(port, stub, stream=False)
    results: dict[str, Any] = {"warmup": warm[0], "cases": {}}

    async def case(
        name: str,
        *,
        stream: bool,
        mode: str,
        token: str | None = TOKEN,
        entered: Any = None,
        read_first_chunk: bool = False,
        restore: Any = None,
    ) -> None:
        seen.clear()
        base_state = len(guardrail._req_state)
        base_records = len(sink.records)
        base_requests = stub.requests
        stub.reset(mode)
        reader, writer = await _open(port, stream=stream, token=token)
        waited_on = entered if entered is not None else stub.connected
        await asyncio.wait_for(waited_on.wait(), BOUND_S)
        client_saw: list[Any] = []
        if read_first_chunk:
            client_saw.append(await _status(reader))
            while await asyncio.wait_for(reader.readline(), BOUND_S) not in (b"\r\n", b""):
                pass
            client_saw.append((await asyncio.wait_for(reader.readuntil(b"\n\n"), BOUND_S)).decode())
        await asyncio.sleep(0.2)
        inflight_before = limiter.inflight
        tagged_while_stalled = len(inflight.pending_request_tasks())
        started = time.monotonic()
        writer.close()
        released_s = await _until(lambda: limiter.inflight <= 0)
        contacted = stub.requests > base_requests
        upstream_closed_s = None
        if contacted:
            await asyncio.wait_for(stub.closed.wait(), BOUND_S)
            upstream_closed_s = stub.closed_at - started if stub.closed_at else None
        await asyncio.sleep(0.5)  # late litellm callbacks, if any, land now
        records = sink.records[base_records:]
        pending = inflight.pending_request_tasks()
        inflight_after = limiter.inflight
        if restore is not None:
            restore()
        nxt = await _complete(port, stub, stream=stream)
        await _until(lambda: limiter.inflight <= 0)
        await asyncio.sleep(0.2)
        results["cases"][name] = {
            "inflight_before": inflight_before,
            "tagged_while_stalled": tagged_while_stalled,
            "released_s": released_s,
            "inflight_after": inflight_after,
            "inflight_after_next": limiter.inflight,
            "upstream_contacted": contacted,
            "upstream_closed_s": upstream_closed_s,
            "pending_request_tasks": len(pending),
            "req_state_delta": len(guardrail._req_state) - base_state,
            "statuses": [r["status"] for r in records],
            "error_codes": [r.get("error_code") for r in records],
            "client_saw": client_saw,
            "litellm_watchers": sorted(set(seen)),
            "next_status": nxt[0],
        }

    await case("a_before_headers", stream=True, mode="stall")
    await case("b_mid_sse", stream=True, mode="sse-one", read_first_chunk=True)
    await case("c_non_streaming", stream=False, mode="stall")

    stall_entered = asyncio.Event()

    async def stalled_profile(team_id: str) -> Any:
        stall_entered.set()
        await asyncio.Event().wait()

    def unstall() -> None:
        del guardrail._resolve_profile

    guardrail._resolve_profile = stalled_profile
    await case("d_pre_call_hook", stream=True, mode="ok", entered=stall_entered, restore=unstall)

    await case("e_zero_chunk_stream", stream=True, mode="sse-zero")

    sink.stall_next = True
    await case("f_audit_emit_in_flight", stream=True, mode="ok", token=None, entered=sink.entered)

    metrics_status, metrics_body = await _metrics(port)
    results["metrics"] = {"status": metrics_status, "text": metrics_body}
    results["cancel_grace_s"] = limiter.cancel_grace_s
    results["max_inflight"] = limiter.max_inflight
    results["loop"] = type(asyncio.get_running_loop()).__module__

    server.should_exit = True
    await serving
    stub_server.close()
    print(SENTINEL + json.dumps(results), flush=True)


async def _metrics(port: int) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /metrics HTTP/1.1\r\nHost: gateway\r\nConnection: close\r\n\r\n")
    await writer.drain()
    status = await _status(reader)
    rest = await asyncio.wait_for(reader.read(), BOUND_S)
    writer.close()
    return status, rest.decode("latin-1")


if __name__ == "__main__":
    if sys.argv[1:] == ["uvloop"]:
        import uvloop

        asyncio.run(main(), loop_factory=uvloop.new_event_loop)
    else:
        asyncio.run(main())
