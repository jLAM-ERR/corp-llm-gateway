"""Stub upstream for the nginx runtime tests: 200 on :8000, nothing on :8001
(connection refused), and :8002 accepts and never answers (a read timeout).
Prints one ``stub-hit`` line per request that reaches it."""

import http.server
import socket
import threading


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


def hold(listener: socket.socket) -> None:
    held = []
    while True:
        held.append(listener.accept()[0])
        print("stub-hit", flush=True)


threading.Thread(target=hold, args=(socket.create_server(("", 8002)),), daemon=True).start()
server = http.server.ThreadingHTTPServer(("", 8000), Ok)
print("stub-ready", flush=True)
server.serve_forever()
