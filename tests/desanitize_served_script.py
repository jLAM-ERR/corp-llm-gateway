"""Child process for ``tests/test_desanitize_served_stack.py``: the real entrypoint
(``asgi.app``: route gate, in-flight limiter, the ASGI desanitiser, litellm's app, the
armed lifespan and the guardrail ``bootstrap`` builds) served by uvicorn on a real
socket, in front of the dispatch harness's stub provider on another socket. Two capture
callbacks sit around ours in ``litellm.callbacks``; litellm runs at DEBUG under the
test-only allow key so its chunk log can be read. Prints one ``@@RESULT@@`` JSON line.

Run as ``python tests/desanitize_served_script.py``; importing ``corp_llm_gateway.asgi``
IS the boot, so this cannot share the test process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SENTINEL = "@@RESULT@@"
TOKEN = "served-dev-token"
BOUND_S = 30.0

CONFIG = """
model_list:
  - model_name: "corp-chat"
    litellm_params:
      model: "openai/gpt-4o-mini"
      api_base: "http://127.0.0.1:{port}/v1"
      api_key: "sk-stub-key"
  - model_name: "corp-claude"
    litellm_params:
      model: "anthropic/claude-sonnet-4-5"
      api_base: "http://127.0.0.1:{port}"
      api_key: "sk-stub-key"
  - model_name: "corp-responses"
    litellm_params:
      model: "openai/gpt-4o-mini"
      api_base: "http://127.0.0.1:{port}/v1"
      api_key: "sk-stub-key"
litellm_settings:
  callbacks:
    - "served_captures.before"
    - "corp_llm_gateway.bootstrap.guardrail"
    - "served_captures.after"
  drop_params: true
general_settings:
  cancel_on_disconnect: true
  supported_db_objects: ["models"]
"""

CAPTURES = """
from tests.litellm_hook._dispatch_fixtures import Capture

before = Capture("before")
after = Capture("after")
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _listening_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    sock.setblocking(False)
    return sock


STUB_PORT = _free_port()
_config_dir = Path(tempfile.mkdtemp(prefix="desanitize-served-"))
(_config_dir / "config.yaml").write_text(CONFIG.format(port=STUB_PORT))
(_config_dir / "served_captures.py").write_text(CAPTURES)
sys.path.insert(0, str(_config_dir))
os.environ["CORP_LLM_LITELLM_CONFIG"] = str(_config_dir / "config.yaml")

import uvicorn  # noqa: E402

import corp_llm_gateway.asgi as asgi  # noqa: E402
from corp_llm_gateway.audit import AuditLogger, ListSink  # noqa: E402
from corp_llm_gateway.route_gate import desanitize_middleware  # noqa: E402
from corp_llm_gateway.route_gate.terminal_audit import emit_to  # noqa: E402
from tests.litellm_hook import _dispatch_fixtures  # noqa: E402

# After the boot: this pulls in litellm's proxy app, which asgi imports at its step 3.
from tests.litellm_hook._dispatch_fixtures import (  # noqa: E402
    EMAIL,
    ORIGINAL_MARK,
    PLACEHOLDER_MARK,
    StubUpstream,
    _anthropic_sse,
)

ROUTES = {
    "chat": ("/v1/chat/completions", "corp-chat"),
    "messages": ("/v1/messages", "corp-claude"),
    "responses": ("/v1/responses", "corp-responses"),
}
# litellm's DEBUG chunk log (common_request_processing.py): runs on the stream litellm
# sends, i.e. before the desanitiser.
CHUNK_LOG_FILE = "common_request_processing.py"


class _DebugRecords(logging.Handler):
    """Every litellm DEBUG line, as (file, line, message)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[tuple[str, int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        self.records.append((record.pathname.rsplit("/", 1)[-1], record.lineno, message))

    def holding(self, needle: str) -> list[tuple[str, int]]:
        return sorted({(f, n) for f, n, m in self.records if needle in m})


def _body(route: str, *, stream: bool, extra: dict[str, Any] | None = None) -> bytes:
    _, model = ROUTES[route]
    text = f"write to {EMAIL}"
    body: dict[str, Any] = {"model": model, "stream": stream}
    if route == "responses":
        body["input"] = text
    else:
        body["messages"] = [{"role": "user", "content": text}]
    if route == "messages":
        body["max_tokens"] = 64
    return json.dumps(body | (extra or {})).encode()


async def _post(port: int, path: str, body: bytes) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = (
        f"POST {path} HTTP/1.1\r\nHost: gateway\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nX-Corp-Auth: {TOKEN}\r\nConnection: close\r\n\r\n"
    )
    writer.write(head.encode() + body)
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), BOUND_S)
    writer.close()
    head_part, _, rest = raw.partition(b"\r\n\r\n")
    lines = head_part.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    if headers.get("transfer-encoding") == "chunked":
        rest = _dechunk(rest)
    return status, headers, rest


def _dechunk(data: bytes) -> bytes:
    out = b""
    while data:
        size_line, _, data = data.partition(b"\r\n")
        size = int(size_line.split(b";")[0], 16)
        if size == 0:
            break
        out += data[:size]
        data = data[size + 2 :]
    return out


async def _settle(guardrail: Any) -> None:
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    for _ in range(4):
        await asyncio.sleep(0.05)
        await GLOBAL_LOGGING_WORKER.flush()
    await asyncio.wait_for(asyncio.shield(asgi.terminal.drain()), BOUND_S)


async def main() -> None:
    stub = StubUpstream(port=STUB_PORT)
    debug = _DebugRecords()
    import litellm
    from litellm._logging import verbose_logger, verbose_proxy_logger, verbose_router_logger

    for lg in (verbose_logger, verbose_proxy_logger, verbose_router_logger):
        lg.addHandler(debug)

    gw_sock = _listening_socket()
    port = gw_sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(asgi.app, lifespan="on", log_level="warning", access_log=False)
    )
    serving = asyncio.create_task(server.serve(sockets=[gw_sock]))
    start = time.monotonic()
    while not server.started and not serving.done() and time.monotonic() - start < BOUND_S:
        await asyncio.sleep(0.01)
    if serving.done():
        serving.result()

    guardrail = next(cb for cb in litellm.callbacks if type(cb).__name__ == "CorpLlmGuardrail")
    # The instances litellm registered (it may load the module under its own name).
    registered = [cb for cb in litellm.callbacks if type(cb).__name__ == "Capture"]
    sink = ListSink()
    guardrail._audit = AuditLogger(sink, gateway_version="served")
    asgi.terminal._emit = emit_to(AuditLogger(sink, gateway_version="served"))
    asgi.gate._audit = AuditLogger(sink, gateway_version="served")
    captures = tuple(sorted(registered, key=lambda cb: cb.name != "before"))
    order = [
        getattr(cb, "name", type(cb).__name__)
        for cb in litellm.callbacks
        if type(cb).__name__ in ("Capture", "CorpLlmGuardrail")
    ]

    results: dict[str, Any] = {"armed": asgi.gate.armed, "callback_order": order, "flows": {}}

    async def flow(name: str, route: str, *, stream: bool, extra: dict[str, Any] | None = None):
        for capture in captures:
            capture.seen.clear()
        debug.records.clear()
        base_records = len(sink.records)
        base_bodies = len(stub.bodies)
        path, _ = ROUTES[route]
        status, headers, body = await _post(port, path, _body(route, stream=stream, extra=extra))
        await _settle(guardrail)
        text = body.decode("utf-8", "replace")
        records = sink.records[base_records:]
        results["flows"][name] = {
            "status": status,
            "call_id": headers.get("x-litellm-call-id"),
            "client_original": ORIGINAL_MARK in text,
            "client_placeholder": PLACEHOLDER_MARK in text,
            "provider_bodies": stub.bodies[base_bodies:],
            "captures_holding_original": {
                capture.name: sorted(capture.seen.holding(ORIGINAL_MARK)) for capture in captures
            },
            "captures_holding_placeholder": {
                capture.name: sorted(capture.seen.holding(PLACEHOLDER_MARK)) for capture in captures
            },
            "captures_saw": {
                capture.name: sorted(k for k, v in capture.seen.hooks().items() if v)
                for capture in captures
            },
            "debug_original_sites": debug.holding(ORIGINAL_MARK),
            "debug_chunk_sites_with_placeholder": [
                site for site in debug.holding(PLACEHOLDER_MARK) if site[0] == CHUNK_LOG_FILE
            ],
            "records": records,
            "mappings_left": len(asgi.response_mappings),
            "req_state": len(guardrail._req_state),
        }

    for route in ROUTES:
        for stream in (False, True):
            await flow(f"{route}-{'sse' if stream else 'unary'}", route, stream=stream)

    # The OpenAI SDK's own chat stream accumulator, over the wire.
    results["sdk_chat_stream"] = await _sdk_chat_stream(port, captures, sink)
    await _settle(guardrail)

    # A top-level `policies` body key: refused at the gate, nothing egresses.
    base_bodies = len(stub.bodies)
    base_records = len(sink.records)
    status, _, body = await _post(
        port, "/v1/chat/completions", _body("chat", stream=False, extra={"policies": ["p1"]})
    )
    await _settle(guardrail)
    results["body_policies"] = {
        "status": status,
        "code": json.loads(body)["error"]["code"],
        "reason": json.loads(body)["error"]["reason"],
        "provider_bodies": stub.bodies[base_bodies:],
        "records": sink.records[base_records:],
    }

    results["cancelled_mid_stream"] = await _cancel_mid_stream(port, stub, guardrail, sink)
    results["restoration_failure"] = await _restoration_failure(port, guardrail, sink)
    results["metrics"] = await _metrics(port)

    server.should_exit = True
    await serving
    stub.close()
    print(SENTINEL + json.dumps(results, default=str), flush=True)


async def _sdk_chat_stream(port: int, captures: Any, sink: ListSink) -> dict[str, Any]:
    from openai import AsyncOpenAI

    for capture in captures:
        capture.seen.clear()
    base_records = len(sink.records)
    client = AsyncOpenAI(
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key="sk-client",
        default_headers={"X-Corp-Auth": TOKEN},
        max_retries=0,
    )
    try:
        async with client.chat.completions.stream(
            model="corp-chat", messages=[{"role": "user", "content": f"write to {EMAIL}"}]
        ) as stream:
            done: list[str] = []
            async for event in stream:
                if event.type == "content.done":
                    done.append(event.content)
            final = await stream.get_final_completion()
    finally:
        await client.close()
    await asyncio.sleep(0.2)
    content = final.choices[0].message.content or ""
    return {
        "content": content,
        "content_done": done,
        "client_original": ORIGINAL_MARK in content,
        "client_placeholder": PLACEHOLDER_MARK in content,
        "captures_holding_original": {
            capture.name: sorted(capture.seen.holding(ORIGINAL_MARK)) for capture in captures
        },
        "records": sink.records[base_records:],
    }


def _long_anthropic(path: str, text: str, **_: Any) -> list[str]:
    events = _anthropic_sse(text)
    filler = [
        "event: content_block_delta\ndata: "
        + json.dumps(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": f" w{i}"},
            }
        )
        for i in range(40)
    ]
    return [*events[:4], *filler, *events[4:]]


async def _cancel_mid_stream(port: int, stub: Any, guardrail: Any, sink: ListSink) -> dict:
    """The client reads the start of a /v1/messages stream, then hangs up."""
    real_stream = _dispatch_fixtures._stream
    _dispatch_fixtures._stream = _long_anthropic
    stub.event_delay = 0.05
    base_records = len(sink.records)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        body = _body("messages", stream=True)
        head = (
            "POST /v1/messages HTTP/1.1\r\nHost: gateway\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nX-Corp-Auth: {TOKEN}\r\n\r\n"
        )
        writer.write(head.encode() + body)
        await writer.drain()
        first = await asyncio.wait_for(reader.readuntil(b"content_block_delta"), BOUND_S)
        writer.close()
        start = time.monotonic()
        while asgi.limiter.inflight > 0 and time.monotonic() - start < BOUND_S:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)
        await _settle(guardrail)
    finally:
        _dispatch_fixtures._stream = real_stream
        stub.event_delay = 0.0
    records = sink.records[base_records:]
    return {
        "read_first": b"200" in first,
        "records": records,
        "mappings_left": len(asgi.response_mappings),
        "req_state": len(guardrail._req_state),
        "inflight": asgi.limiter.inflight,
    }


async def _restoration_failure(port: int, guardrail: Any, sink: ListSink) -> dict:
    """The restorer fails on the event carrying the placeholder, mid-stream."""
    real_feed = desanitize_middleware.SseStreamDesanitizer.feed

    def feed(self: Any, chunk: Any) -> Any:
        if PLACEHOLDER_MARK in str(chunk):
            raise ValueError(f"restore failed near {EMAIL}")
        return real_feed(self, chunk)

    desanitize_middleware.SseStreamDesanitizer.feed = feed  # type: ignore[method-assign]
    base_records = len(sink.records)
    try:
        status, _, body = await _post(port, "/v1/messages", _body("messages", stream=True))
        await _settle(guardrail)
    finally:
        desanitize_middleware.SseStreamDesanitizer.feed = real_feed  # type: ignore[method-assign]
    return {
        "status": status,
        "client_original": ORIGINAL_MARK in body.decode("utf-8", "replace"),
        "records": sink.records[base_records:],
    }


async def _metrics(port: int) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /metrics HTTP/1.1\r\nHost: gateway\r\nConnection: close\r\n\r\n")
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), BOUND_S)
    writer.close()
    return raw.decode("latin-1")


if __name__ == "__main__":
    asyncio.run(main())
