import io
import json
from datetime import UTC, datetime

import pytest

from corp_llm_gateway.audit import (
    AuditEvent,
    AuditLogger,
    ListSink,
    NEVER_FIELDS,
    NeverFieldPresentError,
    StdoutSink,
    assert_no_never_fields,
)


def _event(**overrides: object) -> AuditEvent:
    base: dict[str, object] = {
        "timestamp": datetime(2026, 5, 7, 12, 0, 0, tzinfo=UTC),
        "request_id": "req-1",
        "user_id": "alice",
        "team_id": "t1",
        "provider": "anthropic",
        "model": "claude-opus-4-7",
        "latency_ms": 1234,
        "prompt_token_count": 100,
        "completion_token_count": 50,
        "redaction_count": 0,
        "finding_label_counts": {},
        "cache_a_hit": False,
        "status": "ok",
    }
    base.update(overrides)
    return AuditEvent(**base)  # type: ignore[arg-type]


# Always-fields presence ----------------------------------------------------


async def test_emits_all_always_fields() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_event())
    rec = sink.records[0]
    expected_always = {
        "timestamp",
        "request_id",
        "user_id",
        "team_id",
        "provider",
        "model",
        "latency_ms",
        "prompt_token_count",
        "completion_token_count",
        "redaction_count",
        "finding_label_counts",
        "cache_a_hit",
        "gateway_version",
        "status",
    }
    assert expected_always.issubset(rec.keys())


async def test_gateway_version_is_logger_version_not_event() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="9.9.9")
    await logger.emit(_event())
    assert sink.records[0]["gateway_version"] == "9.9.9"


# Conditional fields appear only when set ----------------------------------


async def test_conditional_fields_absent_when_unset() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_event())
    rec = sink.records[0]
    for key in (
        "placeholder_list",
        "error_code",
        "corp_llm_latency_ms",
        "pre_pass_latency_ms",
        "audit_buffer_full",
    ):
        assert key not in rec


async def test_placeholder_list_emitted_when_set() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(
        _event(redaction_count=2, placeholder_list=("[NAME_001]", "[EMAIL_002]"))
    )
    assert sink.records[0]["placeholder_list"] == ["[NAME_001]", "[EMAIL_002]"]


async def test_error_code_emitted_when_failed() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_event(status="failed", error_code="E_CORP_LLM_DOWN"))
    rec = sink.records[0]
    assert rec["status"] == "failed"
    assert rec["error_code"] == "E_CORP_LLM_DOWN"


async def test_latency_breakdowns_emitted_when_set() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_event(corp_llm_latency_ms=800, pre_pass_latency_ms=120))
    rec = sink.records[0]
    assert rec["corp_llm_latency_ms"] == 800
    assert rec["pre_pass_latency_ms"] == 120


async def test_audit_buffer_full_emitted_when_set() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_event(audit_buffer_full=True))
    assert sink.records[0]["audit_buffer_full"] is True


# Finding label counts: only counts, never the matched text ----------------


async def test_finding_label_counts_serialized() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_event(finding_label_counts={"EMAIL": 2, "PERSON": 1}))
    assert sink.records[0]["finding_label_counts"] == {"EMAIL": 2, "PERSON": 1}


# NEVER-fields invariant ----------------------------------------------------


def test_never_fields_set_includes_critical_keys() -> None:
    expected = {
        "mapping",
        "mapping_table",
        "original_content",
        "x_corp_auth",
        "corp_token",
        "authorization",
    }
    assert expected.issubset(NEVER_FIELDS)


def test_assert_no_never_fields_passes_clean_record() -> None:
    assert_no_never_fields({"timestamp": "x", "user_id": "alice"})


def test_assert_no_never_fields_rejects_lowercase() -> None:
    with pytest.raises(NeverFieldPresentError):
        assert_no_never_fields({"mapping": []})


def test_assert_no_never_fields_rejects_uppercase() -> None:
    with pytest.raises(NeverFieldPresentError):
        assert_no_never_fields({"AUTHORIZATION": "Bearer x"})


def test_assert_no_never_fields_rejects_mixed_case() -> None:
    with pytest.raises(NeverFieldPresentError):
        assert_no_never_fields({"X-Corp-Auth": "tok"})


# StdoutSink writes valid JSON one line per record -------------------------


async def test_stdout_sink_writes_one_line_json() -> None:
    buf = io.StringIO()
    sink = StdoutSink(stream=buf)
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_event())
    await logger.emit(_event(request_id="req-2"))
    lines = buf.getvalue().splitlines()
    assert len(lines) == 2
    rec1 = json.loads(lines[0])
    rec2 = json.loads(lines[1])
    assert rec1["request_id"] == "req-1"
    assert rec2["request_id"] == "req-2"
