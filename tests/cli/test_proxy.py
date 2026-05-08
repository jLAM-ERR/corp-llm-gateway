"""Smoke tests for the proxy CLI.

The full HTTP integration of the proxy is exercised in docker compose
(tests/e2e); here we just verify the wiring and CLI args. The proxy is
small enough that the contract is mostly its argparse + the
header-injection logic, both of which are testable without sockets.
"""
from pathlib import Path

import pytest

from corp_llm_gateway.cli.proxy import (
    DEFAULT_LISTEN,
    DEFAULT_TOKEN_FILE,
    DEFAULT_UPSTREAM,
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
