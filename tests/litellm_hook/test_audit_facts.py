"""The audit facts the hook records: the event, distinct-secret counts, finding labels, and
re-entrant audit failures."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from corp_llm_gateway.audit import AuditLogger
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo
from tests.hook_fixtures import (
    _AmbiguousAckSink,
    _build_guardrail,
    _corp_llm_email_per_segment,
    _corp_llm_returning,
    _data_with_token,
    _RaisingSink,
    _RecordingMetrics,
    _StaticRules,
)


async def test_reentrant_audit_failure_does_not_double_count_component_failure() -> None:
    """Major: a DLP block whose own inline `audit()` call itself raises (sink
    outage) must not ALSO record `gateway_failure{component="internal"}` on
    top of the `component="dlp"` failure already recorded for the same
    request — one real failure, one metric, not two."""
    metrics = _RecordingMetrics()
    orch = SanitizationOrchestrator(_corp_llm_returning([]), InMemoryMappingStore(), _StaticRules())
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
    g = CorpLlmGuardrail(
        orch,
        AuthMiddleware(token_store),
        AuditLogger(_RaisingSink(), gateway_version="0.0.1"),
        metrics=metrics,
    )
    raw_key = "sk-" + "a" * 48
    data = _data_with_token("tok-1", content=f"my key is {raw_key}")

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)

    # The sink outage on the DLP branch's own audit() call re-enters the
    # wrapper, which reclassifies the client-visible error — but the
    # component metric must stay attributed to the failure that actually
    # happened (dlp), not doubled with an "internal" for the same request.
    assert ei.value.error_code == "E_INTERNAL"
    assert metrics.failures == ["dlp"]
    # A confirmed-failed emit + the safety net's own retry both call audit()
    # for this request — the latency histogram must still see it only once.
    assert len(metrics.latencies) == 1


async def test_reentrant_audit_ambiguous_delivery_does_not_duplicate_and_keeps_state() -> None:
    """Major: a sink that delivers-then-fails-the-ack must not have the
    safety net retry-emit a second record for the same request. And whatever
    WAS recorded must carry the real block_reason/user_id, not "unknown"/None
    from a state destroyed by popping `_req_state` before emit."""
    metrics = _RecordingMetrics()
    orch = SanitizationOrchestrator(_corp_llm_returning([]), InMemoryMappingStore(), _StaticRules())
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
    sink = _AmbiguousAckSink()
    g = CorpLlmGuardrail(
        orch,
        AuthMiddleware(token_store),
        AuditLogger(sink, gateway_version="0.0.1"),
        metrics=metrics,
    )
    raw_key = "sk-" + "a" * 48
    data = _data_with_token("tok-1", content=f"my key is {raw_key}")

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)

    assert ei.value.error_code == "E_INTERNAL"
    assert metrics.failures == ["dlp"]

    # Exactly one delivered record — the safety net must not retry an
    # ambiguous (possibly-already-delivered) audit write.
    assert len(sink.records) == 1
    record = sink.records[0]
    assert record["user_id"] == "alice"
    assert record["block_reason"] == "dlp:secret_leak"


async def test_audit_emits_full_event_after_pre_and_post() -> None:
    g, sink = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)
    response = {
        "choices": [{"message": {"content": "hello [N1]"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    start = time.time()
    await g.audit(data, response, start_time=start, end_time=start + 0.250, status="ok")

    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec["user_id"] == "alice"
    assert rec["team_id"] == "t1"
    assert rec["status"] == "ok"
    assert rec["redaction_count"] == 1
    assert rec["placeholder_list"] == ["[N1]"]
    assert rec["prompt_token_count"] == 10
    assert rec["completion_token_count"] == 5
    assert rec["latency_ms"] >= 250 and rec["latency_ms"] < 1000


async def test_audit_after_failed_pre_call_uses_unknown_user() -> None:
    """A request that never made it past auth still gets audited — INLINE.

    litellm does NOT fire async_log_failure_event for a pre_call rejection, so the
    auth-failure audit is emitted inline (operators need auth-failure rates). The
    record uses placeholder identity ("unknown") since no state was created.
    """
    g, sink = _build_guardrail()
    data = {"model": "claude", "messages": [], "headers": {}}
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_MISSING_TOKEN"
    # Emitted inline by pre_call — no manual failure-event call needed.
    assert len(sink.records) == 1
    assert sink.records[0]["status"] == "failed"
    assert sink.records[0]["user_id"] == "unknown"
    assert sink.records[0]["error_code"] == "E_MISSING_TOKEN"
    # Idempotency: a follow-up failure event adds no second record.
    start = time.time()
    await g.audit(data, None, start_time=start, end_time=start + 0.05, status="failed")
    assert len(sink.records) == 1


async def test_pre_call_bad_request_audits_inline() -> None:
    """A malformed request (messages not a list) audits inline as E_BAD_REQUEST."""
    g, sink = _build_guardrail()
    data = _data_with_token("tok-1", content="hi")
    data["messages"] = "not-a-list"
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_BAD_REQUEST"
    assert len(sink.records) == 1
    assert sink.records[0]["status"] == "failed"
    assert sink.records[0]["error_code"] == "E_BAD_REQUEST"


async def test_audit_provider_detection_anthropic_claude() -> None:
    g, sink = _build_guardrail()
    data = _data_with_token("tok-1", model="claude-opus-4-7")
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")
    assert sink.records[0]["provider"] == "anthropic"


async def test_audit_provider_detection_openai_default() -> None:
    g, sink = _build_guardrail()
    data = _data_with_token("tok-1", model="gpt-4o")
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")
    assert sink.records[0]["provider"] == "openai"


async def test_audit_same_email_two_segments_redaction_count_one() -> None:
    """Same email in system + message must produce redaction_count==1, one placeholder."""
    g, sink = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content="contact a@corp.example",
        system="a@corp.example is admin",
    )
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")

    rec = sink.records[0]
    assert rec["redaction_count"] == 1
    assert rec["placeholder_list"] == ["[EMAIL_001]"]
    assert rec["finding_label_counts"] == {"EMAIL": 1}


async def test_audit_two_different_emails_redaction_count_two() -> None:
    """Two DIFFERENT emails (system + message) -> redaction_count==2, two placeholders."""
    g, sink = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content="contact b@corp.example",
        system="a@corp.example is admin",
    )
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")

    rec = sink.records[0]
    assert rec["redaction_count"] == 2
    assert rec["finding_label_counts"] == {"EMAIL": 2}
    assert len(rec["placeholder_list"]) == 2
    assert len(set(rec["placeholder_list"])) == 2


async def test_audit_mixed_families_label_counts() -> None:
    """Mixed families: EMAIL + API_KEY -> finding_label_counts has both, redaction_count==2."""
    g, sink = _build_guardrail([("a@x.com", "[EMAIL_001]"), ("SEKRET", "[API_KEY_001]")])
    data = _data_with_token("tok-1", content="mail a@x.com key SEKRET")
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")

    rec = sink.records[0]
    assert rec["finding_label_counts"] == {"EMAIL": 1, "API_KEY": 1}
    assert rec["redaction_count"] == 2


async def test_audit_invariant_sum_equals_redaction_count_equals_placeholder_len() -> None:
    """sum(finding_label_counts.values()) == redaction_count == len(placeholder_list)."""
    g, sink = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1",
        content="contact b@corp.example",
        system="a@corp.example is admin",
    )
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")

    rec = sink.records[0]
    assert sum(rec["finding_label_counts"].values()) == rec["redaction_count"]
    assert rec["redaction_count"] == len(rec["placeholder_list"])


async def test_audit_finding_label_counts_keys_never_contain_originals() -> None:
    """M1-14: finding_label_counts keys must be category labels, never originals.

    Pins that _label_counts uses placeholder families (e.g. 'EMAIL'), not
    the original PII values ('bob@corp.example' or the local-part 'bob').
    """
    g, sink = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token("tok-1", content="mail bob@corp.example")
    await g.pre_call(data)
    start = time.time()
    await g.audit(data, {}, start_time=start, end_time=start, status="ok")

    rec = sink.records[0]
    flc = rec["finding_label_counts"]

    # (a) keys are category labels, not originals
    assert "EMAIL" in flc, f"expected 'EMAIL' key, got {flc!r}"

    # (b) neither the full address nor the local-part appears anywhere in keys or repr
    flc_repr = repr(flc)
    assert "bob@corp.example" not in flc_repr, (
        f"original leaked into finding_label_counts: {flc_repr!r}"
    )
    assert "bob" not in flc_repr, f"local-part leaked into finding_label_counts: {flc_repr!r}"
