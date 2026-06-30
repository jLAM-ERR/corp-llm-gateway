"""Tests for block_reason CONDITIONAL field in AuditEvent / AuditLogger."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from corp_llm_gateway.audit import AuditEvent, AuditLogger, ListSink
from corp_llm_gateway.audit.invariants import NEVER_FIELDS, assert_no_never_fields


def _base_event(**overrides: object) -> AuditEvent:
    base: dict[str, object] = {
        "timestamp": datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC),
        "request_id": "req-block-1",
        "user_id": "alice",
        "team_id": "t1",
        "provider": "anthropic",
        "model": "claude-opus",
        "latency_ms": 0,
        "prompt_token_count": 0,
        "completion_token_count": 0,
        "redaction_count": 0,
        "finding_label_counts": {},
        "cache_a_hit": False,
        "status": "failed",
    }
    base.update(overrides)
    return AuditEvent(**base)  # type: ignore[arg-type]


async def test_block_reason_absent_when_not_set() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_base_event())
    assert "block_reason" not in sink.records[0]


async def test_block_reason_emitted_when_set() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_base_event(error_code="E_POLICY_BLOCKED", block_reason="config:env"))
    rec = sink.records[0]
    assert rec["block_reason"] == "config:env"
    assert rec["error_code"] == "E_POLICY_BLOCKED"


@pytest.mark.parametrize(
    "reason",
    ["config:env", "config:kube", "config:nginx", "config:ini", "log:dump"],
)
async def test_all_block_reason_codes_serialize(reason: str) -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_base_event(error_code="E_POLICY_BLOCKED", block_reason=reason))
    assert sink.records[0]["block_reason"] == reason


def test_block_reason_not_a_never_field() -> None:
    """'block_reason' must NOT be in NEVER_FIELDS (it's a safe reason code)."""
    assert "block_reason" not in NEVER_FIELDS


def test_block_reason_value_passes_never_fields_gate() -> None:
    """A record containing block_reason must survive assert_no_never_fields."""
    record = {
        "request_id": "req-1",
        "status": "failed",
        "error_code": "E_POLICY_BLOCKED",
        "block_reason": "config:env",
    }
    # Must not raise
    assert_no_never_fields(record)


async def test_block_reason_contains_no_raw_content_in_record() -> None:
    """The serialized audit record must contain the short code, not any original text."""
    import json

    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_base_event(error_code="E_POLICY_BLOCKED", block_reason="config:env"))
    serialized = json.dumps(sink.records[0])
    # The reason code is short and carries no user payload
    assert "config:env" in serialized
    # Sanity: no hypothetical raw content leaked via this path
    assert "SECRET" not in serialized
    assert "password" not in serialized
