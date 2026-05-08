"""E2E test for the corp-llm-gateway proxy CLI.

Skipped unless RUN_PROXY_E2E is set (it's the heaviest test — spins up
a real upstream HTTP server, a real proxy, and exercises both with
urllib). Runs cleanly in docker compose where the env is hygienic.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from corp_llm_gateway.cli.proxy import serve

skip_if_no_e2e = pytest.mark.skipif(
    not os.environ.get("RUN_PROXY_E2E"),
    reason="RUN_PROXY_E2E must be set",
)


@pytest.fixture
def upstream() -> Iterator[tuple[str, list]]:
    captured: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a, **k):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length else b""
            captured.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body.decode("utf-8", errors="replace"),
                }
            )
            payload = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    s = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = s.server_address[1]
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", captured
    finally:
        s.shutdown()
        s.server_close()


@pytest.fixture
def proxy(upstream: tuple[str, list], tmp_path: Path) -> Iterator[str]:
    upstream_url, _ = upstream
    token = tmp_path / "token"
    token.write_text("ct_abc123\n")
    s = serve("127.0.0.1:0", upstream_url, token)
    port = s.server_address[1]
    t = threading.Thread(target=s.serve_forever, args=(0.05,), daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        s.shutdown()
        s.server_close()


def _post(url: str, body: dict, headers: dict[str, str] | None = None) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


@skip_if_no_e2e
def test_proxy_injects_x_corp_auth(proxy, upstream) -> None:
    _, captured = upstream
    _post(f"{proxy}/v1/chat/completions", {"model": "x", "messages": []})
    assert len(captured) == 1
    headers = {k.lower(): v for k, v in captured[0]["headers"].items()}
    assert headers["x-corp-auth"] == "ct_abc123"


@skip_if_no_e2e
def test_proxy_forwards_authorization_untouched(proxy, upstream) -> None:
    _, captured = upstream
    _post(
        f"{proxy}/v1/chat/completions",
        {"x": 1},
        headers={"Authorization": "Bearer byok-developer-key"},
    )
    headers = {k.lower(): v for k, v in captured[0]["headers"].items()}
    assert headers["authorization"] == "Bearer byok-developer-key"


@skip_if_no_e2e
def test_proxy_forwards_path_unchanged(proxy, upstream) -> None:
    _, captured = upstream
    _post(f"{proxy}/v1/messages", {"x": 1})
    assert captured[0]["path"] == "/v1/messages"


@skip_if_no_e2e
def test_proxy_rereads_token_per_request(proxy, upstream, tmp_path) -> None:
    _, captured = upstream
    (tmp_path / "token").write_text("ct_rotated_456\n")
    time.sleep(0.05)
    _post(f"{proxy}/v1/chat/completions", {"x": 1})
    headers = {k.lower(): v for k, v in captured[-1]["headers"].items()}
    assert headers["x-corp-auth"] == "ct_rotated_456"


@skip_if_no_e2e
def test_proxy_401_when_token_missing(upstream, tmp_path) -> None:
    upstream_url, _ = upstream
    s = serve("127.0.0.1:0", upstream_url, tmp_path / "no-token")
    port = s.server_address[1]
    t = threading.Thread(target=s.serve_forever, args=(0.05,), daemon=True)
    t.start()
    try:
        url = f"http://127.0.0.1:{port}/v1/x"
        with pytest.raises(urllib.error.HTTPError) as ei:
            _post(url, {"x": 1})
        assert ei.value.code == 401
    finally:
        s.shutdown()
        s.server_close()
