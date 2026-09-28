"""OpenAI chat-completions SSE through ``SseStreamDesanitizer``: the shape a real client
accumulates. Tail chunks are full chunks of the stream they belong to, every choice and
every tool call has its own buffer, and nothing held is sent after the chunk that ends
its choice, after ``[DONE]`` or after an error event.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from corp_llm_gateway.sanitizer import SseStreamDesanitizer, StrategyResult

EMAIL = "alice@corp.example"
NAME = "Иван Петров"
MAPPING = StrategyResult(pairs=((EMAIL, "[EMAIL_1]"), (NAME, "[NAME_1]")))
META = {
    "id": "chatcmpl-9",
    "object": "chat.completion.chunk",
    "created": 1758500000,
    "model": "gpt-4o-mini",
    "system_fingerprint": "fp_1",
}


def chunk(*choices: dict[str, Any], **extra: Any) -> bytes:
    return (
        b"data: "
        + json.dumps({**META, "choices": list(choices), **extra}, ensure_ascii=False).encode()
        + b"\n\n"
    )


def choice(index: int, finish: str | None = None, **delta: Any) -> dict[str, Any]:
    return {"index": index, "delta": delta, "finish_reason": finish}


def tool(
    index: int, arguments: str, *, call_id: str | None = None, name: str | None = None
) -> dict[str, Any]:
    if call_id is None:
        return {"index": index, "function": {"arguments": arguments}}
    return {
        "index": index,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


DONE = b"data: [DONE]\n\n"


def run(events: list[bytes], mapping: StrategyResult = MAPPING) -> list[bytes]:
    sse = SseStreamDesanitizer(mapping)
    out: list[bytes] = []
    for event in events:
        out.extend(sse.feed(event))  # type: ignore[arg-type]
    out.extend(sse.flush())  # type: ignore[arg-type]
    return out


def events_of(out: list[bytes]) -> list[Any]:
    """Every ``data:`` payload, in order; ``[DONE]`` as the string."""
    found: list[Any] = []
    for line in b"".join(out).decode().splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            found.append(payload if payload == "[DONE]" else json.loads(payload))
    return found


def text_of(events: list[Any], index: int = 0) -> str:
    return "".join(
        c["delta"].get("content") or ""
        for e in events
        if isinstance(e, dict)
        for c in e.get("choices", [])
        if c.get("index") == index
    )


def arguments_of(events: list[Any], choice_index: int = 0) -> dict[int, str]:
    found: dict[int, str] = {}
    for e in events:
        if not isinstance(e, dict):
            continue
        for c in e.get("choices", []):
            if c.get("index") != choice_index:
                continue
            for call in c["delta"].get("tool_calls") or []:
                found[call["index"]] = found.get(call["index"], "") + call["function"]["arguments"]
    return found


def test_a_tail_chunk_is_a_full_chunk_of_its_stream() -> None:
    out = events_of(run([chunk(choice(0, content="to [EMAIL_1")), chunk(choice(0, content="]"))]))

    assert text_of(out) == f"to {EMAIL}"
    for event in out:
        assert {key: event[key] for key in META} == META
        assert [c["index"] for c in event["choices"]] == [0]


def test_held_text_rides_in_the_chunk_that_carries_finish_reason() -> None:
    out = events_of(
        run(
            [
                chunk(choice(0, role="assistant", content="to [EM")),
                chunk(choice(0, content="AIL_1]")),
                chunk(choice(0, "stop")),
                chunk(usage={"prompt_tokens": 3, "completion_tokens": 2}),
                DONE,
            ]
        )
    )

    finish_at = next(
        i
        for i, e in enumerate(out)
        if isinstance(e, dict)
        and e["choices"][:1]
        and e["choices"][0].get("finish_reason") == "stop"
    )
    assert text_of(out[: finish_at + 1]) == f"to {EMAIL}"
    assert text_of(out[finish_at + 1 :]) == ""
    assert out[-1] == "[DONE]"


def test_a_finish_chunk_with_its_own_content_keeps_the_order() -> None:
    out = events_of(
        run([chunk(choice(0, content="to [EMAIL")), chunk(choice(0, "stop", content="_1] ok"))])
    )

    assert text_of(out) == f"to {EMAIL} ok"
    assert out[-1]["choices"][0]["finish_reason"] == "stop"


def test_held_text_is_sent_before_done_never_after() -> None:
    out = events_of(run([chunk(choice(0, content="to [EMAIL_1]")), DONE]))

    assert out[-1] == "[DONE]"
    assert text_of(out[:-1]) == f"to {EMAIL}"


def test_held_text_is_sent_before_an_error_event() -> None:
    error = b'data: {"error": {"message": "upstream gone", "code": 500}}\n\n'

    out = events_of(run([chunk(choice(0, content="to [EMAIL_1]")), error]))

    assert out[-1] == {"error": {"message": "upstream gone", "code": 500}}
    assert text_of(out[:-1]) == f"to {EMAIL}"


def test_each_choice_has_its_own_buffer() -> None:
    """``n=2``: a placeholder split in both choices, interleaved; each is restored in its
    own choice and keeps its index."""
    out = events_of(
        run(
            [
                chunk(choice(0, content="a [EMA"), choice(1, content="b [NAM")),
                chunk(choice(1, content="E_1]")),
                chunk(choice(0, content="IL_1]")),
                chunk(choice(0, "stop"), choice(1, "stop")),
                DONE,
            ]
        )
    )

    assert text_of(out, 0) == f"a {EMAIL}"
    assert text_of(out, 1) == f"b {NAME}"


def test_a_second_choice_is_restored_too() -> None:
    out = events_of(run([chunk(choice(0, content="x"), choice(1, content="[NAME_1] y")), DONE]))

    assert text_of(out, 1) == f"{NAME} y"
    assert "[NAME_1]" not in json.dumps(out, ensure_ascii=False)


def _parallel_calls() -> list[bytes]:
    first = json.dumps({"to": "[EMAIL_1]"})
    second = json.dumps({"name": "[NAME_1]"})
    return [
        chunk(choice(0, tool_calls=[tool(0, "", call_id="call_a", name="mail")])),
        chunk(choice(0, tool_calls=[tool(0, first[:10])])),
        chunk(choice(0, tool_calls=[tool(0, first[10:])])),
        chunk(choice(0, tool_calls=[tool(1, "", call_id="call_b", name="who")])),
        chunk(choice(0, tool_calls=[tool(1, second[:12])])),
        chunk(choice(0, tool_calls=[tool(1, second[12:])])),
        chunk(choice(0, "tool_calls")),
        DONE,
    ]


def test_parallel_tool_calls_get_complete_arguments() -> None:
    out = events_of(run(_parallel_calls()))

    args = arguments_of(out)
    assert json.loads(args[0]) == {"to": EMAIL}
    assert json.loads(args[1]) == {"name": NAME}


def test_a_tool_calls_tail_is_sent_before_the_next_tool_call_starts() -> None:
    """A client closes a tool call when the next index appears: its arguments must be
    complete by then."""
    out = events_of(run(_parallel_calls()))

    second_starts = next(
        i
        for i, e in enumerate(out)
        if isinstance(e, dict)
        and any(
            call.get("id") == "call_b" for call in e["choices"][0]["delta"].get("tool_calls") or []
        )
    )
    assert json.loads(arguments_of(out[:second_starts])[0]) == {"to": EMAIL}


def test_held_content_is_sent_before_the_first_tool_call() -> None:
    out = events_of(
        run(
            [
                chunk(choice(0, content="see [EMAIL_1]")),
                chunk(choice(0, tool_calls=[tool(0, "{}", call_id="c", name="f")])),
                chunk(choice(0, "tool_calls")),
                DONE,
            ]
        )
    )

    first_call = next(
        i
        for i, e in enumerate(out)
        if isinstance(e, dict) and e["choices"][0]["delta"].get("tool_calls")
    )
    assert text_of(out[:first_call]) == f"see {EMAIL}"


def test_utf8_split_across_feeds_is_restored() -> None:
    whole = chunk(choice(0, content="кому: [NAME_1], Иван")) + chunk(choice(0, "stop")) + DONE
    cut = whole.index("Иван".encode()) + 1
    pieces = [whole[:5], whole[5:cut], whole[cut:]]

    out = events_of(run(pieces))

    assert text_of(out) == f"кому: {NAME}, Иван"
    assert "�" not in text_of(out)


def test_a_chunk_with_nothing_held_passes_through_byte_identical() -> None:
    finish = chunk(choice(0, "stop"))

    assert run([finish]) == [finish]


# ── the installed OpenAI SDK's own accumulator ───────────────────────────────


def _accumulate(out: list[bytes]) -> tuple[Any, list[Any]]:
    """What ``client.chat.completions.stream`` does with the bytes: each ``data:`` payload
    built as a ``ChatCompletionChunk`` the way the SDK builds it, stop at ``[DONE]``."""
    openai = pytest.importorskip("openai")
    from openai._models import construct_type
    from openai.lib.streaming.chat import ChatCompletionStreamState
    from openai.types.chat import ChatCompletionChunk

    del openai
    state: ChatCompletionStreamState[Any] = ChatCompletionStreamState()
    events: list[Any] = []
    for payload in events_of(out):
        if payload == "[DONE]":
            break
        events.extend(state.handle_chunk(construct_type(type_=ChatCompletionChunk, value=payload)))
    return state.get_final_completion(), events


def test_the_sdk_accumulates_restored_text_and_a_complete_content_done() -> None:
    out = run(
        [
            chunk(choice(0, role="assistant", content="to [EM")),
            chunk(choice(0, content="AIL_1] and [NAME")),
            chunk(choice(0, content="_1]")),
            chunk(choice(0, "stop")),
            DONE,
        ]
    )

    final, events = _accumulate(out)

    assert final.choices[0].message.content == f"to {EMAIL} and {NAME}"
    (done,) = [e for e in events if e.type == "content.done"]
    assert done.content == f"to {EMAIL} and {NAME}"


def test_the_sdk_accumulates_both_choices() -> None:
    out = run(
        [
            chunk(
                choice(0, role="assistant", content="a [EMA"),
                choice(1, role="assistant", content="b [NAM"),
            ),
            chunk(choice(1, content="E_1]")),
            chunk(choice(0, content="IL_1]")),
            chunk(choice(0, "stop"), choice(1, "stop")),
            DONE,
        ]
    )

    final, _ = _accumulate(out)

    assert [c.message.content for c in final.choices] == [f"a {EMAIL}", f"b {NAME}"]


def test_the_sdk_sees_complete_arguments_in_every_tool_call_done() -> None:
    out = run([chunk(choice(0, role="assistant")), *_parallel_calls()])

    final, events = _accumulate(out)

    calls = final.choices[0].message.tool_calls
    assert [json.loads(c.function.arguments) for c in calls] == [{"to": EMAIL}, {"name": NAME}]
    done = [e for e in events if e.type == "tool_calls.function.arguments.done"]
    assert [json.loads(e.arguments) for e in done] == [{"to": EMAIL}, {"name": NAME}]
