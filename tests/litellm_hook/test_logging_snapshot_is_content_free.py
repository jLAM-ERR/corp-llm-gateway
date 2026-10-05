"""Plan 20260926 hazard 17: litellm's logging snapshot of the request is sanitised.

litellm builds the call's logging object (``function_setup``) before any pre-call hook
runs, and on ``/v1/responses`` nothing refreshes its ``messages`` afterwards (the proxy
calls ``update_messages`` for a ``messages`` body only). The ``StandardLoggingPayload``
every ``litellm.callbacks`` entry receives, success and failure alike, is built from that
object. Our pre-call hands the logging object the sanitised request.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

pytest.importorskip("litellm.proxy.proxy_server", reason="litellm proxy not installed")

from litellm.integrations.custom_logger import CustomLogger

from corp_llm_gateway.litellm_hook import _refresh_logging_snapshot
from tests.litellm_hook._dispatch_fixtures import (
    EMAIL,
    ORIGINAL_MARK,
    PLACEHOLDER,
    DispatchHarness,
    StubUpstream,
    build_ours,
    serialize,
    until,
)

RESPONSES_INPUTS: dict[str, Any] = {
    "string": f"write to {EMAIL}",
    "role-content": [{"role": "user", "content": f"write to {EMAIL}"}],
    "typed-items": [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": f"write to {EMAIL}"}],
        }
    ],
}

OUTCOMES = {"success": None, "failure": 400}


class LogCapture(CustomLogger):
    """Every success and failure event litellm hands a ``litellm.callbacks`` entry."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.events.append(("success", kwargs))

    async def async_log_failure_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.events.append(("failure", kwargs))


@pytest.fixture(params=sorted(OUTCOMES))
def outcome(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.fixture
def upstream(outcome: str) -> Iterator[StubUpstream]:
    stub = StubUpstream(error_status=OUTCOMES[outcome])
    yield stub
    stub.close()


def _payload_fields_holding(payload: dict[str, Any], needle: str) -> set[str]:
    return {key for key, value in payload.items() if needle in json.dumps(value, default=str)}


async def _logged(
    monkeypatch: pytest.MonkeyPatch,
    upstream: StubUpstream,
    route: str,
    *,
    stream: bool,
    extra: dict[str, Any] | None = None,
) -> tuple[int, list[str], str, dict[str, Any]]:
    ours, _ = build_ours()
    capture = LogCapture()
    harness = DispatchHarness(monkeypatch, upstream, [ours, capture])

    exchange = await harness.send(route, stream=stream, extra=extra)
    await until(lambda: len(capture.events) >= 1)

    assert len(capture.events) == 1, [kind for kind, _ in capture.events]
    kind, kwargs = capture.events[0]
    return exchange.status, exchange.provider_bodies, kind, kwargs


def _kwargs_holding(kwargs: dict[str, Any], needle: str) -> set[str]:
    """Every top-level kwargs key whose value, walked recursively, holds ``needle``. The
    logging object is the one live, unserialisable entry; its snapshot is what the other
    keys were built from."""
    return {
        key
        for key, value in kwargs.items()
        if key != "litellm_logging_obj" and needle in serialize(value)
    }


def _assert_sanitised(kwargs: dict[str, Any], provider_bodies: list[str]) -> None:
    assert provider_bodies
    assert all(ORIGINAL_MARK not in body for body in provider_bodies)
    payload = kwargs["standard_logging_object"]
    assert _payload_fields_holding(payload, ORIGINAL_MARK) == set()
    assert PLACEHOLDER in json.dumps(payload["messages"], default=str)
    assert _kwargs_holding(kwargs, ORIGINAL_MARK) == set()


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("shape", sorted(RESPONSES_INPUTS))
async def test_responses_logging_payload_holds_placeholders_only(
    monkeypatch: pytest.MonkeyPatch,
    upstream: StubUpstream,
    outcome: str,
    shape: str,
    stream: bool,
) -> None:
    status, bodies, kind, kwargs = await _logged(
        monkeypatch, upstream, "responses", stream=stream, extra={"input": RESPONSES_INPUTS[shape]}
    )

    assert kind == outcome
    assert (status == 200) is (outcome == "success")
    _assert_sanitised(kwargs, bodies)


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("route", ["chat", "messages"])
async def test_chat_and_messages_logging_payload_stays_content_free(
    monkeypatch: pytest.MonkeyPatch,
    upstream: StubUpstream,
    outcome: str,
    route: str,
    stream: bool,
) -> None:
    status, bodies, kind, kwargs = await _logged(monkeypatch, upstream, route, stream=stream)

    assert kind == outcome
    assert (status == 200) is (outcome == "success")
    _assert_sanitised(kwargs, bodies)


class _LoggingStub:
    def __init__(self) -> None:
        self.updates: list[Any] = []
        self.model_call_details: dict[str, Any] = {"input": EMAIL}

    def update_messages(self, messages: Any) -> None:
        self.updates.append(messages)


def test_the_refreshed_snapshot_is_a_copy_not_the_live_request() -> None:
    """The logging object gets its own list, never the live request's."""
    logging_obj = _LoggingStub()
    data: dict[str, Any] = {
        "input": [{"role": "user", "content": PLACEHOLDER}],
        "litellm_logging_obj": logging_obj,
    }

    _refresh_logging_snapshot(data, "input_list")

    (logged,) = logging_obj.updates
    assert logged == data["input"]
    assert logged is not data["input"]


@pytest.mark.parametrize(
    ("data", "shape", "expected"),
    [
        ({"input": PLACEHOLDER}, "input_string", [{"role": "user", "content": PLACEHOLDER}]),
        ({"input": [PLACEHOLDER]}, "input_list", [{"role": "user", "content": PLACEHOLDER}]),
        (
            {"messages": [{"role": "user", "content": PLACEHOLDER}]},
            "messages",
            [{"role": "user", "content": PLACEHOLDER}],
        ),
        ({"messages": []}, "messages", []),
    ],
    ids=["input-string", "input-bare-strings", "messages", "empty-messages"],
)
def test_the_snapshot_takes_litellms_logged_shape(
    data: dict[str, Any], shape: str, expected: list[Any]
) -> None:
    """A string becomes one user message and a bare string item a user message, the shape
    ``Logging.__init__`` gives a request it snapshots."""
    logging_obj = _LoggingStub()
    data["litellm_logging_obj"] = logging_obj

    _refresh_logging_snapshot(data, shape)

    assert logging_obj.updates == [expected]


@pytest.mark.parametrize(
    "data",
    [
        {"input": PLACEHOLDER},
        {"input": PLACEHOLDER, "litellm_logging_obj": None},
        {"input": PLACEHOLDER, "litellm_logging_obj": object()},
    ],
    ids=["no-logging-object", "none", "no-update-messages"],
)
def test_without_a_logging_object_the_refresh_is_a_no_op(data: dict[str, Any]) -> None:
    _refresh_logging_snapshot(data, "input_string")

    assert data["input"] == PLACEHOLDER


def test_an_unmanaged_body_is_never_handed_to_the_logging_object() -> None:
    """Embeddings / moderations ``input`` is not rewritten, so it is not ours to log."""
    logging_obj = _LoggingStub()

    _refresh_logging_snapshot({"input": [EMAIL], "litellm_logging_obj": logging_obj}, "unmanaged")

    assert logging_obj.updates == []
    assert logging_obj.model_call_details == {"input": EMAIL}


@pytest.mark.parametrize(
    ("data", "shape", "expected"),
    [
        ({"input": PLACEHOLDER}, "input_string", PLACEHOLDER),
        ({"input": [PLACEHOLDER]}, "input_list", [PLACEHOLDER]),
        (
            {"messages": [{"role": "user", "content": PLACEHOLDER}]},
            "messages",
            [{"role": "user", "content": PLACEHOLDER}],
        ),
        ({"messages": []}, "messages", []),
    ],
    ids=["input-string", "input-list", "messages", "empty-messages"],
)
def test_the_logged_input_takes_what_logging_init_stores(
    data: dict[str, Any], shape: str, expected: Any
) -> None:
    """``model_call_details["input"]`` gets the request as ``Logging.__init__`` stored it,
    unconverted: the raw string, or a copy of the list."""
    logging_obj = _LoggingStub()
    data["litellm_logging_obj"] = logging_obj

    _refresh_logging_snapshot(data, shape)

    logged = logging_obj.model_call_details["input"]
    assert logged == expected
    if isinstance(expected, list):
        assert logged is not data["messages" if shape == "messages" else "input"]


def test_a_body_with_no_request_list_leaves_the_logged_input_alone() -> None:
    logging_obj = _LoggingStub()

    _refresh_logging_snapshot({"messages": None, "litellm_logging_obj": logging_obj}, "messages")

    assert logging_obj.updates == []
    assert logging_obj.model_call_details == {"input": EMAIL}


def test_a_logging_object_without_call_details_still_gets_the_messages() -> None:
    logging_obj = _LoggingStub()
    del logging_obj.model_call_details

    _refresh_logging_snapshot(
        {"input": PLACEHOLDER, "litellm_logging_obj": logging_obj}, "input_string"
    )

    assert logging_obj.updates == [[{"role": "user", "content": PLACEHOLDER}]]


@pytest.fixture
def healthy_upstream() -> Iterator[StubUpstream]:
    stub = StubUpstream()
    yield stub
    stub.close()


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("route", ["chat", "messages", "responses"])
async def test_a_failure_before_the_provider_call_logs_the_sanitised_input(
    monkeypatch: pytest.MonkeyPatch,
    healthy_upstream: StubUpstream,
    route: str,
    stream: bool,
) -> None:
    """A request litellm rejects after our pre-call and before the provider's own
    ``pre_call`` (here a malformed ``tools``) logs a failure while ``model_call_details``
    still holds the snapshot ``Logging.__init__`` took: its ``input`` must be ours too.
    A streamed call may log one failure per retry; every one is checked."""
    ours, _ = build_ours()
    capture = LogCapture()
    harness = DispatchHarness(monkeypatch, healthy_upstream, [ours, capture])

    exchange = await harness.send(route, stream=stream, extra={"tools": "nope"})
    await until(lambda: len(capture.events) >= 1)

    assert exchange.status != 200
    assert exchange.provider_bodies == []
    assert {kind for kind, _ in capture.events} == {"failure"}
    for _, kwargs in capture.events:
        assert PLACEHOLDER in serialize(kwargs["input"])
        assert _kwargs_holding(kwargs, ORIGINAL_MARK) == set()


def test_serialize_unwraps_httpx_request_and_response() -> None:
    """``repr`` of an httpx message shows ``<Response [200 OK]>``; the bodies ride inside,
    so the leak checks must see them. Credential headers stay out of the rendering."""
    request = httpx.Request(
        "POST",
        "http://upstream/v1/messages",
        headers={"authorization": "Bearer sk-live", "x-api-key": "sk-key", "x-trace": "t1"},
        content=f"sent {EMAIL}".encode(),
    )
    response = httpx.Response(200, request=request, text=f"echo {EMAIL}")

    for rendered in (serialize(request), serialize(response)):
        assert f"sent {EMAIL}" in rendered
        assert "t1" in rendered
        assert "sk-live" not in rendered and "sk-key" not in rendered
    assert f"echo {EMAIL}" in serialize(response)


def test_serialize_survives_an_unread_streaming_response() -> None:
    async def body() -> Any:
        yield EMAIL.encode()

    request = httpx.Request("POST", "http://upstream/v1/messages", content=b"{}")
    response = httpx.Response(200, request=request, content=body())

    rendered = serialize(response)

    assert "<unread>" in rendered and EMAIL not in rendered


async def test_the_messages_success_log_carries_an_httpx_response_the_checks_now_read(
    monkeypatch: pytest.MonkeyPatch, healthy_upstream: StubUpstream
) -> None:
    """``/v1/messages`` hands the success log ``httpx_response``; unwrapped, it shows the
    provider-bound request and the provider's reply, and both hold placeholders only."""
    ours, _ = build_ours()
    capture = LogCapture()
    harness = DispatchHarness(monkeypatch, healthy_upstream, [ours, capture])

    exchange = await harness.send("messages", stream=False)
    await until(lambda: len(capture.events) >= 1)

    assert exchange.status == 200
    (kind, kwargs), *_ = capture.events
    assert kind == "success"
    responses = [value for value in kwargs.values() if isinstance(value, httpx.Response)]
    assert responses
    for response in responses:
        rendered = serialize(response)
        assert PLACEHOLDER in rendered
        assert ORIGINAL_MARK not in rendered
    _assert_sanitised(kwargs, exchange.provider_bodies)
