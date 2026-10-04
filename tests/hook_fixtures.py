"""Fakes, builders and stream helpers shared by the litellm hook tests."""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from corp_llm_gateway.audit import AuditLogger, AuditWriteAmbiguousError, ListSink, Sink
from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
from corp_llm_gateway.detectors import DualNerDetector
from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
from corp_llm_gateway.metrics import MetricsExporter
from corp_llm_gateway.route_gate.terminal_audit import TerminalAudit, emit_to
from corp_llm_gateway.rules import Gazetteer, Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo, TokenStore
from tests.response_restore import restore_stream


class _StaticRules(RulesLoader):
    def __init__(self, rules: Rules | None = None) -> None:
        self._rules = rules or Rules(rules=())

    async def load(self, team_id: str) -> Rules:
        return self._rules


class _RaisingNerEngine(PIIDetector):
    """A NER engine whose model/deps are absent — raises like the real ones do."""

    async def detect(self, text: str) -> list[Finding]:
        raise RuntimeError("ner deps absent")


class _RaisingTokenStore(TokenStore):
    """F8 repro: a token store backed by a DB with no schema staged — the real
    PostgresTokenStore.lookup() raises asyncpg.UndefinedTableError with this
    exact message when `corp_tokens` hasn't been migrated."""

    async def lookup(self, corp_token: str) -> TokenInfo | None:
        raise RuntimeError('relation "corp_tokens" does not exist')

    async def revoke_user(self, user_id: str) -> int:
        raise NotImplementedError

    async def list_tokens(self, user_id: str | None = None) -> tuple[TokenInfo, ...]:
        raise NotImplementedError


class _CancellingTokenStore(TokenStore):
    """Simulates the auth backend being cancelled mid-lookup (request timeout /
    server shutdown). ``asyncio.CancelledError`` is a ``BaseException`` in
    Python 3.8+, NOT an ``Exception`` — the F8 wrapper's ``except Exception``
    must not catch it."""

    async def lookup(self, corp_token: str) -> TokenInfo | None:
        raise asyncio.CancelledError

    async def revoke_user(self, user_id: str) -> int:
        raise NotImplementedError

    async def list_tokens(self, user_id: str | None = None) -> tuple[TokenInfo, ...]:
        raise NotImplementedError


class _RecordingMetrics(MetricsExporter):
    """Records every call instead of exporting — used to assert the F8 wrapper
    still fires `gateway_failure{component}` on the unexpected-error path."""

    def __init__(self) -> None:
        self.blocks: list[str] = []
        self.failures: list[str] = []
        self.latencies: list[tuple[float, str]] = []

    def record_block(self, block_reason: str) -> None:
        self.blocks.append(block_reason)

    def record_failure(self, component: str) -> None:
        self.failures.append(component)

    def observe_request_latency(self, seconds: float, *, status: str) -> None:
        self.latencies.append((seconds, status))


def _corp_llm_returning(pairs: list[tuple[str, str]]) -> CorpLlmClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": SANITIZE_TOOL_NAME,
                                        "arguments": json.dumps(
                                            {
                                                "pairs": [
                                                    {"original": o, "replacement": r}
                                                    for o, r in pairs
                                                ]
                                            }
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CorpLlmClient("https://corp-llm.example", model="m", http=http)


def _corp_llm_unreachable() -> CorpLlmClient:
    """A corp LLM whose transport always times out.

    Simulates the corp sanitization LLM being unreachable — the 30s
    ConnectTimeout from the debug-log incident that surfaced to the dev
    as an empty ``500 {"message":"corp-llm transport error: "}``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CorpLlmClient("https://corp-llm.example", model="m", http=http)


def _corp_llm_email_per_segment() -> CorpLlmClient:
    """A corp LLM that redacts whatever single email it finds in the segment
    to ``[EMAIL_001]`` — modelling the real per-call numbering that makes two
    different emails in two segments collide on the same token."""
    email_re = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seg_text = body["messages"][-1]["content"]
        m = email_re.search(seg_text)
        pairs = [{"original": m.group(0), "replacement": "[EMAIL_001]"}] if m else []
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {
                                        "name": SANITIZE_TOOL_NAME,
                                        "arguments": json.dumps({"pairs": pairs}),
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CorpLlmClient("https://corp-llm.example", model="m", http=http)


def _build_guardrail(
    pairs: list[tuple[str, str]] | None = None,
    *,
    valid_token: str = "tok-1",
    corp_llm: CorpLlmClient | None = None,
    forward_chatgpt_auth: bool = False,
    rules: Rules | None = None,
    forward_anthropic_auth: bool = False,
    dlp_guard: DlpEgressGuard | None = None,
    metrics: MetricsExporter | None = None,
) -> tuple[CorpLlmGuardrail, ListSink]:
    pairs = pairs if pairs is not None else []
    token_store = InMemoryTokenStore()
    now = datetime.now(UTC)
    token_store.upsert(
        TokenInfo(
            corp_token=valid_token,
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    auth = AuthMiddleware(token_store)
    orch = SanitizationOrchestrator(
        corp_llm if corp_llm is not None else _corp_llm_returning(pairs),
        InMemoryMappingStore(),
        _StaticRules(rules),
    )
    sink = ListSink()
    audit_logger = AuditLogger(sink, gateway_version="0.0.1")
    return (
        CorpLlmGuardrail(
            orch,
            auth,
            audit_logger,
            forward_chatgpt_auth=forward_chatgpt_auth,
            forward_anthropic_auth=forward_anthropic_auth,
            dlp_guard=dlp_guard,
            metrics=metrics,
        ),
        sink,
    )


def _build_guardrail_oversize(
    *,
    threshold: int,
    policy: str = "fail-closed",
    deliver_teams: frozenset[str] = frozenset(),
    local_detectors: list[Any] | None = None,
) -> tuple[CorpLlmGuardrail, ListSink]:
    """A guardrail whose orchestrator trips the size threshold at *threshold* bytes."""
    token_store = InMemoryTokenStore()
    now = datetime.now(UTC)
    token_store.upsert(
        TokenInfo(
            corp_token="tok-1",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    orch = SanitizationOrchestrator(
        _corp_llm_returning([]),
        InMemoryMappingStore(),
        _StaticRules(),
        size_threshold_bytes=threshold,
        oversize_policy=policy,
        oversize_deliver_teams=deliver_teams,
        local_detectors=local_detectors,
    )
    sink = ListSink()
    return (
        CorpLlmGuardrail(
            orch, AuthMiddleware(token_store), AuditLogger(sink, gateway_version="0.0.1")
        ),
        sink,
    )


def _build_guardrail_oracle_disabled(gazetteer: Gazetteer) -> tuple[CorpLlmGuardrail, ListSink]:
    """A guardrail with NO corp-LLM client at all — the oracle switched off."""
    token_store = InMemoryTokenStore()
    now = datetime.now(UTC)
    token_store.upsert(
        TokenInfo(
            corp_token="tok-1",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    orch = SanitizationOrchestrator(
        None,
        InMemoryMappingStore(),
        _StaticRules(),
        gazetteer=gazetteer,
        oracle_enabled=False,
    )
    sink = ListSink()
    return (
        CorpLlmGuardrail(
            orch, AuthMiddleware(token_store), AuditLogger(sink, gateway_version="0.0.1")
        ),
        sink,
    )


def _data_with_token(
    token: str,
    *,
    content: str | list[Any] = "hello",
    model: str = "claude",
    system: str | list[Any] | None = None,
) -> dict:
    data = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "headers": {"X-Corp-Auth": token, "Authorization": "Bearer byok"},
    }
    if system is not None:
        data["system"] = system
    return data


class _RaisingSink(Sink):
    """An audit sink whose transport is down — write() always raises."""

    async def write(self, record: dict[str, Any]) -> None:
        raise RuntimeError("sink transport down")


def _terminal() -> tuple[TerminalAudit, ListSink]:
    records = ListSink()
    return TerminalAudit(emit_to(AuditLogger(records, gateway_version="0.0.1"))), records


class _AmbiguousAckSink(Sink):
    """Simulates a sink whose write() call already persisted the record
    downstream before raising — e.g. an HTTP response was accepted but
    reading the acknowledgement timed out."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)
        raise AuditWriteAmbiguousError("ack read timed out")


def _case_missing_token() -> tuple[CorpLlmGuardrail, dict[str, Any]]:
    g, _ = _build_guardrail()
    return g, {"messages": [], "headers": {}}


def _case_bad_request() -> tuple[CorpLlmGuardrail, dict[str, Any]]:
    g, _ = _build_guardrail()
    data = _data_with_token("tok-1", content="hi")
    data["messages"] = "not-a-list"
    return g, data


def _case_policy_blocked_ambiguous_shape() -> tuple[CorpLlmGuardrail, dict[str, Any]]:
    g, _ = _build_guardrail()
    data = {
        "model": "gpt-5.6-sol",
        "messages": [],
        "input": ["anything"],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    return g, data


def _case_provider_auth() -> tuple[CorpLlmGuardrail, dict[str, Any]]:
    g, _ = _build_guardrail(forward_chatgpt_auth=True)
    data = {
        "model": "gpt-5.6-sol",
        "input": "hello",
        "headers": {"X-Corp-Auth": "tok-1"},
    }
    return g, data


def _case_corp_llm_down() -> tuple[CorpLlmGuardrail, dict[str, Any]]:
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    data = _data_with_token("tok-1", content="hello")
    return g, data


def _case_dlp_blocked() -> tuple[CorpLlmGuardrail, dict[str, Any]]:
    g, _ = _build_guardrail(pairs=[])
    raw_key = "sk-" + "a" * 48
    data = _data_with_token("tok-1", content=f"my key is {raw_key}")
    return g, data


def _guardrail_with_ner(dual: DualNerDetector) -> CorpLlmGuardrail:
    # Reuse the oversize helper with a huge threshold (no leaf is oversize) to
    # get an orchestrator whose local cascade includes the injected NER.
    g, _ = _build_guardrail_oversize(threshold=10_000_000, local_detectors=[dual])
    return g


def _data_all_header_locations(token: str) -> dict[str, Any]:
    """Inbound headers duplicated across every location litellm may forward upstream."""
    hdrs = {"X-Corp-Auth": token, "Authorization": "Bearer byok"}
    return {
        "model": "claude",
        "messages": [{"role": "user", "content": "hello"}],
        "headers": dict(hdrs),
        "proxy_server_request": {"headers": dict(hdrs)},
        "metadata": {"headers": dict(hdrs)},
        "litellm_metadata": {"headers": dict(hdrs)},
    }


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for it in items:
        yield it


async def _anthropic_stream_through_the_callback(sse_events: list[bytes]) -> list[bytes]:
    g, _ = _build_guardrail([("user@example.com", "[EMAIL_001]")])
    data = _data_with_token("tok-1", content="email is user@example.com")
    await g.pre_call(data)
    out: list[bytes] = []
    async for chunk in restore_stream(g, data, _async_iter(sse_events)):
        assert isinstance(chunk, bytes)
        out.append(chunk)
    return out


def _sse_events(out: list[bytes]) -> list[bytes]:
    wire = b"".join(out)
    return [event + b"\n\n" for event in wire.split(b"\n\n") if event]


def _text_deltas(events: list[bytes]) -> str:
    text = ""
    for event in events:
        for line in event.decode().splitlines():
            if not line.startswith("data:"):
                continue
            obj = json.loads(line[5:].lstrip())
            if obj.get("type") == "content_block_delta" and obj["delta"]["type"] == "text_delta":
                text += obj["delta"]["text"]
    return text


def _restore_object(g: CorpLlmGuardrail, data: dict[str, Any], response: Any) -> Any:
    """``_apply_reverse_to_response`` on an in-process response object, with the mapping
    the pre-call hands a ticketed request. The ASGI desanitiser only ever passes it the
    JSON a response became on the wire; these object shapes pin the function itself."""
    from corp_llm_gateway.litellm_hook import _apply_reverse_to_response, _response_mapping

    state = g._req_state[CorpLlmGuardrail._ensure_request_id(data)]
    return _apply_reverse_to_response(
        response, _response_mapping(state, include_bare_aliases=g._forward_chatgpt_auth)
    )


class _FakeChatModelResponse:
    """Duck-typed stand-in for litellm's Pydantic Chat Completions
    ``ModelResponse`` (pydantic isn't installed in this venv). Mirrors the
    ``model_dump``/``model_validate``/``model_copy`` trio
    ``_apply_reverse_to_response`` relies on, since litellm hands
    ``post_call_unary`` a real object rather than a dict in some code paths."""

    def __init__(self, choices: list[dict[str, Any]]) -> None:
        self.choices = choices

    def model_dump(self, mode: str = "python", exclude_none: bool = False) -> dict[str, Any]:
        return {"choices": self.choices}

    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> "_FakeChatModelResponse":
        return cls(payload["choices"])

    def model_copy(self, *, update: dict[str, Any], deep: bool = True) -> "_FakeChatModelResponse":
        return _FakeChatModelResponse(update.get("choices", self.choices))


class _FakeChatModelResponseValidateFails(_FakeChatModelResponse):
    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> "_FakeChatModelResponseValidateFails":
        raise ValueError("simulated reconstruction failure")


class _FakeChatModelResponseValidateFailsWithPayloadInMessage(_FakeChatModelResponse):
    """Stands in for a real pydantic ValidationError, whose message embeds
    the offending `input_value` — i.e. the payload that failed to validate,
    which at this call site is the already-DESANITIZED (original-bearing)
    response dict."""

    @classmethod
    def model_validate(
        cls, payload: dict[str, Any]
    ) -> "_FakeChatModelResponseValidateFailsWithPayloadInMessage":
        raise ValueError(f"1 validation error for Foo\n  input_value={payload!r}")


class _RestoredWithReadOnlyHiddenParams:
    """Stands in for a validated object where `_hidden_params` exists (so
    `hasattr` is True) but can't be reassigned — simulates any failure while
    restoring litellm's private response attrs after a successful
    `model_validate`."""

    def __init__(self, choices: list[dict[str, Any]]) -> None:
        self.choices = choices

    @property
    def _hidden_params(self) -> dict[str, Any]:
        return {}


class _FakeChatModelResponseHiddenParamsRestoreFails(_FakeChatModelResponse):
    @classmethod
    def model_validate(cls, payload: dict[str, Any]) -> "_RestoredWithReadOnlyHiddenParams":
        return _RestoredWithReadOnlyHiddenParams(payload["choices"])


def _assistant_tool_call_msg(arguments: str, *, name: str = "save") -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
    }


def _tc_delta_chunk(arguments: str, *, index: int = 0, first: bool = False) -> dict:
    """One OpenAI streaming tool_calls delta chunk. The first fragment carries id/name."""
    fn: dict[str, Any] = {"arguments": arguments}
    tc: dict[str, Any] = {"index": index, "function": fn}
    if first:
        fn["name"] = "f"
        tc["id"] = "c1"
        tc["type"] = "function"
    return {"choices": [{"delta": {"tool_calls": [tc]}}]}


def _assistant_dict_args_msg(arguments: dict | list, *, name: str = "save") -> dict:
    """A tool-call-only assistant message whose function.arguments is ALREADY a
    dict/list (not a JSON string) — a shape some clients/providers emit."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
    }


def _chunk_tool_args(chunk: dict) -> str:
    return "".join(
        tc["function"]["arguments"] for tc in (chunk["choices"][0]["delta"].get("tool_calls") or [])
    )


def _build_guardrail_with_cap(cap: int) -> tuple[CorpLlmGuardrail, ListSink]:
    token_store = InMemoryTokenStore()
    now = datetime.now(UTC)
    token_store.upsert(
        TokenInfo(
            corp_token="tok-1",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    orch = SanitizationOrchestrator(_corp_llm_returning([]), InMemoryMappingStore(), _StaticRules())
    sink = ListSink()
    return (
        CorpLlmGuardrail(
            orch,
            AuthMiddleware(token_store),
            AuditLogger(sink, gateway_version="0.0.1"),
            max_output_tokens_cap=cap,
        ),
        sink,
    )


_STORE_DETAIL = "store-detail-tok-5e2a"


class _UnavailableTokenStore(TokenStore):
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def lookup(self, corp_token: str) -> TokenInfo | None:
        raise self._exc

    async def revoke_user(self, user_id: str) -> int:
        raise NotImplementedError

    async def list_tokens(self, user_id: str | None = None) -> tuple[TokenInfo, ...]:
        raise NotImplementedError


def _asyncpg_error(name: str) -> BaseException:
    asyncpg = pytest.importorskip("asyncpg")
    return getattr(asyncpg.exceptions, name)(_STORE_DETAIL)


class _HangingTokenStore(InMemoryTokenStore):
    """Lookups of ``hung`` tokens never return until ``release`` is set."""

    def __init__(self, *hung: str) -> None:
        super().__init__()
        self.hung = set(hung)
        self.release = asyncio.Event()

    async def lookup(self, corp_token: str) -> TokenInfo | None:
        if corp_token in self.hung:
            await self.release.wait()
        return await super().lookup(corp_token)


def _token(corp_token: str, user_id: str) -> TokenInfo:
    now = datetime.now(UTC)
    return TokenInfo(
        corp_token=corp_token,
        user_id=user_id,
        team_id="t1",
        scopes=("read",),
        issued_at=now,
        expires_at=now + timedelta(days=30),
    )


async def _ticketed_pre_call(
    g: CorpLlmGuardrail, data: dict[str, Any], ticket: Any, *, call_type: str | None = None
) -> None:
    from corp_llm_gateway.route_gate.inflight import _TICKET

    token = _TICKET.set(ticket)
    try:
        await g.pre_call(data, call_type=call_type)
    finally:
        _TICKET.reset(token)


async def _in_context(ticket: Any, call: Any) -> None:
    from corp_llm_gateway.route_gate.inflight import _TICKET

    token = _TICKET.set(ticket)
    try:
        await call
    finally:
        _TICKET.reset(token)
