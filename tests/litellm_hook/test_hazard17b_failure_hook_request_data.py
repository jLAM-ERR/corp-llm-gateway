"""Plan 20260926 hazard 17b: what a pre-call rejection hands ``async_post_call_failure_hook``.

litellm fires no failure log event for a request our pre-call refuses, but it hands every
``litellm.callbacks`` entry the request (``ProxyLogging.post_call_failure_hook``). Its
``proxy_server_request.body`` is the snapshot litellm took before any pre-call hook ran and
refreshes only after ``pre_call_hook`` returns — a Stage 5 rejection never returns.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("litellm.proxy.proxy_server", reason="litellm proxy not installed")

from litellm.integrations.custom_logger import CustomLogger

from corp_llm_gateway.litellm_hook import _refresh_request_body_snapshot
from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard
from tests.litellm_hook._dispatch_fixtures import (
    EMAIL,
    ORIGINAL_MARK,
    PLACEHOLDER,
    DispatchHarness,
    StubUpstream,
    build_ours,
    serialize,
)

CANARY = "DLP-CANARY-RAW-17B"

STAGE0_ENV = (
    f"ADMIN_EMAIL={EMAIL}\n"
    "DATABASE_URL=postgres://admin:hunter2@db.corp.lan:5432/prod\n"
    "SECRET_KEY=supersecretvalue-abc123\n"
    "DEBUG=False\n"
    "REDIS_URL=redis://cache.corp.lan:6379/0\n"
    "ALLOWED_HOSTS=*.corp.lan\n"
)


class FailureCapture(CustomLogger):
    """The request each failure hook call receives, serialised per key on arrival."""

    def __init__(self) -> None:
        super().__init__()
        self.request_data: list[dict[str, str]] = []
        self.logged_failures = 0

    async def async_post_call_failure_hook(
        self,
        request_data: dict[str, Any],
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: str | None = None,
    ) -> None:
        self.request_data.append({key: serialize(value) for key, value in request_data.items()})

    async def async_log_failure_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self.logged_failures += 1


@pytest.fixture
def upstream() -> Iterator[StubUpstream]:
    stub = StubUpstream()
    yield stub
    stub.close()


async def _rejected(
    monkeypatch: pytest.MonkeyPatch,
    upstream: StubUpstream,
    route: str,
    *,
    stream: bool,
    content: str,
) -> tuple[int, str, dict[str, str]]:
    ours, _ = build_ours()
    ours._dlp_guard = DlpEgressGuard(canary_patterns=[CANARY], secret_rescan=False)
    capture = FailureCapture()
    harness = DispatchHarness(monkeypatch, upstream, [ours, capture])

    exchange = await harness.send(route, stream=stream, content=content)

    assert exchange.provider_bodies == []
    assert capture.logged_failures == 0
    (request_data,) = capture.request_data
    return exchange.status, exchange.text, request_data


def _keys_holding(request_data: dict[str, str], needle: str) -> set[str]:
    return {key for key, value in request_data.items() if needle in value}


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("route", ["chat", "messages", "responses"])
async def test_a_dlp_rejection_hands_the_failure_hook_the_sanitised_request(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, route: str, stream: bool
) -> None:
    status, text, request_data = await _rejected(
        monkeypatch, upstream, route, stream=stream, content=f"write to {EMAIL} {CANARY}"
    )

    assert status == 422
    assert "E_DLP_BLOCKED" in text
    assert _keys_holding(request_data, ORIGINAL_MARK) == set()
    assert PLACEHOLDER in request_data["proxy_server_request"]


@pytest.mark.parametrize("route", ["chat", "messages", "responses"])
async def test_a_stage0_rejection_hands_the_failure_hook_the_original(
    monkeypatch: pytest.MonkeyPatch, upstream: StubUpstream, route: str
) -> None:
    """Characterisation, not a goal: Stage 0 refuses before anything is rewritten, so the
    request litellm hands the failure hook is the original by construction. No callback
    of ours overrides that hook, and the config guard keeps any other one out."""
    status, text, request_data = await _rejected(
        monkeypatch, upstream, route, stream=False, content=STAGE0_ENV
    )

    assert status == 422
    assert "E_POLICY_BLOCKED" in text
    body_key = "input" if route == "responses" else "messages"
    assert {body_key, "proxy_server_request"} <= _keys_holding(request_data, ORIGINAL_MARK)


def test_the_body_snapshot_takes_the_rewritten_content_keys() -> None:
    body: dict[str, Any] = {
        "model": "m",
        "messages": [{"role": "user", "content": EMAIL}],
        "system": EMAIL,
        "instructions": EMAIL,
    }
    data: dict[str, Any] = {
        "model": "m",
        "messages": [{"role": "user", "content": PLACEHOLDER}],
        "system": PLACEHOLDER,
        "instructions": PLACEHOLDER,
        "proxy_server_request": {"body": body},
    }

    _refresh_request_body_snapshot(data)

    assert data["proxy_server_request"]["body"] is body
    assert ORIGINAL_MARK not in serialize(body)
    assert body["messages"] is data["messages"]


def test_the_body_snapshot_gains_no_key_the_request_added() -> None:
    body: dict[str, Any] = {"input": EMAIL}
    data: dict[str, Any] = {
        "input": PLACEHOLDER,
        "api_key": "sk-byok",
        "extra_headers": {"chatgpt-account-id": "acct"},
        "proxy_server_request": {"body": body},
    }

    _refresh_request_body_snapshot(data)

    assert body == {"input": PLACEHOLDER}


@pytest.mark.parametrize(
    "data",
    [
        {"input": PLACEHOLDER},
        {"input": PLACEHOLDER, "proxy_server_request": None},
        {"input": PLACEHOLDER, "proxy_server_request": {}},
        {"input": PLACEHOLDER, "proxy_server_request": {"body": "raw"}},
    ],
    ids=["no-request", "none", "no-body", "non-dict-body"],
)
def test_without_a_body_snapshot_the_refresh_is_a_no_op(data: dict[str, Any]) -> None:
    before = dict(data)

    _refresh_request_body_snapshot(data)

    assert data == before
