"""Stub upstream for the nginx runtime tests: 200 on :8000, nothing on :8001
(connection refused), and :8002 accepts and never answers (a read timeout).
:4000 stands in for the gateway (the harness aliases this container as
``litellm``): it answers 200 to any method and records what arrived; a
``big=1`` query parameter makes the answer 8 MiB instead of ``ok``, and
``delay=<seconds>`` holds the answer that long after the request is recorded,
and ``status=429`` answers the gateway's own capacity refusal instead;
``sse=<n>`` streams n ``text/event-stream`` events 200 ms apart, printing
``stub-sse-sent <i>`` as each one leaves; ``POST
/internal/issue-token`` on :4000 answers the gateway's issuance shape
(``ISSUED_TOKEN``, ``ISSUED_EXPIRES_AT``), the one install.sh parses.
:3000 stands in for Langfuse (aliased ``langfuse-web``) and answers the same
way; a request there with ``Upgrade: websocket`` is answered 101, and the
stand-in then echoes one line back as ``echo:<line>`` through the tunnel.
Prints one ``stub-hit`` line per request that reaches it; a :4000 or :3000 line
carries ``stub-hit <json>`` with the port, the method, the request target
exactly as sent, the headers, and the body's length and sha256."""

import hashlib
import http.server
import json
import socket
import sys
import threading
import time
import urllib.parse

BIG_RESPONSE = b"x" * (8 * 1024 * 1024)
SSE_GAP_SECONDS = 0.2
ISSUED_TOKEN = "ct_" + "S" * 43
ISSUED_EXPIRES_AT = "2026-10-27T00:00:00Z"
_OUTPUT = threading.Lock()


def hit(line: str = "stub-hit") -> None:
    # One whole line per request: the handler threads run concurrently, and
    # print() writes the text and its newline separately.
    with _OUTPUT:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


class Ok(http.server.BaseHTTPRequestHandler):
    def _answer(self) -> None:
        hit()
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self) -> None:
        self._answer()

    def do_POST(self) -> None:
        self._answer()

    def log_message(self, format: str, *args: object) -> None:
        pass


class Recording(http.server.BaseHTTPRequestHandler):
    def _body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return b"".join(chunks)
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _record(self) -> None:
        body = self._body()
        record = {
            "port": self.server.server_address[1],
            "method": self.command,
            "target": self.path,
            "headers": [[name.lower(), value] for name, value in self.headers.items()],
            "body_length": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        hit("stub-hit " + json.dumps(record))
        if (self.headers.get("Upgrade") or "").lower() == "websocket":
            self._tunnel()
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if "delay" in query:
            time.sleep(float(query["delay"][0]))
        if query.get("status") == ["429"]:
            self._capacity_refusal()
            return
        if "sse" in query:
            self._stream(int(query["sse"][0]))
            return
        port = self.server.server_address[1]
        if (port, self.command, self.path) == (4000, "POST", "/internal/issue-token"):
            self._issued()
            return
        answer = BIG_RESPONSE if query.get("big") == ["1"] else b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(answer)

    def _tunnel(self) -> None:
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.end_headers()
        self.wfile.write(b"echo:" + self.rfile.readline())
        self.close_connection = True

    def _stream(self, events: int) -> None:
        # No Content-Length: the stream ends when the connection closes, so
        # nothing here holds an event back; only a buffering proxy can.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for index in range(events):
            if index:
                time.sleep(SSE_GAP_SECONDS)
            self.wfile.write(f"data: event-{index}\n\n".encode())
            self.wfile.flush()
            hit(f"stub-sse-sent {index}")
        self.close_connection = True

    def _issued(self) -> None:
        answer = json.dumps({"corp_token": ISSUED_TOKEN, "expires_at": ISSUED_EXPIRES_AT})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer.encode())

    def _capacity_refusal(self) -> None:
        answer = b'{"error":"E_CAPACITY"}'
        self.send_response(429)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", "7")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def __getattr__(self, name: str):
        # BaseHTTPRequestHandler dispatches to do_<METHOD>: record every method.
        if name.startswith("do_"):
            return self._record
        raise AttributeError(name)

    def log_message(self, format: str, *args: object) -> None:
        pass


def hold(listener: socket.socket) -> None:
    held = []
    while True:
        held.append(listener.accept()[0])
        hit()


threading.Thread(target=hold, args=(socket.create_server(("", 8002)),), daemon=True).start()
for port in (4000, 3000):
    recording = http.server.ThreadingHTTPServer(("", port), Recording)
    threading.Thread(target=recording.serve_forever, daemon=True).start()
server = http.server.ThreadingHTTPServer(("", 8000), Ok)
print("stub-ready", flush=True)
server.serve_forever()
