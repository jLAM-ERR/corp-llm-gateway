"""The M4 fail-policy matrix and the transport-vs-internal error classification: 503 for an
unavailable dependency, an opaque 500 for our own bug."""

import asyncio
import json
import logging
import time
from typing import Any

import httpx
import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME
from corp_llm_gateway.detectors import DualNerDetector
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
from corp_llm_gateway.rules import Gazetteer
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.placeholder import StaleSpanError
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import AuthError, AuthMiddleware, TokenInfo, TokenStore
from tests.hook_fixtures import (
    _STORE_DETAIL,
    _asyncpg_error,
    _build_guardrail,
    _build_guardrail_oracle_disabled,
    _CancellingTokenStore,
    _case_bad_request,
    _case_corp_llm_down,
    _case_dlp_blocked,
    _case_missing_token,
    _case_policy_blocked_ambiguous_shape,
    _case_provider_auth,
    _corp_llm_email_per_segment,
    _corp_llm_returning,
    _corp_llm_unreachable,
    _data_with_token,
    _guardrail_with_ner,
    _HangingTokenStore,
    _RaisingNerEngine,
    _RaisingTokenStore,
    _RecordingMetrics,
    _StaticRules,
    _token,
    _UnavailableTokenStore,
)


async def test_pre_call_backend_db_error_returns_opaque_500(caplog: Any) -> None:
    """F8 repro: with no schema staged, the token store raises a raw DB
    exception. That must never reach the client as exception text — only
    an opaque 500 carrying a stable error_code + request_id."""
    sink = ListSink()
    audit_logger = AuditLogger(sink, gateway_version="0.0.1")
    orch = SanitizationOrchestrator(_corp_llm_returning([]), InMemoryMappingStore(), _StaticRules())
    g = CorpLlmGuardrail(orch, AuthMiddleware(_RaisingTokenStore()), audit_logger)

    with caplog.at_level(logging.INFO), pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(_data_with_token("tok-1"))

    assert ei.value.status_code == 500
    assert ei.value.error_code == "E_INTERNAL"
    exc_text = str(ei.value)
    assert "corp_tokens" not in exc_text
    assert "relation" not in exc_text
    assert "corp_tokens" not in caplog.text

    assert len(sink.records) == 1
    serialized = json.dumps(sink.records[0])
    assert "corp_tokens" not in serialized
    assert sink.records[0]["error_code"] == "E_INTERNAL"


async def test_pre_call_unexpected_error_records_metrics_failure() -> None:
    """F8: an unexpected backend exception must still increment
    gateway_failure{component="internal"} — otherwise a real outage on this
    path is invisible to the runbook's alerting query, and an operator has
    no signal that anything went wrong at all."""
    sink = ListSink()
    audit_logger = AuditLogger(sink, gateway_version="0.0.1")
    orch = SanitizationOrchestrator(_corp_llm_returning([]), InMemoryMappingStore(), _StaticRules())
    metrics = _RecordingMetrics()
    g = CorpLlmGuardrail(orch, AuthMiddleware(_RaisingTokenStore()), audit_logger, metrics=metrics)

    with pytest.raises(GuardrailHttpException):
        await g.pre_call(_data_with_token("tok-1"))

    assert metrics.failures == ["internal"]


async def test_pre_call_wrapper_does_not_swallow_cancelled_error() -> None:
    """The F8 safety-net wrapper's `except Exception` must NOT catch
    `asyncio.CancelledError` — it is a `BaseException`, not an `Exception`, in
    every Python version this repo supports. Catching it would convert a
    cancelled/timed-out request into a fabricated 500 E_INTERNAL and defeat
    task cancellation (a hung request would appear to "complete" instead)."""
    sink = ListSink()
    audit_logger = AuditLogger(sink, gateway_version="0.0.1")
    orch = SanitizationOrchestrator(_corp_llm_returning([]), InMemoryMappingStore(), _StaticRules())
    g = CorpLlmGuardrail(orch, AuthMiddleware(_CancellingTokenStore()), audit_logger)

    with pytest.raises(asyncio.CancelledError):
        await g.pre_call(_data_with_token("tok-1"))

    # The wrapper's except block never ran, so no failure was recorded — a
    # cancellation is not a gateway failure and must not be reported as one.
    assert sink.records == []


async def test_pre_call_non_dict_data_returns_opaque_500() -> None:
    """Minor: `_ensure_request_id` must never raise itself — a non-dict
    `data` must not escape as a raw `AttributeError`, which (fired from
    inside the safety net's own `except` block) would carry the ORIGINAL
    exception via `__context__` (M1-14 surface iii)."""
    g, _sink = _build_guardrail()

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(["not", "a", "dict"])  # type: ignore[arg-type]

    assert ei.value.status_code == 500
    assert ei.value.error_code == "E_INTERNAL"
    assert ei.value.__cause__ is None
    assert ei.value.__context__ is None or ei.value.__suppress_context__


@pytest.mark.parametrize(
    ("case_factory", "expected_status", "expected_error_code"),
    [
        (_case_missing_token, 401, "E_MISSING_TOKEN"),
        (_case_bad_request, 400, "E_BAD_REQUEST"),
        (_case_policy_blocked_ambiguous_shape, 422, "E_POLICY_BLOCKED"),
        (_case_provider_auth, 401, "E_PROVIDER_AUTH"),
        (_case_corp_llm_down, 503, "E_CORP_LLM_DOWN"),
        (_case_dlp_blocked, 422, "E_DLP_BLOCKED"),
    ],
    ids=[
        "missing_token",
        "bad_request",
        "policy_blocked_stage0",
        "provider_auth",
        "corp_llm_down",
        "dlp_blocked_stage5",
    ],
)
async def test_pre_call_wrapper_preserves_named_error_codes(
    case_factory: Any, expected_status: int, expected_error_code: str
) -> None:
    """F8 regression guard: the pre_call safety-net wrapper's
    `except GuardrailHttpException: raise` must pass every deliberately
    raised, already-classified error through UNCHANGED — never flatten it to
    500 E_INTERNAL. NER (both call sites) is pinned separately by
    test_pre_call_ner_required_but_absent_returns_503_not_500 /
    test_pre_call_ner_required_but_absent_on_system_returns_503;
    E_PROVIDER_BLOCKED / E_PROFILE_UNAVAILABLE are pinned in
    tests/sanitizer/test_profile_orchestrator.py (they need a profile-file
    fixture) — both already exercise this same `pre_call` wrapper."""
    g, data = case_factory()
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == expected_status
    assert ei.value.error_code == expected_error_code


async def test_auth_error_message_lookup_falls_back_safely_for_unmapped_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_AUTH_ERROR_MESSAGES.get(error_code, "authentication failed")` must
    degrade to the static default for any AuthError subclass
    `_classify_auth_error` hasn't been taught about yet — never a KeyError,
    never `str(exc)` (which could carry backend-derived detail, the exact
    defect class this lookup table replaced)."""
    import corp_llm_gateway.litellm_hook as hook_module

    class _FutureAuthError(AuthError):
        pass

    class _FutureRaisingTokenStore(TokenStore):
        async def lookup(self, corp_token: str) -> TokenInfo | None:
            raise _FutureAuthError("backend-derived detail that must never reach the client")

        async def revoke_user(self, user_id: str) -> int:
            raise NotImplementedError

        async def list_tokens(self, user_id: str | None = None) -> tuple[TokenInfo, ...]:
            raise NotImplementedError

    monkeypatch.setattr(hook_module, "_classify_auth_error", lambda exc: "E_FUTURE_AUTH_CODE")

    sink = ListSink()
    audit_logger = AuditLogger(sink, gateway_version="0.0.1")
    orch = SanitizationOrchestrator(_corp_llm_returning([]), InMemoryMappingStore(), _StaticRules())
    g = CorpLlmGuardrail(orch, AuthMiddleware(_FutureRaisingTokenStore()), audit_logger)

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(_data_with_token("tok-1"))

    assert ei.value.status_code == 401
    assert ei.value.error_code == "E_FUTURE_AUTH_CODE"
    assert "backend-derived detail" not in str(ei.value)
    assert str(ei.value) == "401 E_FUTURE_AUTH_CODE: authentication failed"


def test_auth_error_messages_keys_match_classify_auth_error_return_values() -> None:
    """Pin `_AUTH_ERROR_MESSAGES`'s keys against every value `_classify_auth_error`
    can currently return, so a new `AuthError` subclass added there without a
    matching message here fails this test immediately instead of silently
    degrading to the generic fallback in production."""
    from corp_llm_gateway.litellm_hook import _AUTH_ERROR_MESSAGES, _classify_auth_error
    from corp_llm_gateway.tokens import ExpiredTokenError, InvalidTokenError, RevokedTokenError

    class _UnclassifiedAuthError(AuthError):
        pass

    observed = {
        _classify_auth_error(ExpiredTokenError("x")),
        _classify_auth_error(RevokedTokenError("x")),
        _classify_auth_error(InvalidTokenError("x")),
        _classify_auth_error(_UnclassifiedAuthError("x")),
    }
    assert observed == set(_AUTH_ERROR_MESSAGES.keys())


async def test_pre_call_stale_span_in_message_loop_maps_to_fail_policy_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """apply_spans raising StaleSpanError (e.g. a stale allocator remap) inside
    the messages loop must fail closed with a stable error_code + audit record,
    not escape as a generic 500."""
    import corp_llm_gateway.litellm_hook as hook_module

    def _raise_stale(*_args: object, **_kwargs: object) -> str:
        raise StaleSpanError("applied span does not match source text: start=0 end=5")

    monkeypatch.setattr(hook_module, "apply_spans", _raise_stale)

    # Cross-block collision (both blocks handled inside the SAME messages-loop
    # iteration) forces the allocator remap that triggers apply_spans.
    g, sink = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content=[
            {"type": "text", "text": "first a@corp.example"},
            {"type": "text", "text": "second b@corp.example"},
        ],
    )
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_SPAN_INVALID"
    assert ei.value.status_code == 500

    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec["status"] == "failed"
    assert rec["error_code"] == "E_SPAN_INVALID"
    rec_json = json.dumps(rec)
    assert "a@corp.example" not in rec_json
    assert "b@corp.example" not in rec_json


async def test_pre_call_stale_span_in_prompt_field_maps_to_fail_policy_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same fail-closed mapping via `_sanitize_prompt_field` (the ``system``/
    ``instructions`` path), triggered by the cross-segment collision remap."""
    import corp_llm_gateway.litellm_hook as hook_module

    def _raise_stale(*_args: object, **_kwargs: object) -> str:
        raise StaleSpanError("applied span does not match source text: start=0 end=5")

    monkeypatch.setattr(hook_module, "apply_spans", _raise_stale)

    g, sink = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content="contact customer b@corp.example",
        system="admin is a@corp.example",
    )
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_SPAN_INVALID"
    assert ei.value.status_code == 500

    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec["status"] == "failed"
    assert rec["error_code"] == "E_SPAN_INVALID"
    rec_json = json.dumps(rec)
    assert "a@corp.example" not in rec_json
    assert "b@corp.example" not in rec_json


async def test_pre_call_corp_llm_down_fails_closed_503() -> None:
    """Fail-policy matrix (M4): a corp-LLM sanitization failure must fail
    CLOSED with 503 E_CORP_LLM_DOWN — not leak as a generic 500.

    Regression for the field incident where a 30s corp-LLM timeout
    surfaced to Claude Code as ``500 {"message":"corp-llm transport
    error: "}`` (empty — httpx timeouts stringify to '') because
    pre_call let the raw CorpLlmHttpError escape.
    """
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    data = _data_with_token("tok-1", content="hello alice")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 503
    assert ei.value.error_code == "E_CORP_LLM_DOWN"


async def test_pre_call_corp_llm_down_does_not_forward_content() -> None:
    """Belt-and-braces on the fail-closed posture: when sanitization
    can't run, the request must be rejected — never forwarded with the
    original (un-sanitized) content."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    data = _data_with_token("tok-1", content="my secret is hunter2")
    with pytest.raises(GuardrailHttpException):
        await g.pre_call(data)
    # The raise short-circuits the upstream call; the original content
    # was never handed back as a sanitized payload.
    assert data["messages"][0]["content"] == "my secret is hunter2"


async def test_pre_call_oracle_disabled_gazetteer_hit_sanitizes_without_oracle() -> None:
    """Contrast with test_pre_call_corp_llm_down_fails_closed_503: with the oracle
    switched off (no corp-LLM client at all — not merely unreachable), a
    gazetteer-hit-style request must still sanitize successfully. It must NOT
    raise 503 E_CORP_LLM_DOWN, because no oracle call is ever attempted."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    g, _ = _build_guardrail_oracle_disabled(gaz)
    data = _data_with_token("tok-1", content="We are working on Project Polaris this sprint.")

    result = await g.pre_call(data)

    assert "Project Polaris" not in result["messages"][0]["content"]


async def test_pre_call_ner_required_but_absent_returns_503_not_500() -> None:
    """F2 fail-closed (M4): when CORP_LLM_REQUIRE_NER is on and a configured NER
    engine's model is absent, pre_call must map the NerUnavailableError to a
    503 E_NER_UNAVAILABLE — NOT let it escape as a generic 500."""
    dual = DualNerDetector(require_ner=True, engines=[_RaisingNerEngine(), _RaisingNerEngine()])
    g = _guardrail_with_ner(dual)
    data = _data_with_token("tok-1", content="ping John Smith")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 503
    assert ei.value.error_code == "E_NER_UNAVAILABLE"


async def test_pre_call_ner_required_but_absent_on_system_returns_503() -> None:
    """Same fail-closed path on the system field (empty message → system scan)."""
    dual = DualNerDetector(require_ner=True, engines=[_RaisingNerEngine(), _RaisingNerEngine()])
    g = _guardrail_with_ner(dual)
    data = _data_with_token("tok-1", content="", system="owner John Smith")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 503
    assert ei.value.error_code == "E_NER_UNAVAILABLE"


async def test_pre_call_ner_required_but_absent_does_not_forward_content() -> None:
    dual = DualNerDetector(require_ner=True, engines=[_RaisingNerEngine(), _RaisingNerEngine()])
    g = _guardrail_with_ner(dual)
    data = _data_with_token("tok-1", content="ping John Smith")
    with pytest.raises(GuardrailHttpException):
        await g.pre_call(data)
    # Fail-closed: the request is rejected, never forwarded with the original.
    assert data["messages"][0]["content"] == "ping John Smith"


async def test_pre_call_ner_not_required_stays_on_dev_graceful_path() -> None:
    """require-ner OFF: absent NER degrades to [] (the documented F2 fail-open,
    intentional only for dev / Python 3.14). Request proceeds — no 503."""
    dual = DualNerDetector(require_ner=False, engines=[_RaisingNerEngine(), _RaisingNerEngine()])
    g = _guardrail_with_ner(dual)
    data = _data_with_token("tok-1", content="ping John Smith")
    result = await g.pre_call(data)  # no exception
    # No rule/regex/gazetteer hit and NER degraded → content unchanged (egresses).
    assert result["messages"][0]["content"] == "ping John Smith"


async def test_pre_call_corp_llm_down_on_system_fails_closed_503(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fail-closed (M4): corp-LLM error on system field also raises 503 E_CORP_LLM_DOWN."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    data = _data_with_token("tok-1", content="hello", system="SecretEnv=/prod")

    with caplog.at_level(logging.WARNING), pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)

    assert ei.value.status_code == 503
    assert ei.value.error_code == "E_CORP_LLM_DOWN"
    # Verify the failure was logged.
    assert "litellm_pre_call_corp_llm_failed" in caplog.text


async def test_corp_llm_fails_on_second_segment_fails_closed_503() -> None:
    """M4 fail-closed: if corp-LLM succeeds on segment 1 then dies on segment 2,
    pre_call must raise GuardrailHttpException 503 E_CORP_LLM_DOWN.

    Partially-sanitized content must never egress; the request is rejected
    before any mutated data reaches the upstream LLM.
    """
    call_count = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First segment succeeds.
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
                                                        {
                                                            "original": "a@corp.example",
                                                            "replacement": "[EMAIL_001]",
                                                        }
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
        # Second segment times out.
        raise httpx.ConnectTimeout("", request=request)

    from corp_llm_gateway.corp_llm import CorpLlmClient

    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    flaky_corp_llm = CorpLlmClient("https://corp-llm.example", model="m", http=http)

    g, _ = _build_guardrail(corp_llm=flaky_corp_llm)
    # Two distinct emails in two segments: message + system.
    data = _data_with_token(
        "tok-1",
        content="message from a@corp.example",
        system="system has b@corp.example",
    )

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)

    assert ei.value.status_code == 503, f"expected 503, got {ei.value.status_code}"
    assert ei.value.error_code == "E_CORP_LLM_DOWN", (
        f"expected E_CORP_LLM_DOWN, got {ei.value.error_code!r}"
    )


@pytest.mark.parametrize(
    "make_exc",
    [
        lambda: TimeoutError(),  # the pool's acquire past its timeout
        lambda: OSError(_STORE_DETAIL),
        lambda: ConnectionResetError(_STORE_DETAIL),
        lambda: _asyncpg_error("PostgresConnectionError"),
        lambda: _asyncpg_error("ConnectionDoesNotExistError"),
        lambda: _asyncpg_error("InterfaceError"),
        lambda: _asyncpg_error("AdminShutdownError"),
    ],
    ids=[
        "acquire-timeout",
        "oserror",
        "reset",
        "PostgresConnectionError",
        "ConnectionDoesNotExistError",
        "InterfaceError",
        "AdminShutdownError",
    ],
)
async def test_an_unavailable_token_store_is_503_not_500(make_exc: Any, caplog: Any) -> None:
    exc = make_exc()
    sink = ListSink()
    metrics = _RecordingMetrics()
    orch = SanitizationOrchestrator(_corp_llm_returning([]), InMemoryMappingStore(), _StaticRules())
    g = CorpLlmGuardrail(
        orch,
        AuthMiddleware(_UnavailableTokenStore(exc)),
        AuditLogger(sink, gateway_version="0.0.1"),
        metrics=metrics,
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(_data_with_token("tok-secret-7c1f"))

    assert (ei.value.status_code, ei.value.error_code) == (503, "E_STORE_UNAVAILABLE")
    assert ei.value.__cause__ is None
    assert _STORE_DETAIL not in str(ei.value)
    assert type(exc).__name__ in caplog.text
    assert _STORE_DETAIL not in caplog.text
    assert "tok-secret-7c1f" not in caplog.text
    assert len(sink.records) == 1
    assert sink.records[0]["status"] == "failed"
    assert sink.records[0]["error_code"] == "E_STORE_UNAVAILABLE"
    assert _STORE_DETAIL not in json.dumps(sink.records[0])
    assert metrics.failures == ["token_store"]


async def test_a_saturated_token_store_pool_is_503_within_the_acquire_bound() -> None:
    from tests.postgres_support import pg_dsn, require_asyncpg, skip_or_fail

    require_asyncpg()
    from corp_llm_gateway.tokens.postgres_store import _ACQUIRE_TIMEOUT_S, PostgresTokenStore

    store = PostgresTokenStore(pg_dsn(), pool_max_size=1)
    try:
        pool = await store._get_pool()
    except Exception as exc:
        await store.close()
        skip_or_fail(f"Postgres unreachable: {type(exc).__name__}")
    sink = ListSink()
    orch = SanitizationOrchestrator(_corp_llm_returning([]), InMemoryMappingStore(), _StaticRules())
    g = CorpLlmGuardrail(orch, AuthMiddleware(store), AuditLogger(sink, gateway_version="0.0.1"))
    try:
        async with pool.acquire():
            loop = asyncio.get_running_loop()
            start = loop.time()
            with pytest.raises(GuardrailHttpException) as ei:
                await asyncio.wait_for(g.pre_call(_data_with_token("tok-1")), timeout=15)
            elapsed = loop.time() - start
    finally:
        await store.close()

    assert (ei.value.status_code, ei.value.error_code) == (503, "E_STORE_UNAVAILABLE")
    assert elapsed < _ACQUIRE_TIMEOUT_S + 1.0


async def test_a_hung_token_lookup_is_503_at_the_auth_bound_and_spares_other_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from corp_llm_gateway import litellm_hook

    bound_s = 0.3
    monkeypatch.setattr(litellm_hook, "AUTH_LOOKUP_BOUND_S", bound_s)
    store = _HangingTokenStore("tok-stuck")
    store.upsert(_token("tok-stuck", "alice"))
    store.upsert(_token("tok-free", "bob"))
    sink = ListSink()
    orch = SanitizationOrchestrator(_corp_llm_returning([]), InMemoryMappingStore(), _StaticRules())
    g = CorpLlmGuardrail(orch, AuthMiddleware(store), AuditLogger(sink, gateway_version="0.0.1"))
    loop = asyncio.get_running_loop()
    try:
        start = loop.time()
        stuck = asyncio.create_task(g.pre_call(_data_with_token("tok-stuck")))
        await asyncio.sleep(0)
        out = await asyncio.wait_for(g.pre_call(_data_with_token("tok-free")), timeout=1)
        assert "X-Corp-Auth" not in out["headers"]
        assert not stuck.done()

        with pytest.raises(GuardrailHttpException) as ei:
            await asyncio.wait_for(stuck, timeout=5)
        assert (ei.value.status_code, ei.value.error_code) == (503, "E_STORE_UNAVAILABLE")
        assert loop.time() - start < bound_s + 0.5
    finally:
        store.release.set()


async def test_a_token_store_stalled_mid_query_is_503_and_its_connection_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.postgres_support import pg_dsn, require_asyncpg, skip_or_fail
    from tests.stalling_proxy import StallingProxy

    require_asyncpg()
    from corp_llm_gateway import litellm_hook
    from corp_llm_gateway.pg_session import CANCEL_BUDGET_S
    from corp_llm_gateway.tokens import postgres_store

    lookup_s = 1.0
    monkeypatch.setattr(postgres_store, "LOOKUP_TIMEOUT_S", lookup_s)
    monkeypatch.setattr(litellm_hook, "AUTH_LOOKUP_BOUND_S", lookup_s + 1.0)
    proxy = StallingProxy(pg_dsn())
    store = postgres_store.PostgresTokenStore(await proxy.start(), pool_max_size=1)
    corp_token = f"pg-itest-stall-{time.monotonic_ns()}"
    reached = False
    try:
        try:
            await store.init_schema()
        except Exception as exc:
            skip_or_fail(f"Postgres unreachable: {type(exc).__name__}")
        reached = True
        await store.upsert(_token(corp_token, "pg-test-stall"))
        pool = await store._get_pool()
        assert pool.get_size() == 1
        sink = ListSink()
        orch = SanitizationOrchestrator(
            _corp_llm_returning([]), InMemoryMappingStore(), _StaticRules()
        )
        g = CorpLlmGuardrail(
            orch, AuthMiddleware(store), AuditLogger(sink, gateway_version="0.0.1")
        )

        proxy.stall()
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(GuardrailHttpException) as ei:
            await asyncio.wait_for(g.pre_call(_data_with_token(corp_token)), timeout=15)
        elapsed = loop.time() - start

        assert (ei.value.status_code, ei.value.error_code) == (503, "E_STORE_UNAVAILABLE")
        assert elapsed < lookup_s + CANCEL_BUDGET_S + 0.5
        # The stalled connection was terminated, not handed back to the pool.
        assert pool.get_size() == 0
        proxy.resume()
        assert await asyncio.wait_for(store.lookup(corp_token), timeout=5) is not None
        assert pool.get_size() == 1
    finally:
        proxy.resume()
        try:
            if reached:
                pool = await store._get_pool()
                async with pool.acquire() as conn:
                    await conn.execute("DELETE FROM corp_tokens WHERE corp_token = $1", corp_token)
        finally:
            await store.close()
            await proxy.close()
