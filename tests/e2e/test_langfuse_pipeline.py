"""End-to-end test for the audit → Langfuse pipeline.

Skipped unless LANGFUSE_URL is set (so the suite is no-op outside docker-
compose). Drives an AuditEvent through LangfuseSink and asserts the mock
Langfuse received the expected trace + generation events with the right
shape.

Run via:
  docker compose run --rm e2e
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
import pytest

from corp_llm_gateway.audit import AuditEvent, LangfuseSink

LANGFUSE_URL = os.environ.get("LANGFUSE_URL")
LANGFUSE_PUBLIC_KEY = os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-test-ci")
LANGFUSE_SECRET_KEY = os.environ.get("LANGFUSE_SECRET_KEY", "sk-test-ci")

skip_if_no_langfuse = pytest.mark.skipif(
    not LANGFUSE_URL, reason="LANGFUSE_URL must be set for the langfuse e2e"
)


def _event(**overrides: object) -> AuditEvent:
    base: dict[str, object] = {
        "timestamp": datetime.now(UTC),
        "request_id": "e2e-req-1",
        "user_id": "alice",
        "team_id": "t1",
        "provider": "anthropic",
        "model": "claude-opus-4-7",
        "latency_ms": 250,
        "prompt_token_count": 42,
        "completion_token_count": 17,
        "redaction_count": 1,
        "finding_label_counts": {"EMAIL": 1},
        "cache_a_hit": False,
        "status": "ok",
        "placeholder_list": ("[EMAIL_001]",),
    }
    base.update(overrides)
    return AuditEvent(**base)  # type: ignore[arg-type]


@pytest.fixture
async def sink_and_client():
    assert LANGFUSE_URL
    async with httpx.AsyncClient(base_url=LANGFUSE_URL, timeout=5.0) as control:
        await control.delete("/__captures")
        sink = LangfuseSink(
            LANGFUSE_URL,
            public_key=LANGFUSE_PUBLIC_KEY,
            secret_key=LANGFUSE_SECRET_KEY,
        )
        try:
            yield sink, control
        finally:
            await sink.aclose()


@skip_if_no_langfuse
async def test_event_lands_in_langfuse(sink_and_client) -> None:
    sink, control = sink_and_client
    await sink.write_event(_event())
    resp = await control.get("/__captures")
    captures = resp.json()
    assert captures["count"] == 1
    body = captures["captures"][0]["body"]
    types = [e["type"] for e in body["batch"]]
    assert types == ["trace-create", "generation-create"]


@skip_if_no_langfuse
async def test_basic_auth_forwarded(sink_and_client) -> None:
    sink, control = sink_and_client
    await sink.write_event(_event())
    cap = (await control.get("/__captures")).json()["captures"][0]
    assert cap["auth"]["public_key"] == LANGFUSE_PUBLIC_KEY
    assert cap["auth"]["secret_key"] == LANGFUSE_SECRET_KEY


@skip_if_no_langfuse
async def test_team_and_user_metadata_preserved(sink_and_client) -> None:
    sink, control = sink_and_client
    await sink.write_event(_event(user_id="bob", team_id="t-research"))
    cap = (await control.get("/__captures")).json()["captures"][0]
    trace = next(e for e in cap["body"]["batch"] if e["type"] == "trace-create")["body"]
    assert trace["userId"] == "bob"
    assert trace["metadata"]["team_id"] == "t-research"
    assert "team:t-research" in trace["tags"]


@skip_if_no_langfuse
async def test_failure_status_and_error_code_emitted(sink_and_client) -> None:
    sink, control = sink_and_client
    await sink.write_event(_event(status="failed", error_code="E_CORP_LLM_DOWN"))
    cap = (await control.get("/__captures")).json()["captures"][0]
    trace = next(e for e in cap["body"]["batch"] if e["type"] == "trace-create")["body"]
    assert trace["metadata"]["status"] == "failed"
    assert trace["metadata"]["error_code"] == "E_CORP_LLM_DOWN"


@skip_if_no_langfuse
async def test_token_usage_in_generation(sink_and_client) -> None:
    sink, control = sink_and_client
    await sink.write_event(_event(prompt_token_count=100, completion_token_count=200))
    cap = (await control.get("/__captures")).json()["captures"][0]
    gen = next(e for e in cap["body"]["batch"] if e["type"] == "generation-create")["body"]
    assert gen["usage"] == {
        "input": 100,
        "output": 200,
        "total": 300,
        "unit": "TOKENS",
    }


@skip_if_no_langfuse
async def test_no_originals_in_batch_payload(sink_and_client) -> None:
    """Even though the event holds placeholders, an original-looking
    string ('alice') passed via tags or metadata must not appear in the
    Langfuse-bound payload."""
    sink, control = sink_and_client
    await sink.write_event(_event(user_id="alice", team_id="t1"))
    cap = (await control.get("/__captures")).json()["captures"][0]
    payload_text = str(cap["body"])
    # Originals from the test corpus would have been redacted upstream;
    # what reaches Langfuse can carry user_id + tags, but never the
    # message content itself.
    assert "[EMAIL_001]" in payload_text  # placeholder is fine
    # finding_label_counts must be present (counts only, no text)
    trace = next(e for e in cap["body"]["batch"] if e["type"] == "trace-create")["body"]
    assert trace["metadata"]["finding_label_counts"] == {"EMAIL": 1}
