"""``ResponsesStreamDesanitizer`` never lets held text trail the event that ends it: an
item's tails go out before its ``output_item.done``, every tail before a terminal event
(``response.completed`` / ``failed`` / ``incomplete`` / ``error``)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from corp_llm_gateway.sanitizer import ResponsesStreamDesanitizer, StrategyResult

EMAIL = "alice@corp.example"
MAPPING = StrategyResult(pairs=((EMAIL, "[EMAIL_1]"),))
TEXT = {"item_id": "msg_1", "output_index": 0, "content_index": 0}
CALL = {"item_id": "fc_1", "output_index": 1}


def feed_all(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    adapter = ResponsesStreamDesanitizer(MAPPING)
    out: list[Any] = []
    for event in events:
        out.extend(adapter.feed(event))
    out.extend(adapter.flush())
    return [json.loads(item) if isinstance(item, str) else item for item in out]


def delta(text: str, n: int) -> dict[str, Any]:
    return {"type": "response.output_text.delta", "sequence_number": n, **TEXT, "delta": text}


def args_delta(text: str, n: int) -> dict[str, Any]:
    return {
        "type": "response.function_call_arguments.delta",
        "sequence_number": n,
        **CALL,
        "delta": text,
    }


def before(out: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return out[: next(i for i, e in enumerate(out) if e["type"] == event_type)]


def text_of(events: list[dict[str, Any]]) -> str:
    return "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta")


TERMINALS = {
    "completed": {"type": "response.completed", "response": {"id": "r", "output": []}},
    "failed": {"type": "response.failed", "response": {"id": "r", "status": "failed"}},
    "incomplete": {"type": "response.incomplete", "response": {"id": "r", "output": []}},
    "error": {"type": "error", "code": "server_error", "message": "upstream failed"},
}


@pytest.mark.parametrize("terminal", sorted(TERMINALS))
def test_held_text_goes_out_before_the_terminal_event(terminal: str) -> None:
    """No ``output_text.done`` arrives: the held tail must still precede the end."""
    event = {**TERMINALS[terminal], "sequence_number": 3}

    out = feed_all([delta("mail [EM", 1), delta("AIL_1]", 2), event])

    assert text_of(before(out, event["type"])) == f"mail {EMAIL}"
    assert out[-1]["type"] == event["type"]


def test_a_failed_response_is_restored_too() -> None:
    event = {
        "type": "response.failed",
        "sequence_number": 1,
        "response": {
            "id": "r",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "[EMAIL_1]"}]}
            ],
        },
    }

    (out,) = feed_all([event])

    assert EMAIL in json.dumps(out) and "[EMAIL_1]" not in json.dumps(out)


def test_function_call_arguments_are_complete_before_the_item_is_done() -> None:
    args = json.dumps({"to": "[EMAIL_1]"})
    item_done = {
        "type": "response.output_item.done",
        "sequence_number": 3,
        "output_index": 1,
        "item": {"id": "fc_1", "type": "function_call", "name": "mail", "arguments": args},
    }

    out = feed_all([args_delta(args[:9], 1), args_delta(args[9:], 2), item_done])

    streamed = "".join(
        e["delta"]
        for e in before(out, "response.output_item.done")
        if e["type"] == "response.function_call_arguments.delta"
    )
    assert json.loads(streamed) == {"to": EMAIL}
    assert json.loads(out[-1]["item"]["arguments"]) == {"to": EMAIL}


def test_an_item_done_leaves_another_items_buffer_alone() -> None:
    item_done = {
        "type": "response.output_item.done",
        "sequence_number": 3,
        "output_index": 1,
        "item": {"id": "fc_1", "type": "function_call", "arguments": "{}"},
    }

    out = feed_all([delta("mail [EMAIL_1]", 1), item_done, delta(" ok", 4)])

    assert text_of(out) == f"mail {EMAIL} ok"
    head = before(out, "response.output_item.done")
    assert [e["type"] for e in head] == ["response.output_text.delta"]
    assert text_of(head) != f"mail {EMAIL}"


# ── sequence_number ──────────────────────────────────────────────────────────


def _item_done(n: int) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "sequence_number": n,
        "output_index": 0,
        "item": {"id": "msg_1", "type": "message", "content": []},
    }


def _part_done(n: int) -> dict[str, Any]:
    return {
        "type": "response.content_part.done",
        "sequence_number": n,
        **TEXT,
        "part": {"type": "output_text", "text": "mail [EMAIL_1]"},
    }


def _text_done(n: int) -> dict[str, Any]:
    return {"type": "response.output_text.done", "sequence_number": n, **TEXT, "text": "x"}


SEQUENCED = {
    **{
        f"terminal-{name}": [
            delta("please mail [EM", 1),
            delta("AIL_1]", 2),
            {**event, "sequence_number": 3},
        ]
        for name, event in TERMINALS.items()
    },
    "output_item.done": [delta("please mail [EM", 1), delta("AIL_1]", 2), _item_done(3)],
    "content_part.done": [delta("please mail [EM", 1), delta("AIL_1]", 2), _part_done(3)],
    "output_text.done": [delta("please mail [EM", 1), delta("AIL_1]", 2), _text_done(3)],
    "function_call item.done": [
        args_delta('{"to": "[EM', 1),
        args_delta('AIL_1]"}', 2),
        {
            "type": "response.output_item.done",
            "sequence_number": 3,
            "output_index": 1,
            "item": {"id": "fc_1", "type": "function_call", "arguments": '{"to": "[EMAIL_1]"}'},
        },
    ],
    "held past a later item": [
        delta("mail [EMAIL_1]", 1),
        _item_done(2),
        {**TERMINALS["completed"], "sequence_number": 3},
    ],
    "stream ends without a terminal": [delta("please mail [EM", 1), delta("AIL_1]", 2)],
}


@pytest.mark.parametrize("shape", sorted(SEQUENCED))
def test_every_emitted_event_gets_the_next_sequence_number(shape: str) -> None:
    """Synthetic tails included: strictly increasing by one from the first, no duplicates."""
    out = feed_all(SEQUENCED[shape])

    numbers = [event["sequence_number"] for event in out]
    assert numbers == list(range(1, len(out) + 1))


def test_a_stream_that_starts_past_one_keeps_its_first_number() -> None:
    out = feed_all(
        [
            delta("please mail [EM", 7),
            delta("AIL_1]", 8),
            {**TERMINALS["error"], "sequence_number": 9},
        ]
    )

    assert [event["sequence_number"] for event in out] == list(range(7, 7 + len(out)))


def test_a_stream_without_sequence_numbers_gets_none() -> None:
    created = {"type": "response.created", "response": {"id": "r"}}
    events = [
        created,
        {"type": "response.output_text.delta", **TEXT, "delta": "mail [EM"},
        {"type": "response.output_text.delta", **TEXT, "delta": "AIL_1]"},
        TERMINALS["completed"],
    ]
    adapter = ResponsesStreamDesanitizer(MAPPING)

    out = [item for event in events for item in adapter.feed(event)] + adapter.flush()

    assert out[0] is created
    decoded = [json.loads(item) if isinstance(item, str) else item for item in out]
    assert all("sequence_number" not in event for event in decoded)
    assert text_of(decoded) == f"mail {EMAIL}"


class _Typed:
    """The surface of a typed (pydantic) event the adapter uses."""

    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> _Typed:
        return cls(**payload)

    def model_copy(self, *, update: dict[str, Any] | None = None, deep: bool = False) -> _Typed:
        return type(self)(**{**self.__dict__, **(update or {})})


def _pydantic_event() -> type[Any]:
    pydantic = pytest.importorskip("pydantic")

    class Event(pydantic.BaseModel):
        model_config = pydantic.ConfigDict(extra="allow")
        type: str
        sequence_number: int

    return Event


@pytest.mark.parametrize("kind", ["typed", "pydantic"])
def test_typed_events_are_renumbered_as_copies(kind: str) -> None:
    make = _Typed if kind == "typed" else _pydantic_event()
    first = make(type="response.output_text.delta", sequence_number=1, **TEXT, delta="please [EM")
    second = make(type="response.output_text.delta", sequence_number=2, **TEXT, delta="AIL_1] ok")
    done = make(type="response.completed", sequence_number=3, response={"id": "r"})
    adapter = ResponsesStreamDesanitizer(MAPPING)

    out = [item for event in (first, second, done) for item in adapter.feed(event)]
    out += adapter.flush()

    decoded = [json.loads(item) if isinstance(item, str) else item.model_dump() for item in out]
    assert [event["sequence_number"] for event in decoded] == list(range(1, len(out) + 1))
    assert type(out[-1]) is make and out[-1] is not done and done.sequence_number == 3
    assert text_of(decoded) == f"please {EMAIL} ok"
