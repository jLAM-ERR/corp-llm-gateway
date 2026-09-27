"""Operator extras at scale and at odd spellings, and the limiter's refusal codes
end to end: the body a client gets and the audit record agree, and no two
refusals share a code."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from corp_llm_gateway.audit import ListSink
from corp_llm_gateway.route_gate.classify import ROUTE_GATE_MALFORMED, classify
from corp_llm_gateway.route_gate.table import Verdict, parse_extras
from tests.route_gate.test_inflight import (
    _Client,
    _Holding,
    _scope,
    _stack,
    _stalled_client,
)
from tests.route_gate.test_middleware import _drive, _gate, _http_scope, _json, _status, _Stub

# ── extras ───────────────────────────────────────────────────────────────────


def test_a_thousand_extras_are_each_admitted_and_nothing_else_is() -> None:
    raw = ",".join(f"GET /internal/ops-{index}" for index in range(1000))
    extras = parse_extras(raw)

    assert len(extras) == 1000
    for index in (0, 499, 999):
        path = f"/internal/ops-{index}"
        assert classify("GET", path, path.encode(), extras=extras).verdict is Verdict.PASSTHROUGH
    assert classify("GET", "/internal/ops-1000", extras=extras).verdict is Verdict.REFUSE
    assert classify("POST", "/internal/ops-1", extras=extras).verdict is Verdict.REFUSE


def test_extras_that_differ_only_in_method_case_are_one_row() -> None:
    extras = parse_extras("get /internal/x\nGET /internal/x, Get /internal/x")

    assert list(extras) == [("GET", "/internal/x")]
    assert classify("get", "/internal/x", b"/internal/x", extras=extras).verdict is (
        Verdict.PASSTHROUGH
    )


async def test_an_extra_naming_a_rewritten_route_never_skips_the_rewrite() -> None:
    # Accepted at load, but the table speaks first: the route stays REWRITTEN,
    # so an unarmed gate still refuses it rather than passing it through.
    extras = parse_extras("POST /v1/messages")
    decision = classify("POST", "/v1/messages", b"/v1/messages", extras=extras)
    assert decision.verdict is Verdict.REWRITTEN

    stub = _Stub()
    sent = await _drive(_gate(stub, extras=extras), _http_scope("POST", "/v1/messages"))
    assert _status(sent) == 503
    assert not stub.called


async def test_a_non_ascii_extra_never_reaches_the_app_through_a_server_that_sends_raw_path() -> (
    None
):
    extras = parse_extras("GET /café")
    stub = _Stub()
    sent = await _drive(
        _gate(stub, extras=extras), _http_scope("GET", "/café", raw_path="/café".encode())
    )

    assert _status(sent) == 403
    assert _json(sent)["error"]["reason"] == ROUTE_GATE_MALFORMED
    assert not stub.called


@pytest.mark.parametrize("path", ["/key/list/", "/Key/List", "/key/list/x"])
async def test_a_spelling_next_to_a_refused_row_does_not_open_the_row(path: str) -> None:
    # Near-miss extras load (the table does not know them); the refused row
    # itself stays refused beside them.
    extras = parse_extras(f"GET {path}")
    stub = _Stub()
    sent = await _drive(_gate(stub, armed=True, extras=extras), _http_scope("GET", "/key/list"))

    assert _status(sent) == 403
    assert not stub.called


# ── the limiter's refusal codes ──────────────────────────────────────────────


async def _capacity(sink: ListSink) -> _Client:
    app = _Holding()
    gate, _, _, _ = _stack(app, max_inflight=1, sink=sink)
    held = _Client()
    task = asyncio.create_task(gate(_scope(), held.receive, held.send))
    await asyncio.wait_for(app.entered.acquire(), 2)
    refused = _Client()
    await gate(_scope(), refused.receive, refused.send)
    app.release.set()
    await task
    return refused


async def _body_timeout(sink: ListSink) -> _Client:
    gate, _, _, _ = _stack(_Holding(), sink=sink, body_read_s=0.05)
    client = _stalled_client()
    await asyncio.wait_for(gate(_scope(), client.receive, client.send), 2)
    return client


async def _oversize(sink: ListSink) -> _Client:
    gate, _, _, _ = _stack(_Holding(), sink=sink, max_body_bytes=4, max_draining_bytes=64)
    client = _Client((b"12345",))
    await gate(_scope(), client.receive, client.send)
    return client


_REFUSALS: dict[str, tuple[Any, int]] = {
    "capacity": (_capacity, 429),
    "body_timeout": (_body_timeout, 408),
    "oversize": (_oversize, 422),
}


@pytest.mark.parametrize("name", list(_REFUSALS))
async def test_the_client_and_the_audit_record_get_the_same_code(name: str) -> None:
    drive, status = _REFUSALS[name]
    sink = ListSink()

    client = await drive(sink)

    assert client.status == status
    error = client.json()["error"]
    (record,) = [r for r in sink.records if r.get("block_reason") == error["reason"]]
    assert record["error_code"] == error["code"]
    assert re.fullmatch(r"E_[A-Z][A-Z_]*", error["code"])


async def test_no_two_limiter_refusals_share_a_code() -> None:
    codes = []
    for drive, _ in _REFUSALS.values():
        client = await drive(ListSink())
        codes.append(client.json()["error"]["code"])

    assert len(set(codes)) == len(codes)
    assert "E_ROUTE_BLOCKED" not in codes
