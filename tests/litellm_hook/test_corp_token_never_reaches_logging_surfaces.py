"""Plan 20260926 hazard 18: the corp token never reaches litellm's logging surfaces.

On chat litellm mirrors the inbound headers into ``metadata["headers"]`` and then
deep-copies that metadata into ``metadata["requester_metadata"]``
(``litellm_pre_call_utils.py:2047`` then ``:2164``), before any pre-call hook runs. The
copy is its own headers dict; the logging object shares the metadata, so every success
and failure ``StandardLoggingPayload``, the log kwargs and the spend-log
``proxy_server_request`` carried ``X-Corp-Auth``. litellm also hands a request our
pre-call refuses, as it stands, to every ``async_post_call_failure_hook``: the strip ran
after authentication, so a 401 handed over the token on every route.
Invariant 4 (CLAUDE.md): the token is never logged.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

pytest.importorskip("litellm.proxy.proxy_server", reason="litellm proxy not installed")

from litellm._logging import verbose_logger, verbose_proxy_logger, verbose_router_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.spend_tracking.spend_tracking_utils import get_logging_payload

from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard
from tests.hook_fixtures import _build_guardrail
from tests.litellm_hook._dispatch_fixtures import (
    EMAIL,
    PLACEHOLDER,
    DispatchHarness,
    StubUpstream,
    until,
)

CORP_TOKEN = "corp-tok-canary-h18-9d4c2e"
BYOK = "Bearer byok-h18-keep-7a1f"
DLP_CANARY = "DLP-CANARY-RAW-H18"
STRIPPED_MARKER = "litellm_pre_call_corp_token_stripped"

ROUTES = ["chat", "messages", "responses"]
# How each request ends: the provider answers, the provider refuses, or litellm fails it
# after our pre-call and before the provider's own pre_call (hazard 17's window).
OUTCOMES: dict[str, tuple[int | None, dict[str, Any]]] = {
    "success": (None, {}),
    "provider-error": (400, {}),
    "pre-provider-failure": (None, {"tools": "nope"}),
}


MAX_WALK_DEPTH = 40


def token_sites(
    obj: Any, needle: str, path: str = "", seen: frozenset[int] = frozenset()
) -> list[str]:
    """Every path under ``obj`` whose value holds ``needle``: dicts, lists, pydantic
    models, httpx messages with ALL their headers, and litellm's own objects."""
    if isinstance(obj, (str, bytes)):
        text = obj.decode("utf-8", "replace") if isinstance(obj, bytes) else obj
        return [path] if needle in text else []
    if id(obj) in seen:
        return []
    if len(seen) >= MAX_WALK_DEPTH:
        raise AssertionError(f"token_sites: depth cap {MAX_WALK_DEPTH} hit at {path!r}")
    seen = seen | {id(obj)}
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            found += token_sites(key, needle, f"{path}<key>", seen)
            found += token_sites(value, needle, f"{path}.{key}", seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for index, value in enumerate(obj):
            found += token_sites(value, needle, f"{path}[{index}]", seen)
    elif isinstance(obj, (httpx.Request, httpx.Response)):
        request = obj if isinstance(obj, httpx.Request) else obj._request
        if request is not None:
            found += token_sites(dict(request.headers), needle, f"{path}.request.headers", seen)
    elif callable(getattr(obj, "model_dump", None)):
        try:
            found += token_sites(obj.model_dump(), needle, path, seen)
        except Exception:
            found += token_sites(repr(obj), needle, path, seen)
    elif type(obj).__module__.startswith("litellm") and hasattr(obj, "__dict__"):
        found += token_sites(vars(obj), needle, f"{path}<{type(obj).__name__}>", seen)
    return found


def _captured_sites(obj: Any) -> list[str]:
    """``token_sites`` inside a litellm callback, where a raise would be swallowed: a hit
    cap comes back as a site, so the empty-list assertions fail on it."""
    try:
        return token_sites(obj, CORP_TOKEN)
    except AssertionError as exc:
        return [f"<walker failed: {exc}>"]


class TokenCapture(CustomLogger):
    """Registered after ours: what our pre-call leaves in the request, every log event
    (kwargs and the spend-log row litellm would write from them), every failure hook."""

    def __init__(self) -> None:
        super().__init__()
        self.after_pre_call: list[dict[str, Any]] = []
        self.events: list[tuple[str, list[str], list[str], str]] = []
        self.failure_hook_sites: list[list[str]] = []

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: str
    ) -> Any:
        self.after_pre_call.append(data)
        return data

    def _record(self, kind: str, kwargs: dict[str, Any], response_obj: Any, start: Any, end: Any):
        # A failure event may carry no end or first-token time; the row needs both.
        end = end or datetime.now(UTC)
        timed = {**kwargs, "completion_start_time": kwargs.get("completion_start_time") or end}
        spend = dict(get_logging_payload(timed, response_obj, start or end, end))
        self.events.append(
            (
                kind,
                _captured_sites(kwargs),
                _captured_sites(spend),
                str(spend.get("proxy_server_request")),
            )
        )

    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self._record("success", kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        self._record("failure", kwargs, response_obj, start_time, end_time)

    async def async_post_call_failure_hook(
        self,
        request_data: dict[str, Any],
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: str | None = None,
    ) -> None:
        self.failure_hook_sites.append(_captured_sites(request_data))


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:
            self.messages.append(str(record.msg))

    def after_marker(self) -> list[str] | None:
        for index, message in enumerate(self.messages):
            if message.startswith(STRIPPED_MARKER):
                return self.messages[index + 1 :]
        return None


@pytest.fixture
def debug_lines() -> Iterator[_Lines]:
    """litellm's three loggers at DEBUG, and ours, into one ordered list."""
    handler = _Lines()
    loggers = [
        verbose_logger,
        verbose_proxy_logger,
        verbose_router_logger,
        logging.getLogger("corp_llm_gateway.litellm_hook"),
    ]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
    yield handler
    for lg, level in zip(loggers, levels, strict=True):
        lg.removeHandler(handler)
        lg.setLevel(level)


def _guardrail(valid_token: str = CORP_TOKEN) -> Any:
    ours, _ = _build_guardrail([(EMAIL, PLACEHOLDER)], valid_token=valid_token)
    ours._strip_inbound_headers_to_upstream = True
    return ours


def _headers_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    buckets = [data.get("headers")]
    for key in ("metadata", "litellm_metadata"):
        meta = data.get(key)
        if isinstance(meta, dict):
            buckets.append(meta.get("headers"))
            requester = meta.get("requester_metadata")
            if isinstance(requester, dict):
                buckets.append(requester.get("headers"))
    request = data.get("proxy_server_request")
    if isinstance(request, dict):
        buckets.append(request.get("headers"))
    secret_fields = data.get("secret_fields")
    if isinstance(secret_fields, dict):
        buckets.append(secret_fields.get("raw_headers"))
    return [b for b in buckets if isinstance(b, dict)]


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "sse"])
@pytest.mark.parametrize("outcome", sorted(OUTCOMES))
@pytest.mark.parametrize("route", ROUTES)
async def test_no_logging_surface_holds_the_corp_token(
    monkeypatch: pytest.MonkeyPatch,
    debug_lines: _Lines,
    route: str,
    outcome: str,
    stream: bool,
) -> None:
    error_status, extra = OUTCOMES[outcome]
    upstream = StubUpstream(error_status=error_status)
    try:
        capture = TokenCapture()
        harness = DispatchHarness(
            monkeypatch,
            upstream,
            [_guardrail(), capture],
            general_settings={"store_prompts_in_spend_logs": True},
        )
        exchange = await harness.send(
            route, stream=stream, extra=extra, token=CORP_TOKEN, headers={"Authorization": BYOK}
        )
        await until(lambda: len(capture.events) >= 1)
    finally:
        upstream.close()

    assert (exchange.status == 200) is (outcome == "success")
    assert {kind for kind, *_ in capture.events} == {
        "success" if outcome == "success" else "failure"
    }
    for _, kwargs_sites, spend_sites, spend_request in capture.events:
        assert kwargs_sites == []
        assert spend_sites == []
        # Not vacuous: prompts are on, so the row carries the (sanitised) request.
        assert PLACEHOLDER in spend_request
    assert all(sites == [] for sites in capture.failure_hook_sites)
    after = debug_lines.after_marker()
    assert after, "the strip marker, and litellm DEBUG lines after it"
    assert [line for line in after if CORP_TOKEN in line] == []


@pytest.mark.parametrize("route", ROUTES)
async def test_the_byok_authorization_survives_where_the_corp_token_is_dropped(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """Invariant 3: only ``x-corp-auth`` is dropped. litellm keeps the developer's
    ``Authorization`` out of its logged header copies and in ``secret_fields.raw_headers``
    (what its BYOK forwarding reads); there it stays byte-exact."""
    upstream = StubUpstream()
    try:
        capture = TokenCapture()
        harness = DispatchHarness(monkeypatch, upstream, [_guardrail(), capture])
        exchange = await harness.send(
            route, stream=False, token=CORP_TOKEN, headers={"Authorization": BYOK}
        )
    finally:
        upstream.close()

    assert exchange.status == 200
    (data,) = capture.after_pre_call
    buckets = _headers_of(data)
    assert token_sites(buckets, CORP_TOKEN) == []
    assert all(k.lower() != "x-corp-auth" for bucket in buckets for k in bucket)
    carried = [v for bucket in buckets for k, v in bucket.items() if k.lower() == "authorization"]
    assert set(carried) == {BYOK}
    assert data["secret_fields"]["raw_headers"]["authorization"] == BYOK


REFUSALS = {"unknown-token": 401, "dlp": 422}


@pytest.mark.parametrize("refusal", sorted(REFUSALS))
@pytest.mark.parametrize("route", ROUTES)
async def test_a_refused_request_hands_no_failure_hook_the_corp_token(
    monkeypatch: pytest.MonkeyPatch, route: str, refusal: str
) -> None:
    """litellm hands a request our pre-call refuses to every failure hook: an unknown
    (revoked, expired, mistyped) token and a Stage 5 block alike."""
    ours = _guardrail("some-other-valid-token" if refusal == "unknown-token" else CORP_TOKEN)
    ours._dlp_guard = DlpEgressGuard(canary_patterns=[DLP_CANARY], secret_rescan=False)
    upstream = StubUpstream()
    try:
        capture = TokenCapture()
        harness = DispatchHarness(monkeypatch, upstream, [ours, capture])
        exchange = await harness.send(
            route,
            stream=False,
            token=CORP_TOKEN,
            headers={"Authorization": BYOK},
            content=f"write to {EMAIL} {DLP_CANARY}" if refusal == "dlp" else None,
        )
    finally:
        upstream.close()

    assert exchange.status == REFUSALS[refusal]
    assert exchange.provider_bodies == []
    assert capture.failure_hook_sites == [[]]


def test_the_walker_finds_a_token_in_every_shape_it_reads() -> None:
    request = httpx.Request("POST", "http://u/v1", headers={"x-corp-auth": CORP_TOKEN})
    shapes: list[Any] = [
        {"a": {"b": [CORP_TOKEN]}},
        {CORP_TOKEN: 1},
        (CORP_TOKEN.encode(),),
        request,
        httpx.Response(200, request=request),
    ]

    for shape in shapes:
        assert token_sites(shape, CORP_TOKEN), shape
    assert token_sites({"a": "clean"}, CORP_TOKEN) == []


def test_the_walker_fails_loudly_at_its_depth_cap() -> None:
    deep: Any = "clean"
    for _ in range(MAX_WALK_DEPTH + 1):
        deep = {"n": deep}

    with pytest.raises(AssertionError, match="depth cap"):
        token_sites(deep, CORP_TOKEN)
    (site,) = _captured_sites(deep)
    assert site.startswith("<walker failed")
