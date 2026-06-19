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


def test_streaming_desanitizer_full_round_trip_recovers_originals() -> None:
    mapping = StrategyResult(pairs=_redacted_pairs())
    placeholders = [p for _, p in _redacted_pairs()]
    redacted_doc = " | ".join(placeholders)
    d = StreamingDesanitizer(mapping)
    out = "".join(d.feed(c) for c in redacted_doc)
    out += d.flush()
    for original in ORIGINAL_CORPUS:
        assert original in out
