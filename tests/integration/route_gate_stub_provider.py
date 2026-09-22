"""Stub upstream provider for the route-gate container tests.

Runs INSIDE a container on the egress-blocked network, as the only thing the
gateway can reach. It answers the Anthropic and OpenAI shapes litellm posts and
writes one ``@@CAPTURE@@<json>`` line per request to stdout, which the test reads
with ``docker logs``. A route the gate was supposed to refuse shows up here as a
capture; nothing else can, because the network has no route off it.

Not a pytest module: it is mounted into the container and run as a script.
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CAPTURE = "@@CAPTURE@@"
PORT = 8000

# Between SSE events, so a client that buffers the whole stream is visibly
# different from one that forwards each event as it arrives.
SSE_DELAY_SECONDS = 0.5

MAX_DRAIN_BYTES = 1 << 20

MODEL = "claude-3-5-sonnet-20241022"

MESSAGE: dict[str, Any] = {
    "id": "msg_route_gate_1",
    "type": "message",
    "role": "assistant",
    "model": MODEL,
    "content": [{"type": "text", "text": "stub reply"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 7, "output_tokens": 2},
}

CHAT: dict[str, Any] = {
    "id": "chatcmpl-route-gate-1",
    "object": "chat.completion",
    "created": 1758500000,
    "model": "gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "stub reply"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
}

RESPONSE: dict[str, Any] = {
    "id": "resp_route_gate_1",
    "object": "response",
    "created_at": 1758500000,
    "status": "completed",
    "model": "gpt-4o-mini",
    "output": [
        {
            "id": "msg_route_gate_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "stub reply", "annotations": []}],
        }
    ],
    "usage": {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9},
}

_ANTHROPIC_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_route_gate_1",'
    f'"type":"message","role":"assistant","model":"{MODEL}","content":[],"stop_reason":null,'
    '"stop_sequence":null,"usage":{"input_tokens":7,"output_tokens":1}}}\n\n',
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}\n\n',
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"stub reply"}}\n\n',
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn",'
    '"stop_sequence":null},"usage":{"output_tokens":2}}\n\n',
    'event: message_stop\ndata: {"type":"message_stop"}\n\n',
)

_CHAT_SSE = (
    'data: {"id":"chatcmpl-route-gate-1","object":"chat.completion.chunk","created":1758500000,'
    '"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"role":"assistant","content":""},'
    '"finish_reason":null}]}\n\n',
    'data: {"id":"chatcmpl-route-gate-1","object":"chat.completion.chunk","created":1758500000,'
    '"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"content":"stub reply"},'
    '"finish_reason":null}]}\n\n',
    'data: {"id":"chatcmpl-route-gate-1","object":"chat.completion.chunk","created":1758500000,'
    '"model":"gpt-4o-mini","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
    "data: [DONE]\n\n",
)

_RESPONSES_SSE = (
    'event: response.created\ndata: {"type":"response.created","response":'
    '{"id":"resp_route_gate_1","object":"response","created_at":1758500000,'
    '"status":"in_progress","model":"gpt-4o-mini","output":[]}}\n\n',
    'event: response.output_text.delta\ndata: {"type":"response.output_text.delta",'
    '"item_id":"msg_route_gate_1","output_index":0,"content_index":0,"delta":"stub reply"}\n\n',
    'event: response.completed\ndata: {"type":"response.completed","response":'
    + json.dumps(RESPONSE)
    + "}\n\n",
)


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _body_for(path: str) -> dict[str, Any]:
    if "/messages" in path:
        return MESSAGE
    if "/responses" in path:
        return RESPONSE
    return CHAT


def _stream_for(path: str) -> tuple[str, ...]:
    if "/messages" in path:
        return _ANTHROPIC_SSE
    if "/responses" in path:
        return _RESPONSES_SSE
    return _CHAT_SSE


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # do_GET / do_POST: BaseHTTPRequestHandler's own dispatch spelling.
    def do_GET(self) -> None:
        # Readiness probe from the test harness; deliberately not captured.
        self._send(204, b"", "text/plain")

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        body = _parse(raw)
        if body is None and raw:
            # litellm's Anthropic pass-through forwards the CLIENT's
            # content-length alongside its own, rewritten body, so the declared
            # length can be short. Reading only that many bytes would hide the
            # tail of the request — exactly where a canary could sit.
            raw += self._drain(raw)
            body = _parse(raw)
            self.close_connection = True
        if body is None:
            body = raw.decode("utf-8", "replace")
        print(
            CAPTURE
            + json.dumps(
                {
                    "path": self.path,
                    "headers": {name.lower(): value for name, value in self.headers.items()},
                    "body": body,
                }
            ),
            flush=True,
        )
        if isinstance(body, dict) and body.get("stream"):
            self._send_stream(_stream_for(self.path))
            return
        self._send(200, json.dumps(_body_for(self.path)).encode(), "application/json")

    def _drain(self, prefix: bytes, seconds: float = 1.0) -> bytes:
        """Read past the declared length until the body parses."""
        rest = b""
        # `rfile`, not `connection`: BaseHTTPRequestHandler wraps the socket in a
        # BufferedReader, so the bytes past content-length are usually already
        # buffered and a raw recv() would just block until it timed out.
        self.connection.settimeout(seconds)
        try:
            while len(rest) < MAX_DRAIN_BYTES:
                chunk = self.rfile.read1(65536)
                if not chunk:
                    break
                rest += chunk
                if _parse(prefix + rest) is not None:
                    break
        except OSError:
            pass
        finally:
            self.connection.settimeout(None)
        return rest

    def _send(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _send_stream(self, events: tuple[str, ...]) -> None:
        # Real chunked transfer, one chunk per event with a gap between them:
        # anything that buffers on the way back shows up as every event landing
        # at once at the end.
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        for index, event in enumerate(events):
            if index:
                time.sleep(SSE_DELAY_SECONDS)
            payload = event.encode()
            self.wfile.write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _Handler)
    server.daemon_threads = True
    print(f"stub provider listening on {PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
