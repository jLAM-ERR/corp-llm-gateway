"""The gateway owns litellm_call_id: a client's ``x-litellm-call-id`` header never
reaches litellm, so two client requests can never share the id the guardrail keys
its per-request state and audit record on."""

from __future__ import annotations

from typing import Any

import pytest

from corp_llm_gateway.route_gate.middleware import CALL_ID_HEADER
from tests.route_gate.test_inflight import _Client, _read_body, _respond, _scope, _stack
from tests.route_gate.test_middleware import _drive, _gate, _http_scope, _status, _Stub

SPELLINGS = [b"x-litellm-call-id", b"X-LiteLLM-Call-Id", b"X-LITELLM-CALL-ID"]


def _names(scope: dict[str, Any]) -> list[bytes]:
    return [bytes(name).lower() for name, _ in scope["headers"]]


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize(
    ("method", "path"),
    [("GET", "/health/liveness"), ("POST", "/v1/messages")],
    ids=["passthrough", "rewritten-no-limiter"],
)
async def test_the_call_id_header_never_reaches_the_downstream(
    spelling: bytes, method: str, path: str
) -> None:
    stub = _Stub()
    headers = [
        (b"content-type", b"application/json"),
        (spelling, b"client-chosen"),
        (b"x-other", b"kept"),
        (spelling, b"second"),
    ]
    scope = _http_scope(method, path, headers=headers)

    sent = await _drive(_gate(stub, armed=True), scope, body=b"{}")

    assert _status(sent) == 200
    (seen,) = stub.scopes
    assert CALL_ID_HEADER not in _names(seen)
    assert seen["headers"] == [(b"content-type", b"application/json"), (b"x-other", b"kept")]
    # The server's own scope is left as it was.
    assert scope["headers"] == headers


@pytest.mark.parametrize("spelling", SPELLINGS)
async def test_the_call_id_header_never_reaches_an_admitted_request(spelling: bytes) -> None:
    seen: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(dict(scope))
        await _read_body(receive)
        await _respond(send)

    gate, limiter, _, _ = _stack(app)
    scope = _scope()
    scope["headers"].append((spelling, b"client-chosen"))
    client = _Client()

    await gate(scope, client.receive, client.send)

    assert client.status == 200
    assert CALL_ID_HEADER not in _names(seen[0])
    assert limiter.inflight == 0


async def test_a_request_without_the_header_is_forwarded_with_its_own_scope() -> None:
    forwarded: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        forwarded.append(scope)
        await _read_body(receive)
        await _respond(send)

    gate = _gate(app, armed=True)
    scope = _http_scope("GET", "/health/liveness")

    await _drive(gate, scope)

    assert forwarded == [scope]
    assert forwarded[0] is scope


async def test_a_refused_route_is_refused_whatever_call_id_it_carries() -> None:
    stub = _Stub()
    scope = _http_scope("GET", "/key/list", headers=[(CALL_ID_HEADER, b"client-chosen")])

    sent = await _drive(_gate(stub, armed=True), scope)

    assert _status(sent) == 403
    assert not stub.called
