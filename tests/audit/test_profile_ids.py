"""Tests for the profile_ids / jurisdiction CONDITIONAL audit fields (D7).

profile_ids records WHICH policy profile(s) / jurisdiction resolved for a
request. It is METADATA (policy identity), never mapping/original/credential
content, so it must pass the NEVER-fields gate and flow to the sinks.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from corp_llm_gateway.audit import AuditEvent, AuditLogger, ListSink
from corp_llm_gateway.audit.invariants import NEVER_FIELDS, assert_no_never_fields

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIGMAP = _REPO_ROOT / "helm" / "corp-llm-gateway" / "templates" / "configmap.yaml"
_CHART_DIR = _REPO_ROOT / "helm" / "corp-llm-gateway"


def _base_event(**overrides: object) -> AuditEvent:
    base: dict[str, object] = {
        "timestamp": datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC),
        "request_id": "req-profile-1",
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
        "status": "ok",
    }
    base.update(overrides)
    return AuditEvent(**base)  # type: ignore[arg-type]


# AuditEvent carries the fields ---------------------------------------------


def test_event_defaults_are_empty() -> None:
    event = _base_event()
    assert event.profile_ids == ()
    assert event.jurisdiction is None


def test_event_carries_profile_ids_and_jurisdiction() -> None:
    event = _base_event(profile_ids=("core", "ru-152fz"), jurisdiction="ru")
    assert event.profile_ids == ("core", "ru-152fz")
    assert event.jurisdiction == "ru"


# Conditional serialization: present when set, omitted when not -------------


async def test_profile_ids_absent_when_empty() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_base_event())
    rec = sink.records[0]
    assert "profile_ids" not in rec
    assert "jurisdiction" not in rec


async def test_profile_ids_emitted_when_set() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_base_event(profile_ids=("core", "division-x")))
    rec = sink.records[0]
    assert rec["profile_ids"] == ["core", "division-x"]


async def test_jurisdiction_emitted_when_set() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_base_event(profile_ids=("ru-152fz",), jurisdiction="ru"))
    rec = sink.records[0]
    assert rec["profile_ids"] == ["ru-152fz"]
    assert rec["jurisdiction"] == "ru"


async def test_jurisdiction_absent_when_none_even_with_profiles() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_base_event(profile_ids=("core",)))
    rec = sink.records[0]
    assert rec["profile_ids"] == ["core"]
    assert "jurisdiction" not in rec


# NEVER-gate safety: profile_ids is metadata, not a NEVER field ------------


def test_profile_ids_not_a_never_field() -> None:
    assert "profile_ids" not in NEVER_FIELDS
    assert "jurisdiction" not in NEVER_FIELDS


def test_profile_ids_value_passes_never_fields_gate() -> None:
    record = {
        "request_id": "req-1",
        "status": "ok",
        "profile_ids": ["core", "ru-152fz"],
        "jurisdiction": "ru",
    }
    # Must not raise — profile_ids is policy identity, not content.
    assert_no_never_fields(record)


async def test_emit_with_profile_ids_runs_never_gate_and_succeeds() -> None:
    # emit() calls assert_no_never_fields before writing; a clean record lands.
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_base_event(profile_ids=("core",), jurisdiction="ru"))
    assert sink.records[0]["profile_ids"] == ["core"]


def test_never_gate_still_fires_alongside_profile_ids() -> None:
    # Adding profile_ids must NOT disable the gate for a genuine NEVER field.
    import pytest

    from corp_llm_gateway.audit import NeverFieldPresentError

    with pytest.raises(NeverFieldPresentError):
        assert_no_never_fields({"profile_ids": ["core"], "mapping": []})


async def test_profile_ids_record_carries_no_raw_content() -> None:
    sink = ListSink()
    logger = AuditLogger(sink, gateway_version="0.0.1")
    await logger.emit(_base_event(profile_ids=("core", "ru-152fz"), jurisdiction="ru"))
    serialized = json.dumps(sink.records[0])
    assert "ru-152fz" in serialized
    assert "mapping" not in serialized
    assert "SECRET" not in serialized


# Vector VRL passthrough + NEVER-gate intact -------------------------------


def test_vector_vrl_carries_profile_ids() -> None:
    text = _CONFIGMAP.read_text()
    assert '"profile_ids": .__lf.profile_ids' in text
    assert '"jurisdiction": .__lf.jurisdiction' in text


def test_vector_vrl_never_gate_intact() -> None:
    # Every NEVER field is still dropped by the enforce_audit_schema filter.
    text = _CONFIGMAP.read_text()
    for field in NEVER_FIELDS:
        assert f"!exists(.{field})" in text, f"NEVER-gate lost {field}"


def test_vector_vrl_renders_with_profile_ids() -> None:
    # Prove the template still renders and carries the field where helm exists.
    if shutil.which("helm") is None:
        import pytest

        pytest.skip("helm not on PATH")
    result = subprocess.run(
        ["helm", "template", str(_CHART_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert '"profile_ids": .__lf.profile_ids' in result.stdout
    assert "!exists(.mapping)" in result.stdout
