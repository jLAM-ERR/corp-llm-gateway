"""corp-llm-gateway proxy — header-injecting localhost proxy for harnesses
that can't send X-Corp-Auth themselves.

Listens on a configurable localhost port; forwards every request to the
configured gateway URL with `X-Corp-Auth` injected from the token file.
The token file is re-read on every request so token rotation takes
effect without restarting the proxy.

Streams SSE responses chunk-by-chunk so harnesses see the same byte
stream they would get directly from the gateway.

Usage:
    corp-llm-gateway proxy --listen 127.0.0.1:9999 --upstream https://gateway.corp.lan
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import ProxyHandler, Request, build_opener

# Don't honor env-based proxy lookup. The whole point of this CLI is to BE
# the proxy; silently routing through a system HTTP_PROXY would defeat that
# and (worse) leak the corp token to whatever the env names.
_OPENER = build_opener(ProxyHandler({}))

logger = logging.getLogger("corp-llm-gateway.proxy")

DEFAULT_LISTEN = "127.0.0.1:9999"
DEFAULT_UPSTREAM = "https://gateway.corp.lan"
DEFAULT_TOKEN_FILE = "~/.corp-llm-gateway/token"

# Hop-by-hop headers — RFC 2616 §13.5.1. We don't forward these.
_HOP_HEADERS = frozenset(
    [
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    ]
)


class _ProxyHandler(BaseHTTPRequestHandler):
    upstream_base: str = DEFAULT_UPSTREAM
    token_file: Path = Path(DEFAULT_TOKEN_FILE).expanduser()

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - " + format, self.client_address[0], *args)

    def do_GET(self) -> None: self._handle()
    def do_POST(self) -> None: self._handle()
    def do_PUT(self) -> None: self._handle()
    def do_DELETE(self) -> None: self._handle()
    def do_PATCH(self) -> None: self._handle()
    def do_OPTIONS(self) -> None: self._handle()

    def _handle(self) -> None:
        try:
            corp_token = _read_token(self.token_file)
        except FileNotFoundError:
            self._send_error(401, "corp token file not found; run install.sh")
            return

        body = self._read_body()
        upstream_url = urljoin(self.upstream_base.rstrip("/") + "/", self.path.lstrip("/"))
        headers = self._forward_headers()
        headers["X-Corp-Auth"] = corp_token

        req = Request(upstream_url, data=body, method=self.command, headers=headers)
        try:
            with _OPENER.open(req, timeout=300) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in _HOP_HEADERS:
                        continue
                    self.send_header(k, v)
                self.end_headers()
                for chunk in _read_chunks(resp):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except OSError as exc:
            self._send_error(502, f"upstream error: {type(exc).__name__}")

    def _read_body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return self.rfile.read(length) if length > 0 else None

    def _forward_headers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in self.headers.keys():
            if name.lower() in _HOP_HEADERS:
                continue
            value = self.headers[name]
            if value is not None:
                out[name] = value
        return out

    def _send_error(self, code: int, message: str) -> None:
        body = f'{{"error":{{"message":"{message}"}}}}'.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _read_token(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _read_chunks(resp: Any, chunk_size: int = 4096) -> Iterator[bytes]:
    while True:
        chunk = resp.read(chunk_size)
        if not chunk:
            return
        yield chunk


def serve(
    listen: str,
    upstream: str,
    token_file: Path,
) -> ThreadingHTTPServer:
    host, _, port_s = listen.partition(":")
    port = int(port_s)

    class Handler(_ProxyHandler):
        pass

    Handler.upstream_base = upstream
    Handler.token_file = token_file

    server = ThreadingHTTPServer((host, port), Handler)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="corp-llm-gateway proxy")
    parser.add_argument("--listen", default=DEFAULT_LISTEN)
    parser.add_argument(
        "--upstream",
        default=os.environ.get("CORP_GATEWAY_URL", DEFAULT_UPSTREAM),
    )
    parser.add_argument(
        "--token-file",
        default=os.environ.get("CORP_GATEWAY_TOKEN_FILE", DEFAULT_TOKEN_FILE),
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level)
    server = serve(args.listen, args.upstream, Path(args.token_file).expanduser())

    print(
        f"corp-llm-gateway proxy listening on http://{args.listen} → {args.upstream}",
        file=sys.stderr,
    )

    stop = threading.Event()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        stop.set()
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
