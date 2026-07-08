"""Smoke tests for the proxy CLI.

The full HTTP integration of the proxy is exercised in docker compose
(tests/e2e); here we just verify the wiring and CLI args. The proxy is
small enough that the contract is mostly its argparse + the
header-injection logic, both of which are testable without sockets.
"""

from __future__ import annotations

import io
from http.client import HTTPMessage
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

import pytest

from corp_llm_gateway.cli import proxy
from corp_llm_gateway.cli.proxy import (
    DEFAULT_LISTEN,
    DEFAULT_TOKEN_FILE,
    DEFAULT_UPSTREAM,
    _ProxyHandler,
    _read_token,
    serve,
)


def test_defaults_match_install_sh_layout() -> None:
    assert DEFAULT_LISTEN == "127.0.0.1:9999"
    assert DEFAULT_UPSTREAM == "https://gateway.corp.lan"
    assert DEFAULT_TOKEN_FILE == "~/.corp-llm-gateway/token"


def test_serve_returns_a_threading_server(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("ct_xyz")
    server = serve("127.0.0.1:0", "https://upstream.example", token)
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_address[1] > 0  # auto-assigned port
    finally:
        server.server_close()


def test_serve_subclass_carries_upstream_and_token(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("ct_xyz")
    server = serve("127.0.0.1:0", "https://up.example/", token)
    try:
        handler_cls = server.RequestHandlerClass  # type: ignore[attr-defined]
        assert handler_cls.upstream_base == "https://up.example/"
        assert handler_cls.token_file == token
    finally:
        server.server_close()


def test_serve_isolates_handler_per_call(tmp_path: Path) -> None:
    """Two servers must not share Handler class state."""
    token_a = tmp_path / "a"
    token_a.write_text("ct_a")
    token_b = tmp_path / "b"
    token_b.write_text("ct_b")
    s_a = serve("127.0.0.1:0", "https://a.example", token_a)
    s_b = serve("127.0.0.1:0", "https://b.example", token_b)
    try:
        cls_a = s_a.RequestHandlerClass  # type: ignore[attr-defined]
        cls_b = s_b.RequestHandlerClass  # type: ignore[attr-defined]
        assert cls_a is not cls_b
        assert cls_a.upstream_base == "https://a.example"
        assert cls_b.upstream_base == "https://b.example"
    finally:
        s_a.server_close()
        s_b.server_close()


def test_read_token_strips_trailing_newline(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("ct_xyz\n\n  \n")
    assert _read_token(token) == "ct_xyz"


def test_read_token_raises_filenotfound(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _read_token(tmp_path / "missing")


# --- F7: dev-proxy must not forward secrets to a client-chosen host ---

CORP_TOKEN = "ct-corp-secret-token"
BYOK = "Bearer byok-provider-secret-key"


class _FakeResponse:
    status = 200
    headers: ClassVar[dict[str, str]] = {}

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return b""


class _RecordingOpener:
    """Stands in for the module opener; records outbound Requests."""

    def __init__(self) -> None:
        self.requests: list = []

    def open(self, req: object, timeout: int | None = None) -> _FakeResponse:
        self.requests.append(req)
        return _FakeResponse()


def _make_handler(
    path: str,
    token_file: Path,
    upstream: str = "https://gateway.corp.lan",
) -> _ProxyHandler:
    h = _ProxyHandler.__new__(_ProxyHandler)
    h.path = path
    h.command = "POST"
    h.requestline = f"POST {path} HTTP/1.1"
    h.request_version = "HTTP/1.1"
    msg = HTTPMessage()
    msg["Authorization"] = BYOK
    msg["Content-Length"] = "2"
    h.headers = msg
    h.rfile = io.BytesIO(b"{}")
    h.wfile = io.BytesIO()
    h.client_address = ("127.0.0.1", 5555)
    h.upstream_base = upstream
    h.token_file = token_file
    return h


def _status_code(handler: _ProxyHandler) -> int:
    first_line = handler.wfile.getvalue().split(b"\r\n", 1)[0]  # type: ignore[attr-defined]
    return int(first_line.split(b" ")[1])


def _drive(
    path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[_ProxyHandler, _RecordingOpener]:
    token = tmp_path / "token"
    token.write_text(CORP_TOKEN)
    opener = _RecordingOpener()
    monkeypatch.setattr(proxy, "_OPENER", opener)
    handler = _make_handler(path, token)
    handler._handle()
    return handler, opener


def test_absolute_uri_target_rejected_no_secret_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, opener = _drive("http://evil.com/v1/messages", tmp_path, monkeypatch)
    assert opener.requests == []  # no outbound request => no token/BYOK leak
    assert _status_code(handler) == 403


def test_protocol_relative_target_rejected_no_secret_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, opener = _drive("//evil.com/v1/messages", tmp_path, monkeypatch)
    assert opener.requests == []
    assert _status_code(handler) == 403


def test_normal_relative_path_proxies_with_corp_auth_and_byok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, opener = _drive("/v1/messages", tmp_path, monkeypatch)
    assert len(opener.requests) == 1
    req = opener.requests[0]
    assert urlsplit(req.full_url).hostname == "gateway.corp.lan"
    sent = {k.lower(): v for k, v in req.headers.items()}
    assert sent["x-corp-auth"] == CORP_TOKEN
    assert sent["authorization"] == BYOK
