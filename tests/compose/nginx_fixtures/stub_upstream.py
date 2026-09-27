"""Stub upstream for the nginx runtime tests: 200 on :8000, nothing on :8001
(connection refused), and :8002 accepts and never answers (a read timeout).
:4000 stands in for the gateway (the harness aliases this container as
``litellm``): it answers 200 to any method and records what arrived; a
``big=1`` query parameter makes the answer 8 MiB instead of ``ok``.
Prints one ``stub-hit`` line per request that reaches it; a :4000 line carries
``stub-hit <json>`` with the method, the request target exactly as sent, the
headers, and the body's length and sha256."""

import hashlib
import http.server
import json
import socket
import threading
import urllib.parse

BIG_RESPONSE = b"x" * (8 * 1024 * 1024)


class Ok(http.server.BaseHTTPRequestHandler):
    def _answer(self) -> None:
        print("stub-hit", flush=True)
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
            "method": self.command,
            "target": self.path,
            "headers": [[name.lower(), value] for name, value in self.headers.items()],
            "body_length": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }
        print("stub-hit " + json.dumps(record), flush=True)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        answer = BIG_RESPONSE if query.get("big") == ["1"] else b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        if self.command != "HEAD":
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
        print("stub-hit", flush=True)


threading.Thread(target=hold, args=(socket.create_server(("", 8002)),), daemon=True).start()
recording = http.server.ThreadingHTTPServer(("", 4000), Recording)
threading.Thread(target=recording.serve_forever, daemon=True).start()
server = http.server.ThreadingHTTPServer(("", 8000), Ok)
print("stub-ready", flush=True)
server.serve_forever()
