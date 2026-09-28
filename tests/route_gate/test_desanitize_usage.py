"""Token counts the ASGI desanitiser reads off a response for the terminal record.

A ticketed chat stream always asks the provider for its usage chunk (the pre-call sets
``stream_options.include_usage``); when the client did not ask for it the desanitiser
drops that chunk, so the client's wire is what it requested. A pass-mode JSON body is
read for its ``usage`` only up to ``_USAGE_BODY_CAP``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from corp_llm_gateway.route_gate import desanitize_middleware
from corp_llm_gateway.route_gate.desanitize_middleware import DesanitizeMiddleware, ResponseMappings
from corp_llm_gateway.route_gate.inflight import RequestTicket
from corp_llm_gateway.route_gate.terminal_audit import (
    AuditFacts,
    TerminalAudit,
    TerminalRecord,
    deposit,
)
from corp_llm_gateway.sanitizer.strategies import StrategyResult
from tests.response_restore import body_of, drive

EMAIL = "alice.secret@corp.example"
PLACEHOLDER = "[EMAIL_1]"
META = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": 1, "model": "m"}
USAGE = {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}


def _event(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    return _event({**META, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


# litellm's own (one empty choice), and OpenAI's (no choice), usage chunk.
USAGE_CHUNK = _event({**META, "choices": [{"index": 0, "delta": {}}], "usage": USAGE})
OPENAI_USAGE_CHUNK = _event({**META, "choices": [], "usage": USAGE})
DONE = b"data: [DONE]\n\n"
STREAM = [
    _chunk({"role": "assistant", "content": f"to {PLACEHOLDER}"}),
    _chunk({}, "stop"),
    USAGE_CHUNK,
    DONE,
]


class _Records:
    def __init__(self) -> None:
        self.records: list[TerminalRecord] = []

    async def __call__(self, record: TerminalRecord) -> None:
        self.records.append(record)


def _sse_app(pieces: list[bytes]) -> Any:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        for piece in pieces:
            await send({"type": "http.response.body", "body": piece, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    return app


def _json_app(body: bytes) -> Any:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    return app


async def _serve(
    app: Any, *, asked: bool, restore: bool
) -> tuple[list[dict[str, Any]], TerminalRecord]:
    mappings = ResponseMappings()
    records = _Records()
    terminal = TerminalAudit(records)
    ticket = RequestTicket("f" * 32)
    facts = AuditFacts(
        request_id="call-1",
        user_id="alice",
        team_id="t1",
        provider="openai",
        model="gpt-4o",
        client_asked_usage=asked,
    )
    assert deposit(ticket, facts)
    if restore:
        assert mappings.register(ticket, StrategyResult(pairs=((EMAIL, PLACEHOLDER),)))
    sent = await drive(DesanitizeMiddleware(app, mappings, terminal=terminal), ticket)
    ticket.close()
    await terminal.drain()
    (record,) = records.records
    return sent, record


def _counts(record: TerminalRecord) -> tuple[int, int]:
    event = record.event()
    return event.prompt_token_count, event.completion_token_count


@pytest.mark.parametrize(
    "usage_chunk", [USAGE_CHUNK, OPENAI_USAGE_CHUNK], ids=["litellm", "openai"]
)
@pytest.mark.parametrize("restore", [False, True], ids=["pass", "restore"])
async def test_the_usage_chunk_the_client_did_not_ask_for_is_read_and_dropped(
    restore: bool, usage_chunk: bytes
) -> None:
    stream = [usage_chunk if piece == USAGE_CHUNK else piece for piece in STREAM]

    sent, record = await _serve(_sse_app(stream), asked=False, restore=restore)

    wire = body_of(sent)
    assert b'"usage"' not in wire
    assert wire.endswith(DONE)
    assert (PLACEHOLDER.encode() in wire) is not restore
    assert sent[-1]["more_body"] is False
    assert (record.outcome, _counts(record)) == ("ok", (7, 2))


@pytest.mark.parametrize("restore", [False, True], ids=["pass", "restore"])
async def test_the_usage_chunk_the_client_asked_for_is_delivered(restore: bool) -> None:
    sent, record = await _serve(_sse_app(STREAM), asked=True, restore=restore)

    wire = body_of(sent)
    assert wire.count(b'"usage"') == 1
    assert wire.endswith(USAGE_CHUNK + DONE)
    assert _counts(record) == (7, 2)


async def test_pass_mode_forwards_every_other_byte_unchanged() -> None:
    """Split at arbitrary offsets, CRLF framing, a non-ASCII byte cut in half."""
    stream = [
        _chunk({"role": "assistant", "content": "é ü"}).replace(b"\n\n", b"\r\n\r\n"),
        _chunk({}, "stop"),
        USAGE_CHUNK,
        DONE,
    ]
    wire = b"".join(stream)
    cut = wire.index("é".encode()) + 1
    usage_at = wire.index(USAGE_CHUNK)
    pieces = [wire[:cut], wire[cut : usage_at + 9], wire[usage_at + 9 :]]

    sent, record = await _serve(_sse_app(pieces), asked=False, restore=False)

    assert body_of(sent) == wire.replace(USAGE_CHUNK, b"")
    assert _counts(record) == (7, 2)


@pytest.mark.parametrize("restore", [False, True], ids=["pass", "restore"])
async def test_a_chat_stream_with_no_usage_chunk_ends_cleanly(restore: bool) -> None:
    stream = [piece for piece in STREAM if piece != USAGE_CHUNK]

    sent, record = await _serve(_sse_app(stream), asked=False, restore=restore)

    assert body_of(sent).endswith(DONE)
    assert sent[-1]["more_body"] is False
    assert (record.outcome, _counts(record)) == ("ok", (0, 0))


@pytest.mark.parametrize(
    "choice",
    [
        {"index": 0, "delta": {}, "finish_reason": "stop"},
        {"index": 0, "delta": {"content": ""}},
        {"index": 0, "delta": {}, "logprobs": {"content": []}},
    ],
    ids=["finish", "content", "logprobs"],
)
async def test_usage_on_a_chunk_with_a_choice_is_never_dropped(choice: dict[str, Any]) -> None:
    """litellm's own rule: only a chunk whose every choice is empty is stripped."""
    last = _event({**META, "choices": [choice], "usage": USAGE})
    stream = [STREAM[0], last, DONE]

    sent, record = await _serve(_sse_app(stream), asked=False, restore=False)

    assert body_of(sent) == b"".join(stream)
    assert _counts(record) == (7, 2)


def test_the_json_usage_tap_reads_at_most_one_mib() -> None:
    assert desanitize_middleware._USAGE_BODY_CAP == 1024 * 1024


@pytest.mark.parametrize(
    ("padding", "expected"), [(1000, (7, 2)), (1024 * 1024, (0, 0))], ids=["small", "over-cap"]
)
async def test_a_pass_mode_json_body_past_the_cap_gets_no_counts(
    padding: int, expected: tuple[int, int]
) -> None:
    body = json.dumps({"id": "x", "pad": "p" * padding, "usage": USAGE}).encode()

    sent, record = await _serve(_json_app(body), asked=True, restore=False)

    assert body_of(sent) == body
    assert _counts(record) == expected
