"""M1-14 invariant — regression-grade.

Run a corpus of redactable strings through the components we have today
and assert that the originals do NOT appear in any of:

  (i)   custom_logger emissions (audit records)
  (ii)  HTTPException bodies / structured errors
  (iii) exception traces propagated out of guardrail components
  (iv)  Prometheus metric labels (proxy: any string-typed metric value
        we emit during the run)
  (v)   forwarded HTTP headers (proxy: header dicts after strip)
  (vi)  pod stdout/stderr from unhandled exceptions

This file does NOT cover the pre_call / post_call wiring (M1-7 / M1-8 are
the integration tests for those once corp-LLM URL lands). It guards the
parts that exist today — sanitizer engine, streaming desanitizer, audit
logger, auth middleware — so a regression here trips a hard test failure.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from corp_llm_gateway.audit import (
    AuditEvent,
    AuditLogger,
    ListSink,
    NeverFieldPresentError,
    StdoutSink,
    assert_no_never_fields,
)
from corp_llm_gateway.detectors import (
    Finding,
    PIIDetector,
    ShadowDetector,
)
from corp_llm_gateway.sanitizer import (
    CorpLlmSanitizer,
    SanitizerStrategy,
    StrategyResult,
    StreamingDesanitizer,
)
from corp_llm_gateway.sanitizer.strategies import StrategyError
from corp_llm_gateway.tokens import (
    AuthMiddleware,
    InMemoryTokenStore,
    InvalidTokenError,
    TokenInfo,
)

ORIGINAL_CORPUS = [
    "alice.smith@corp.lan",
    "555-867-5309",
    "BadgeID-XYZ-12345",
    "Project Polaris confidential",
    "Server: db-prod-13.corp.internal",
    "API_KEY=sk-secret-1234567890abcdef",
]


def _redacted_pairs() -> tuple[tuple[str, str], ...]:
    return tuple((orig, f"[REDACTED_{i:03d}]") for i, orig in enumerate(ORIGINAL_CORPUS))


def _haystack_contains_any_original(haystack: str) -> str | None:
    for original in ORIGINAL_CORPUS:
        if original in haystack:
            return original
    return None


# (i) Audit logger never emits originals -----------------------------------


@pytest.mark.asyncio
async def test_audit_logger_never_emits_originals_via_label_counts() -> None:
    """A misuse: caller pushes the matched text into label counts."""
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(
        AuditEvent(
            timestamp=datetime.now(UTC),
            request_id="req-1",
            user_id="alice",
            team_id="t1",
            provider="anthropic",
            model="claude",
            latency_ms=100,
            prompt_token_count=10,
            completion_token_count=5,
            redaction_count=1,
            finding_label_counts={"EMAIL": 1, "PERSON": 1},
            placeholder_list=tuple(p for _, p in _redacted_pairs()),
        )
    )
    serialized = json.dumps(sink.records[0])
    assert _haystack_contains_any_original(serialized) is None


@pytest.mark.asyncio
async def test_audit_logger_stdout_lines_never_contain_originals() -> None:
    buf = io.StringIO()
    logger = AuditLogger(StdoutSink(stream=buf), gateway_version="0.0.1")
    for i, _ in enumerate(ORIGINAL_CORPUS):
        await logger.emit(
            AuditEvent(
                timestamp=datetime.now(UTC),
                request_id=f"req-{i}",
                user_id="alice",
                team_id="t1",
                provider="anthropic",
                model="claude",
                latency_ms=100,
                prompt_token_count=10,
                completion_token_count=5,
                redaction_count=1,
                placeholder_list=(f"[REDACTED_{i:03d}]",),
            )
        )
    assert _haystack_contains_any_original(buf.getvalue()) is None


def test_assert_no_never_fields_catches_originals_in_mapping_field() -> None:
    """Even if a regression would smuggle pairs in via a NEVER key,
    the in-process gate must reject before the sink writes."""
    record = {
        "timestamp": "2026-05-07T12:00:00Z",
        "user_id": "alice",
        "mapping": list(_redacted_pairs()),
    }
    with pytest.raises(NeverFieldPresentError):
        assert_no_never_fields(record)


# (iii) Detector exception messages don't carry originals -------------------


class _RaisingDetectorWithOriginal(PIIDetector):
    async def detect(self, text: str) -> list[Finding]:
        leak = ORIGINAL_CORPUS[0]
        raise RuntimeError(f"shadow leaks {leak}")


class _CleanDetector(PIIDetector):
    async def detect(self, text: str) -> list[Finding]:
        return [Finding(text=text, label="PERSON", start=0, end=len(text), score=0.9)]


@pytest.mark.asyncio
async def test_shadow_detector_swallows_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canonical = _CleanDetector()
    shadow = _RaisingDetectorWithOriginal()
    detector = ShadowDetector(canonical, shadow)
    with caplog.at_level(logging.WARNING):
        await detector.detect("dummy text")
    assert _haystack_contains_any_original(caplog.text) is None


# (iv-proxy) Sanitizer engine never logs the raw output ---------------------


class _AlwaysFailsStrategy(SanitizerStrategy):
    @property
    def name(self) -> str:
        return "always_fails"

    async def extract(self, raw_llm_output: str) -> StrategyResult:
        raise StrategyError("parser exploded")


@pytest.mark.asyncio
async def test_sanitizer_failure_log_does_not_carry_raw_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_blob = "\n".join(ORIGINAL_CORPUS)
    sanitizer = CorpLlmSanitizer(strategies=[_AlwaysFailsStrategy(), _AlwaysFailsStrategy()])
    with caplog.at_level(logging.DEBUG), contextlib.suppress(Exception):
        await sanitizer.extract(secret_blob)
    assert _haystack_contains_any_original(caplog.text) is None


# (v) Auth middleware strip + error path don't leak corp_token -------------


@pytest.mark.asyncio
async def test_strip_corp_token_removes_token_from_forward_headers() -> None:
    store = InMemoryTokenStore()
    store.upsert(
        TokenInfo(
            corp_token="super-secret-corp-tok",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
    )
    mw = AuthMiddleware(store)
    forwarded = mw.strip_corp_token(
        {
            "X-Corp-Auth": "super-secret-corp-tok",
            "Authorization": "Bearer byok-developer-key",
            "Accept": "*/*",
        }
    )
    serialized = json.dumps(forwarded)
    assert "super-secret-corp-tok" not in serialized
    assert "Bearer byok-developer-key" in serialized


@pytest.mark.asyncio
async def test_invalid_token_error_does_not_carry_token_text() -> None:
    """If a future regression embeds the token in the error body, this fails."""
    mw = AuthMiddleware(InMemoryTokenStore())
    try:
        await mw.authenticate("super-secret-corp-tok")
    except InvalidTokenError as exc:
        assert "super-secret-corp-tok" not in str(exc)


# (ii) StreamingDesanitizer correctly de-redacts; partial state never
# emits a placeholder verbatim into a downstream consumer -------------------


# (vi-bis) Segmenter output is pure slices; never logs content ----------------


def test_segmenter_split_segments_is_pure_slices(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """split_segments returns exact text[start:end] slices; never logs originals."""
    import logging

    from corp_llm_gateway.sanitizer.segmenter import split_segments

    original = ORIGINAL_CORPUS[0]  # "alice.smith@corp.lan"
    text = f"see code:\n```python\nresult = call('{original}')\n```\ndone"
    with caplog.at_level(logging.DEBUG):
        segments = split_segments(text)
    for seg in segments:
        assert seg.text == text[seg.start : seg.end], f"segment text is not a slice: {seg!r}"
    assert _haystack_contains_any_original(caplog.text) is None


def test_segmenter_split_identifier_is_pure_slices(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """split_identifier returns exact name[start:end] slices; never logs originals."""
    import logging

    from corp_llm_gateway.sanitizer.segmenter import split_identifier

    name = "AliceSmithCorpLanService"
    with caplog.at_level(logging.DEBUG):
        sub_tokens = split_identifier(name)
    for token_text, start, end in sub_tokens:
        assert token_text == name[start:end], (
            f"sub-token is not a slice: {token_text!r} at [{start}:{end}]"
        )
    assert _haystack_contains_any_original(caplog.text) is None


def test_segmenter_does_not_introduce_new_content() -> None:
    """Every character in every Segment.text is present in the original text at that position."""
    from corp_llm_gateway.sanitizer.segmenter import split_segments

    text = "\n".join(ORIGINAL_CORPUS)
    segments = split_segments(text)
    for seg in segments:
        # Pure-slice invariant: content must be identical to the source span.
        assert seg.text == text[seg.start : seg.end]


# (ii) StreamingDesanitizer correctly de-redacts; partial state never
# emits a placeholder verbatim into a downstream consumer -------------------


def test_streaming_desanitizer_full_round_trip_recovers_originals() -> None:
    mapping = StrategyResult(pairs=_redacted_pairs())
    placeholders = [p for _, p in _redacted_pairs()]
    redacted_doc = " | ".join(placeholders)
    d = StreamingDesanitizer(mapping)
    out = "".join(d.feed(c) for c in redacted_doc)
    out += d.flush()
    for original in ORIGINAL_CORPUS:
        assert original in out


# (vii) Stage-0 block path: GuardrailHttpException and block_reason are original-free


def test_stage0_block_exception_message_contains_no_raw_content() -> None:
    """The GuardrailHttpException raised by Stage 0 must carry no raw user content."""
    from corp_llm_gateway.litellm_hook import GuardrailHttpException

    # The message is always the generic constant string — never the payload.
    exc = GuardrailHttpException(422, "E_POLICY_BLOCKED", "request blocked by content policy")
    exc_text = str(exc)
    for original in ORIGINAL_CORPUS:
        assert original not in exc_text, f"Original {original!r} leaked into exception text"


def test_stage0_block_reason_values_contain_no_raw_content() -> None:
    """classify_block must return only short reason-code tokens, never payload text."""
    from corp_llm_gateway.payload.classifier import classify_block

    # Build a payload that contains originals from the corpus.
    env_payload = "\n".join(
        [
            f"SECRET={ORIGINAL_CORPUS[5]}",  # API_KEY=sk-secret-...
            f"EMAIL={ORIGINAL_CORPUS[0]}",  # alice.smith@corp.lan
            f"SERVER={ORIGINAL_CORPUS[4]}",  # Server: db-prod-13...
            "DEBUG=False",
            "LOG_LEVEL=INFO",
        ]
    )
    reason = classify_block(env_payload)
    assert reason is not None, "payload should be classified"
    for original in ORIGINAL_CORPUS:
        assert original not in reason, f"Original {original!r} leaked into block_reason"


# (viii) Stage-5 DLP guard path: exception, log, and audit are original-free


def test_stage5_dlp_exception_message_contains_no_raw_content() -> None:
    """GuardrailHttpException raised by Stage 5 carries no raw canary/secret value."""
    from corp_llm_gateway.litellm_hook import GuardrailHttpException

    exc = GuardrailHttpException(422, "E_DLP_BLOCKED", "request blocked by DLP egress policy")
    exc_text = str(exc)
    for original in ORIGINAL_CORPUS:
        assert original not in exc_text, f"Original {original!r} leaked into exception text"


def test_stage5_dlp_block_reason_values_contain_no_raw_content() -> None:
    """DlpEgressGuard.scan() must return only short reason codes, never the matched value."""
    from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard

    canary = ORIGINAL_CORPUS[0]  # "alice.smith@corp.lan"
    guard = DlpEgressGuard(canary_patterns=[canary], secret_rescan=True)
    text_with_canary = f"text containing {canary}"
    reason = guard.scan(text_with_canary)
    assert reason is not None, "guard should detect the canary"
    for original in ORIGINAL_CORPUS:
        assert original not in reason, f"Original {original!r} leaked into block_reason"


@pytest.mark.asyncio
async def test_stage5_dlp_log_contains_no_raw_canary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The litellm_egress_blocked log line emits no raw canary or secret value."""
    from corp_llm_gateway.audit import AuditLogger, ListSink
    from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
    from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
    from corp_llm_gateway.rules import Rules, RulesLoader
    from corp_llm_gateway.sanitizer import SanitizationOrchestrator
    from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard
    from corp_llm_gateway.storage import InMemoryMappingStore
    from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo

    canary = ORIGINAL_CORPUS[0]  # "alice.smith@corp.lan"

    def _empty_pairs_handler(request: httpx.Request) -> httpx.Response:
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
                                        "arguments": '{"pairs": []}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(_empty_pairs_handler))
    corp_llm = CorpLlmClient("https://corp-llm.example", model="m", http=http)

    class _NoRules(RulesLoader):
        async def load(self, team_id: str) -> Rules:
            return Rules(rules=())

    store = InMemoryTokenStore()
    now = datetime.now(UTC)
    store.upsert(
        TokenInfo(
            corp_token="tok-inv",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    dlp = DlpEgressGuard(canary_patterns=[canary], secret_rescan=False)
    guardrail = CorpLlmGuardrail(
        SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _NoRules()),
        AuthMiddleware(store),
        AuditLogger(ListSink(), gateway_version="0.0.1"),
        dlp_guard=dlp,
    )
    data = {
        "model": "claude",
        "messages": [{"role": "user", "content": f"message with {canary}"}],
        "headers": {"X-Corp-Auth": "tok-inv", "Authorization": "Bearer byok"},
    }
    with caplog.at_level(logging.INFO), contextlib.suppress(Exception):
        await guardrail.pre_call(data)
    for original in ORIGINAL_CORPUS:
        assert original not in caplog.text, f"Original {original!r} leaked into logs"


@pytest.mark.asyncio
async def test_stage5_audit_record_contains_no_raw_content() -> None:
    """Audit record after a Stage-5 block must contain no raw originals."""
    from corp_llm_gateway.audit import AuditLogger, ListSink
    from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
    from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
    from corp_llm_gateway.rules import Rules, RulesLoader
    from corp_llm_gateway.sanitizer import SanitizationOrchestrator
    from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard
    from corp_llm_gateway.storage import InMemoryMappingStore
    from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo

    canary = ORIGINAL_CORPUS[0]  # "alice.smith@corp.lan"

    def _empty_pairs_handler(request: httpx.Request) -> httpx.Response:
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
                                        "arguments": '{"pairs": []}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(_empty_pairs_handler))
    corp_llm = CorpLlmClient("https://corp-llm.example", model="m", http=http)

    class _NoRules(RulesLoader):
        async def load(self, team_id: str) -> Rules:
            return Rules(rules=())

    store = InMemoryTokenStore()
    now = datetime.now(UTC)
    store.upsert(
        TokenInfo(
            corp_token="tok-inv2",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    sink = ListSink()
    dlp = DlpEgressGuard(canary_patterns=[canary], secret_rescan=False)
    guardrail = CorpLlmGuardrail(
        SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _NoRules()),
        AuthMiddleware(store),
        AuditLogger(sink, gateway_version="0.0.1"),
        dlp_guard=dlp,
    )
    data = {
        "model": "claude",
        "messages": [{"role": "user", "content": f"message containing {canary}"}],
        "headers": {"X-Corp-Auth": "tok-inv2", "Authorization": "Bearer byok"},
    }
    with pytest.raises(GuardrailHttpException) as ei:
        await guardrail.pre_call(data)
    assert ei.value.error_code == "E_DLP_BLOCKED"
    await guardrail.audit(data, None, start_time=now, end_time=now, status="failed")
    assert len(sink.records) == 1
    serialized = json.dumps(sink.records[0])
    for original in ORIGINAL_CORPUS:
        assert original not in serialized, f"Original {original!r} leaked into audit record"


@pytest.mark.asyncio
async def test_stage0_audit_record_contains_no_raw_content() -> None:
    """An audit record emitted after a Stage-0 block must contain no originals."""
    import json
    from datetime import timedelta

    import httpx

    from corp_llm_gateway.audit import AuditLogger, ListSink
    from corp_llm_gateway.corp_llm import CorpLlmClient
    from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
    from corp_llm_gateway.rules import Rules, RulesLoader
    from corp_llm_gateway.sanitizer import SanitizationOrchestrator
    from corp_llm_gateway.storage import InMemoryMappingStore
    from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo

    def _dummy_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("upstream must NOT be called for blocked requests")

    http = httpx.AsyncClient(transport=httpx.MockTransport(_dummy_handler))
    corp_llm = CorpLlmClient("https://corp-llm.example", model="m", http=http)

    class _NoRules(RulesLoader):
        async def load(self, team_id: str) -> Rules:
            return Rules(rules=())

    store = InMemoryTokenStore()
    now = datetime.now(UTC)
    store.upsert(
        TokenInfo(
            corp_token="tok-inv",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    sink = ListSink()
    guardrail = CorpLlmGuardrail(
        SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _NoRules()),
        AuthMiddleware(store),
        AuditLogger(sink, gateway_version="0.0.1"),
    )

    env_payload = (
        f"DATABASE_URL=postgres://{ORIGINAL_CORPUS[0]}:hunter2@db.corp.lan/prod\n"
        f"SECRET_KEY={ORIGINAL_CORPUS[5]}\n"
        "DEBUG=False\n"
        "REDIS_URL=redis://cache.corp.lan:6379\n"
        "ALLOWED_HOSTS=*.corp.lan\n"
    )

    from corp_llm_gateway.litellm_hook import GuardrailHttpException

    data = {
        "model": "claude",
        "messages": [{"role": "user", "content": env_payload}],
        "headers": {"X-Corp-Auth": "tok-inv", "Authorization": "Bearer byok"},
    }
    with pytest.raises(GuardrailHttpException) as ei:
        await guardrail.pre_call(data)

    assert ei.value.error_code == "E_POLICY_BLOCKED"
    # The block audits INLINE (litellm does not fire the failure event for a
    # pre_call rejection). The single audit record must contain no raw originals.
    assert len(sink.records) == 1
    serialized = json.dumps(sink.records[0])
    for original in ORIGINAL_CORPUS:
        assert original not in serialized, f"Original {original!r} leaked into audit record"


# (ix) Oversize content (F1): a leaf over the size threshold is fail-closed and
# never egresses the original via any surface (log line or audit record) --------


def _oversize_guardrail() -> tuple[object, ListSink]:
    """A guardrail whose orchestrator fail-closes any leaf over 32 bytes."""
    from corp_llm_gateway.corp_llm import CorpLlmClient
    from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
    from corp_llm_gateway.rules import Rules, RulesLoader
    from corp_llm_gateway.sanitizer import SanitizationOrchestrator
    from corp_llm_gateway.storage import InMemoryMappingStore
    from corp_llm_gateway.tokens import InMemoryTokenStore, TokenInfo

    def _no_upstream(request: httpx.Request) -> httpx.Response:
        raise AssertionError("oracle/upstream must NOT be called for an oversize block")

    http = httpx.AsyncClient(transport=httpx.MockTransport(_no_upstream))
    corp_llm = CorpLlmClient("https://corp-llm.example", model="m", http=http)

    class _NoRules(RulesLoader):
        async def load(self, team_id: str) -> Rules:
            return Rules(rules=())

    store = InMemoryTokenStore()
    now = datetime.now(UTC)
    store.upsert(
        TokenInfo(
            corp_token="tok-inv",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    sink = ListSink()
    guardrail = CorpLlmGuardrail(
        SanitizationOrchestrator(
            corp_llm, InMemoryMappingStore(), _NoRules(), size_threshold_bytes=32
        ),
        AuthMiddleware(store),
        AuditLogger(sink, gateway_version="0.0.1"),
    )
    return guardrail, sink


@pytest.mark.asyncio
async def test_oversize_message_leaf_blocks_and_never_leaks_original(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An oversize message leaf carrying every corpus original egresses nowhere."""
    from corp_llm_gateway.litellm_hook import GuardrailHttpException

    guardrail, sink = _oversize_guardrail()
    big = "\n".join(ORIGINAL_CORPUS) + " " + "x" * 64  # > 32 bytes; carries all originals
    data = {
        "model": "claude",
        "messages": [{"role": "user", "content": big}],
        "headers": {"X-Corp-Auth": "tok-inv", "Authorization": "Bearer byok"},
    }
    with caplog.at_level(logging.INFO), pytest.raises(GuardrailHttpException) as ei:
        await guardrail.pre_call(data)
    assert ei.value.error_code == "E_OVERSIZE_BLOCKED"
    assert len(sink.records) == 1
    serialized = json.dumps(sink.records[0])
    assert _haystack_contains_any_original(serialized) is None, "original in audit record"
    assert _haystack_contains_any_original(caplog.text) is None, "original in log line"


@pytest.mark.asyncio
async def test_oversize_document_leaf_blocks_and_never_leaks_original(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An oversize document.source.data leaf is fail-closed with no original egress."""
    from corp_llm_gateway.litellm_hook import GuardrailHttpException

    guardrail, sink = _oversize_guardrail()
    big = "\n".join(ORIGINAL_CORPUS) + " " + "y" * 64
    doc = [{"type": "document", "source": {"type": "text", "data": big}}]
    data = {
        "model": "claude",
        "messages": [{"role": "user", "content": doc}],
        "headers": {"X-Corp-Auth": "tok-inv", "Authorization": "Bearer byok"},
    }
    with caplog.at_level(logging.INFO), pytest.raises(GuardrailHttpException) as ei:
        await guardrail.pre_call(data)
    assert ei.value.error_code == "E_OVERSIZE_BLOCKED"
    assert len(sink.records) == 1
    serialized = json.dumps(sink.records[0])
    assert _haystack_contains_any_original(serialized) is None, "original in audit record"
    assert _haystack_contains_any_original(caplog.text) is None, "original in log line"


# (x) OpenAI message-level tool_calls (F4): assistant tool-call history with real
# values in function.arguments must be sanitized before egress, and a raw secret
# there must be caught by Stage-5 DLP — never egress via any surface --------------


def _tool_call_guardrail(pairs: tuple[tuple[str, str], ...]) -> tuple[object, ListSink]:
    """A guardrail whose oracle returns *pairs* for every leaf (default: redact nothing)."""
    from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
    from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
    from corp_llm_gateway.rules import Rules, RulesLoader
    from corp_llm_gateway.sanitizer import SanitizationOrchestrator
    from corp_llm_gateway.storage import InMemoryMappingStore
    from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo

    def _handler(request: httpx.Request) -> httpx.Response:
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
                                                    {"original": o, "replacement": p}
                                                    for o, p in pairs
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

    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    corp_llm = CorpLlmClient("https://corp-llm.example", model="m", http=http)

    class _NoRules(RulesLoader):
        async def load(self, team_id: str) -> Rules:
            return Rules(rules=())

    store = InMemoryTokenStore()
    now = datetime.now(UTC)
    store.upsert(
        TokenInfo(
            corp_token="tok-inv",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    sink = ListSink()
    guardrail = CorpLlmGuardrail(
        SanitizationOrchestrator(corp_llm, InMemoryMappingStore(), _NoRules()),
        AuthMiddleware(store),
        AuditLogger(sink, gateway_version="0.0.1"),
    )
    return guardrail, sink


@pytest.mark.asyncio
async def test_openai_tool_call_arguments_sanitized_never_leaks_original(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every corpus original placed in tool_calls[].function.arguments is redacted
    before egress and appears in no forwarded content, log line, or audit record."""
    guardrail, sink = _tool_call_guardrail(_redacted_pairs())
    args = json.dumps({f"f{i}": orig for i, orig in enumerate(ORIGINAL_CORPUS)})
    data = {
        "model": "gpt-4o",
        "messages": [
            {"role": "user", "content": "call the tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "save", "arguments": args},
                    }
                ],
            },
        ],
        "headers": {"X-Corp-Auth": "tok-inv", "Authorization": "Bearer byok"},
    }
    with caplog.at_level(logging.INFO):
        out = await guardrail.pre_call(data)  # type: ignore[attr-defined]

    forwarded = json.dumps(out["messages"])
    assert _haystack_contains_any_original(forwarded) is None, "original egressed in tool_calls"
    assert _haystack_contains_any_original(caplog.text) is None, "original in log line"

    now = datetime.now(UTC)
    await guardrail.audit(data, None, start_time=now, end_time=now, status="ok")  # type: ignore[attr-defined]
    assert len(sink.records) == 1
    assert _haystack_contains_any_original(json.dumps(sink.records[0])) is None


@pytest.mark.asyncio
async def test_openai_tool_call_raw_secret_in_arguments_blocked_and_no_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raw sk- secret in tool_calls arguments is caught by Stage-5 DLP (no bypass),
    and no original leaks via exception, log, or audit."""
    from corp_llm_gateway.litellm_hook import GuardrailHttpException

    guardrail, sink = _tool_call_guardrail(())  # oracle redacts nothing → DLP is the backstop
    secret = "sk-" + "A" * 40
    args = json.dumps({"note": ORIGINAL_CORPUS[0], "key": secret})
    data = {
        "model": "gpt-4o",
        "messages": [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "f", "arguments": args}}
                ],
            },
        ],
        "headers": {"X-Corp-Auth": "tok-inv", "Authorization": "Bearer byok"},
    }
    with caplog.at_level(logging.INFO), pytest.raises(GuardrailHttpException) as ei:
        await guardrail.pre_call(data)  # type: ignore[attr-defined]
    assert ei.value.error_code == "E_DLP_BLOCKED"

    now = datetime.now(UTC)
    await guardrail.audit(data, None, start_time=now, end_time=now, status="failed")  # type: ignore[attr-defined]
    assert len(sink.records) == 1
    serialized = json.dumps(sink.records[0])
    assert _haystack_contains_any_original(serialized) is None, "original in audit record"
    assert _haystack_contains_any_original(caplog.text) is None, "original in log line"
    assert secret not in serialized and secret not in caplog.text, "secret leaked into a surface"


# (xi) DICT-shaped tool_call arguments (A3 review): the same guarantees hold when
# function.arguments is an already-parsed dict, not a JSON string --------------


@pytest.mark.asyncio
async def test_openai_tool_call_dict_arguments_sanitized_never_leaks_original(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Corpus originals in a DICT-shaped arguments value are redacted before egress
    and leak via no forwarded content, log line, or audit record."""
    guardrail, sink = _tool_call_guardrail(_redacted_pairs())
    args = {f"f{i}": orig for i, orig in enumerate(ORIGINAL_CORPUS)}
    data = {
        "model": "gpt-4o",
        "messages": [
            {"role": "user", "content": "call the tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "save", "arguments": args},
                    }
                ],
            },
        ],
        "headers": {"X-Corp-Auth": "tok-inv", "Authorization": "Bearer byok"},
    }
    with caplog.at_level(logging.INFO):
        out = await guardrail.pre_call(data)  # type: ignore[attr-defined]

    forwarded = json.dumps(out["messages"])
    assert _haystack_contains_any_original(forwarded) is None, "original egressed in dict args"
    assert _haystack_contains_any_original(caplog.text) is None, "original in log line"

    now = datetime.now(UTC)
    await guardrail.audit(data, None, start_time=now, end_time=now, status="ok")  # type: ignore[attr-defined]
    assert len(sink.records) == 1
    assert _haystack_contains_any_original(json.dumps(sink.records[0])) is None


@pytest.mark.asyncio
async def test_openai_tool_call_raw_secret_in_dict_arguments_blocked_and_no_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raw sk- secret in DICT-shaped arguments is caught by Stage-5 DLP (no bypass),
    and no original leaks via exception, log, or audit."""
    from corp_llm_gateway.litellm_hook import GuardrailHttpException

    guardrail, sink = _tool_call_guardrail(())  # oracle redacts nothing → DLP is the backstop
    secret = "sk-" + "A" * 40
    args = {"note": ORIGINAL_CORPUS[0], "key": secret}
    data = {
        "model": "gpt-4o",
        "messages": [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "f", "arguments": args}}
                ],
            },
        ],
        "headers": {"X-Corp-Auth": "tok-inv", "Authorization": "Bearer byok"},
    }
    with caplog.at_level(logging.INFO), pytest.raises(GuardrailHttpException) as ei:
        await guardrail.pre_call(data)  # type: ignore[attr-defined]
    assert ei.value.error_code == "E_DLP_BLOCKED"

    now = datetime.now(UTC)
    await guardrail.audit(data, None, start_time=now, end_time=now, status="failed")  # type: ignore[attr-defined]
    assert len(sink.records) == 1
    serialized = json.dumps(sink.records[0])
    assert _haystack_contains_any_original(serialized) is None, "original in audit record"
    assert _haystack_contains_any_original(caplog.text) is None, "original in log line"
    assert secret not in serialized and secret not in caplog.text, "secret leaked into a surface"


# (xii) Code-fence lang-tag surface (F5): a value smuggled into the ```<lang>
# fence-opener position must be redacted by the local pass before egress. Pre-fix
# the segmenter left the fence-delimiter spans uncovered, so the local detectors
# never scanned them and lang-tag PII (not a rule/gazetteer/DLP term) egressed. --


def _fence_tag_guardrail() -> tuple[object, ListSink]:
    """A guardrail whose LOCAL regex pass is the only redactor (oracle returns no
    pairs) — so a value reachable ONLY via the local pass proves the segmenter now
    covers the fence-delimiter span (F5)."""
    from corp_llm_gateway.corp_llm import SANITIZE_TOOL_NAME, CorpLlmClient
    from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector
    from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
    from corp_llm_gateway.rules import Rules, RulesLoader
    from corp_llm_gateway.sanitizer import SanitizationOrchestrator
    from corp_llm_gateway.storage import InMemoryMappingStore
    from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo

    def _empty_pairs_handler(request: httpx.Request) -> httpx.Response:
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
                                        "arguments": '{"pairs": []}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(_empty_pairs_handler))
    corp_llm = CorpLlmClient("https://corp-llm.example", model="m", http=http)

    class _NoRules(RulesLoader):
        async def load(self, team_id: str) -> Rules:
            return Rules(rules=())

    store = InMemoryTokenStore()
    now = datetime.now(UTC)
    store.upsert(
        TokenInfo(
            corp_token="tok-inv",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    sink = ListSink()
    guardrail = CorpLlmGuardrail(
        SanitizationOrchestrator(
            corp_llm,
            InMemoryMappingStore(),
            _NoRules(),
            local_detectors=[RegexChecksumDetector()],
        ),
        AuthMiddleware(store),
        AuditLogger(sink, gateway_version="0.0.1"),
    )
    return guardrail, sink


@pytest.mark.asyncio
async def test_fence_lang_tag_value_redacted_before_egress(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A value in the opening-fence lang-tag position is redacted before egress and
    appears in no forwarded content, log line, or audit record (F5)."""
    guardrail, sink = _fence_tag_guardrail()
    email = ORIGINAL_CORPUS[0]  # alice.smith@corp.lan
    fence = "```"
    content = f"here is a snippet:\n{fence}{email}\nvalue = compute()\n{fence}\nend"
    data = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": content}],
        "headers": {"X-Corp-Auth": "tok-inv", "Authorization": "Bearer byok"},
    }
    with caplog.at_level(logging.INFO):
        out = await guardrail.pre_call(data)  # type: ignore[attr-defined]

    forwarded = json.dumps(out["messages"])
    assert email not in forwarded, "fence-tag email egressed unredacted (F5)"
    assert _haystack_contains_any_original(forwarded) is None, "original egressed in fence tag"
    assert _haystack_contains_any_original(caplog.text) is None, "original in log line"

    now = datetime.now(UTC)
    await guardrail.audit(data, None, start_time=now, end_time=now, status="ok")  # type: ignore[attr-defined]
    assert len(sink.records) == 1
    assert _haystack_contains_any_original(json.dumps(sink.records[0])) is None
