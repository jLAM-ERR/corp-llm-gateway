"""End-to-end test for the audit → Langfuse pipeline.

Skipped unless LANGFUSE_URL is set (so the suite is no-op outside docker-
compose). Drives an AuditEvent through LangfuseSink and asserts the mock
Langfuse received the expected trace + generation events with the right
shape.

Run via:
  docker compose run --rm e2e
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import httpx
import pytest

from corp_llm_gateway.audit import AuditEvent, LangfuseSink
from corp_llm_gateway.audit.invariants import NEVER_FIELDS, NeverFieldPresentError

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


# Recognisable originals: an email, a card number, a person, a provider key, a
# corp token. None of them may reach Langfuse, whatever field carries them in.
ORIGINALS = (
    "zed.original@corp.lan",
    "4111111111111111",
    "Ivan Originalov",
    "sk-ant-e2e-original-key",
    "ct_e2e_original_token",
)


@skip_if_no_langfuse
async def test_no_originals_in_batch_payload(sink_and_client) -> None:
    """No original reaches the Langfuse-bound payload: the sink forwards a fixed
    metadata subset, and the NEVER gate refuses a record that carries a mapping,
    original content or a credential before anything is sent."""
    sink, control = sink_and_client
    email, card, person, api_key, corp_token = ORIGINALS
    event = _event(
        user_id="alice",
        team_id="t1",
        redaction_count=3,
        finding_label_counts={"EMAIL": 1, "CREDIT_CARD": 1, "PERSON": 1},
        placeholder_list=("[CREDIT_CARD_001]", "[EMAIL_001]", "[PERSON_001]"),
    )
    await sink.write_event(event)
    # A record from another producer: the audit fields plus content-bearing keys
    # outside the forwarded subset, at the top level and nested.
    record = {
        "timestamp": event.timestamp.isoformat(),
        "request_id": "e2e-req-2",
        "user_id": "alice",
        "team_id": "t1",
        "provider": "anthropic",
        "model": "claude-opus-4-7",
        "latency_ms": 250,
        "prompt_token_count": 42,
        "completion_token_count": 17,
        "redaction_count": 3,
        "finding_label_counts": {"EMAIL": 1, "CREDIT_CARD": 1, "PERSON": 1},
        "cache_a_hit": False,
        "status": "ok",
        "placeholder_list": ["[CREDIT_CARD_001]", "[EMAIL_001]", "[PERSON_001]"],
        "messages": [{"role": "user", "content": f"mail {email}, card {card}"}],
        "prompt": f"{person} wrote to {email}",
        "response": f"done, {person}",
        "detail": {"note": f"{card} {person}"},
    }
    await sink.write(record)

    # NEVER fields, top level and nested under a forwarded key: refused, nothing sent.
    never_values = {
        "mapping": {email: "[EMAIL_001]"},
        "mapping_table": [[person, "[PERSON_001]"]],
        "pairs": [[card, "[CREDIT_CARD_001]"]],
        "original_content": f"{person} {email}",
        "unredacted_content": card,
        "pre_sanitization": email,
        "replace_md": f"{person} -> [PERSON_001]",
        "rule_values": [person],
        "x_corp_auth": corp_token,
        "corp_token": corp_token,
        "api_key": api_key,
        "authorization": f"Bearer {api_key}",
        "cookie": f"session={corp_token}",
        "set_cookie": f"session={corp_token}",
        "extra_headers": {"x-api-key": api_key},
    }
    assert set(never_values) == NEVER_FIELDS
    for key, value in never_values.items():
        for smuggled in ({**record, key: value}, {**record, "metadata": {key: value}}):
            with pytest.raises(NeverFieldPresentError):
                await sink.write(smuggled)

    captures = (await control.get("/__captures")).json()
    assert captures["count"] == 2
    wire = json.dumps(captures)
    for original in ORIGINALS:
        assert original not in wire
    for cap in captures["captures"]:
        trace = next(e for e in cap["body"]["batch"] if e["type"] == "trace-create")["body"]
        assert trace["userId"] == "alice"
        assert trace["metadata"]["team_id"] == "t1"
        assert trace["metadata"]["redaction_count"] == 3
        assert trace["metadata"]["finding_label_counts"] == {
            "EMAIL": 1,
            "CREDIT_CARD": 1,
            "PERSON": 1,
        }
