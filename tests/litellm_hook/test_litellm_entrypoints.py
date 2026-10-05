"""The hook through litellm's own entry points: `async_pre_call_hook` and the log events."""

import time
from typing import Any

from tests.hook_fixtures import _build_guardrail, _data_with_token


async def test_async_pre_call_hook_threads_call_type_to_embeddings_gate() -> None:
    """Wiring test: the real litellm entry point must pass call_type through,
    not just the pure-logic pre_call() method."""
    g, _ = _build_guardrail([("alice", "[NAME_001]")])
    data = {
        "model": "text-embedding-3-small",
        "input": "alice@corp.example",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    out = await g.async_pre_call_hook(None, None, data, "embedding")
    assert out["input"] == "alice@corp.example"


async def test_async_pre_call_hook_delegates() -> None:
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    out = await g.async_pre_call_hook(None, None, data, "completion")
    assert out["messages"][0]["content"] == "hi [N1]"


async def test_async_log_success_event_emits_audit() -> None:
    g, sink = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data)
    start = time.time()
    await g.async_log_success_event(
        kwargs={"data": data},
        response_obj={"choices": [{"message": {"content": "ok [N1]"}}]},
        start_time=start,
        end_time=start + 0.100,
    )
    assert len(sink.records) == 1
    assert sink.records[0]["status"] == "ok"


async def test_audit_preserves_user_and_team_across_pre_post_handoff() -> None:
    """Regression: litellm's anthropic-passthrough route hands the
    post-call hooks a NEW ``data`` dict that doesn't carry our top-level
    ``_corp_gateway_request_id``. Before the fix, the audit emitted
    user_id/team_id/model="unknown" because _req_state lookup missed.

    Simulate this by:
      1. running pre_call on dict A,
      2. invoking async_log_success_event with a kwargs envelope where
         ``data`` is a FRESH dict B (no _corp_gateway_request_id at the
         top level) but where metadata/litellm_params carry the id.

    The audit must round-trip the id via metadata and recover state.

    NOTE: this covers the legacy metadata-scatter FALLBACK (no
    ``litellm_call_id`` in the envelope). The primary path — litellm's own
    ``litellm_call_id`` carried across the boundary — is covered by
    ``test_audit_recovers_state_via_litellm_call_id``.
    """
    g, sink = _build_guardrail([("alice", "[N1]")])
    data_pre = _data_with_token("tok-1", content="hi alice")
    await g.pre_call(data_pre)
    rid = data_pre["_corp_gateway_request_id"]

    # Build a fresh dict the way litellm's anthropic-passthrough does.
    fresh_data: dict[str, object] = {
        "model": "claude-opus-4-8",
        "messages": data_pre["messages"],
    }
    kwargs_envelope = {
        "data": fresh_data,
        "metadata": {"_corp_gateway_request_id": rid},
        "litellm_params": {
            "metadata": {"_corp_gateway_request_id": rid},
        },
    }
    start = time.time()
    await g.async_log_success_event(
        kwargs=kwargs_envelope,
        response_obj={"choices": [{"message": {"content": "ok [N1]"}}]},
        start_time=start,
        end_time=start + 0.100,
    )
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec["request_id"] == rid, "round-trip failed: new UUID generated"
    assert rec["user_id"] == "alice", f"got {rec['user_id']!r}, expected alice"
    assert rec["team_id"] == "t1", f"got {rec['team_id']!r}, expected t1"
    assert rec["model"] == "claude", f"got {rec['model']!r}, expected claude"
    assert rec["status"] == "ok"


async def test_audit_recovers_state_via_litellm_call_id() -> None:
    """Regression: litellm v1.85 carries ``litellm_call_id`` (NOT our scattered
    ``_corp_gateway_request_id``, and no top-level ``metadata``) across the
    pre_call → log-event boundary. Per-request state must be keyed on
    ``litellm_call_id`` so the audit keeps the real identity + redaction count.

    Before the fix the audit fell back to a fresh UUID → user/team/model
    "unknown" and redaction_count 0 (seen live as audit records whose
    request_id differed from pre_call's).
    """
    g, sink = _build_guardrail([("alice", "[N1]")])
    call_id = "litellm-call-abc123"
    data = _data_with_token("tok-1", content="hi alice")
    data["litellm_call_id"] = call_id
    await g.pre_call(data)
    assert data["_corp_gateway_request_id"] == call_id  # state keyed on litellm id

    # litellm's log-event envelope, matching the live v1.85 shape: carries
    # litellm_call_id but NOT our id, and no top-level metadata dict.
    kwargs = {
        "litellm_call_id": call_id,
        "optional_params": {"model": "claude"},
        "litellm_params": {"litellm_call_id": call_id, "metadata": {}},
    }
    start = time.time()
    await g.async_log_success_event(
        kwargs=kwargs,
        response_obj={
            "choices": [{"message": {"content": "ok [N1]"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        },
        start_time=start,
        end_time=start + 0.1,
    )
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec["request_id"] == call_id, f"got {rec['request_id']!r}"
    assert rec["user_id"] == "alice"
    assert rec["team_id"] == "t1"
    assert rec["model"] == "claude"
    assert rec["redaction_count"] == 1
    assert rec["status"] == "ok"


async def test_codex_path_metadata_pop_does_not_break_audit_attribution() -> None:
    """Task 15 item 2: the ChatGPT auth bridge's `data.pop("metadata", None)`
    discards litellm's proxy-internal accounting dict wholesale rather than
    narrowing to a single offending key — litellm's real
    `add_litellm_data_to_request()` (proxy/litellm_pre_call_utils.py) populates
    `data["metadata"]` with dozens of keys (`user_api_key_auth`, `headers`,
    `requester_metadata`, ...), several holding non-string/non-serializable
    values, so there is no single key to narrow to; the whole shape is
    unsuited to the wire field, not one member of it. Audit attribution is
    unaffected by the pop either way: team_id/user_id come from AuthMiddleware
    via our own per-request state and request_id is keyed on
    `litellm_call_id` — neither reads `data["metadata"]`."""
    g, sink = _build_guardrail(forward_chatgpt_auth=True)
    call_id = "litellm-call-codex-1"
    data: dict[str, Any] = {
        "model": "gpt-5.6-sol",
        "input": "hello",
        "litellm_call_id": call_id,
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth-value"},
        # Shape modeled on litellm's real proxy-internal metadata dict for a
        # Responses-API call: a rich accounting dict, not a flat string map.
        "metadata": {
            "user_api_key_team_id": "t1",
            "user_api_key_user_id": "alice",
            "headers": {"authorization": "Bearer oauth-value"},
            "requester_metadata": {"trace": "abc"},
        },
    }
    out = await g.pre_call(data)
    assert "metadata" not in out, "the bridge must still drop the proxy-internal dict"

    await g.async_log_success_event(
        kwargs={
            "litellm_call_id": call_id,
            "optional_params": {"model": "gpt-5.6-sol"},
            "litellm_params": {"litellm_call_id": call_id, "metadata": {}},
        },
        response_obj={
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}]
        },
        start_time=time.time(),
        end_time=time.time() + 0.05,
    )
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec["request_id"] == call_id
    assert rec["user_id"] == "alice"
    assert rec["team_id"] == "t1"
    assert rec["status"] == "ok"
