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
