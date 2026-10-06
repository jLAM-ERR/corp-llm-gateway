"""Stage 5: the DLP egress guard re-scans the sanitised request and blocks a surviving secret."""

import json
import logging
from datetime import UTC, datetime

import pytest

from corp_llm_gateway.litellm_hook import GuardrailHttpException
from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard
from tests.hook_fixtures import _build_guardrail, _data_with_token


async def test_stage5_dlp_blocks_canary_survivor(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stage 5 blocks a canary that the primary sanitizer did not redact."""
    canary = "DLP-CANARY-RAW-99999"
    g, _ = _build_guardrail(
        [], dlp_guard=DlpEgressGuard(canary_patterns=[canary], secret_rescan=False)
    )
    data = _data_with_token("tok-1", content=f"here is {canary}")
    with caplog.at_level(logging.INFO), pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_DLP_BLOCKED"
    assert "litellm_egress_blocked" in caplog.text
    assert "dlp:canary" in caplog.text
    blocked = [r for r in caplog.records if r.getMessage().startswith("litellm_egress_blocked ")]
    assert len(blocked) == 1
    assert blocked[0].levelno == logging.INFO
    assert "block_reason=dlp:canary" in blocked[0].getMessage().split()


async def test_stage5_dlp_clean_request_passes_through() -> None:
    """A request without the canary passes Stage 5 and returns data."""
    canary = "DLP-CANARY-RAW-99999"
    g, _ = _build_guardrail(
        [], dlp_guard=DlpEgressGuard(canary_patterns=[canary], secret_rescan=False)
    )
    data = _data_with_token("tok-1", content="ordinary request without canary")
    out = await g.pre_call(data)
    assert out["messages"][0]["content"] == "ordinary request without canary"


async def test_stage5_dlp_audit_has_block_reason_dlp_canary() -> None:
    """The failure-event audit after Stage-5 block carries block_reason='dlp:canary'."""
    canary = "DLP-CANARY-RAW-99999"
    g, sink = _build_guardrail(
        [], dlp_guard=DlpEgressGuard(canary_patterns=[canary], secret_rescan=False)
    )
    now = datetime.now(UTC)
    data = _data_with_token("tok-1", content=f"leaked {canary} here")
    with pytest.raises(GuardrailHttpException):
        await g.pre_call(data)
    # The Stage-5 block audits INLINE (litellm doesn't fire the failure event for
    # a pre_call rejection) — exactly one record right after pre_call.
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec.get("block_reason") == "dlp:canary"
    assert rec.get("error_code") == "E_DLP_BLOCKED"
    assert rec.get("status") == "failed"
    # Raw canary value must NOT appear in any audit field.
    assert canary not in json.dumps(rec)
    # Idempotency: a follow-up failure-event audit adds no second record.
    await g.audit(data, None, start_time=now, end_time=now, status="failed")
    assert len(sink.records) == 1


async def test_stage5_dlp_raw_secret_blocked_by_default_guard() -> None:
    """A raw OpenAI API key that survived the primary sanitizer is blocked by Stage 5."""
    raw_key = "sk-" + "a" * 48
    # Default DlpEgressGuard (secret_rescan=True) — no canaries needed.
    g, _ = _build_guardrail(pairs=[])
    data = _data_with_token("tok-1", content=f"my key is {raw_key}")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_DLP_BLOCKED"


async def test_stage5_dlp_blocks_canary_in_responses_custom_tool_call_input() -> None:
    """Stage 5 must see a canary inside a Responses custom_tool_call.input — it was
    previously invisible (`collect_tool_call_text` returned [] for this item type,
    defect #1), so the canary egressed unblocked."""
    canary = "DLP-CANARY-RAW-99999"
    g, _ = _build_guardrail(
        [], dlp_guard=DlpEgressGuard(canary_patterns=[canary], secret_rescan=False)
    )
    data = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "type": "custom_tool_call",
                "call_id": "call_1",
                "name": "apply_patch",
                "input": f"*** Add File: x\n+SECRET={canary}",
            }
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_DLP_BLOCKED"


async def test_stage5_dlp_blocks_canary_in_local_shell_call_action_command() -> None:
    """CRITICAL 1: local_shell_call.action.command is the Codex CLI shell tool —
    the primary shape this profile exists to support. Previously
    `collect_responses_item_text` had no field-name entry for `action`, so a
    canary there egressed unblocked (identical shape already caught for
    custom_tool_call.input above)."""
    canary = "DLP-CANARY-RAW-99999"
    g, _ = _build_guardrail(
        [], dlp_guard=DlpEgressGuard(canary_patterns=[canary], secret_rescan=False)
    )
    data = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "type": "local_shell_call",
                "call_id": "call_1",
                "status": "completed",
                "action": {
                    "type": "exec",
                    "command": ["bash", "-lc", f"echo {canary}"],
                    "env": {"TOKEN": canary},
                },
            }
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_DLP_BLOCKED"


async def test_stage5_dlp_scans_unmanaged_call_type_input_without_rewriting() -> None:
    """A non-chat endpoint (embeddings) never gets its `input` rewritten
    (MAJOR 6's denylist), but Stage 5 must still SCAN it for canaries/raw
    secrets — an unmanaged call_type must not become a DLP blind spot."""
    canary = "DLP-CANARY-RAW-99999"
    g, _ = _build_guardrail(
        [], dlp_guard=DlpEgressGuard(canary_patterns=[canary], secret_rescan=False)
    )
    data = {
        "model": "text-embedding-3-small",
        "input": f"embed this {canary} please",
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data, call_type="embedding")
    assert ei.value.error_code == "E_DLP_BLOCKED"


async def test_stage5_dlp_disabled_by_flag_passes_through() -> None:
    """When CORP_LLM_DLP_GUARD=0 Stage 5 is skipped entirely."""
    import os

    from corp_llm_gateway import config as _cfg_module

    canary = "DLP-CANARY-RAW-99999"
    os.environ["CORP_LLM_DLP_GUARD"] = "0"
    _cfg_module.reset_cache()
    try:
        g, _ = _build_guardrail(
            [], dlp_guard=DlpEgressGuard(canary_patterns=[canary], secret_rescan=False)
        )
        data = _data_with_token("tok-1", content=f"here is {canary}")
        out = await g.pre_call(data)
        assert canary in out["messages"][0]["content"]
    finally:
        del os.environ["CORP_LLM_DLP_GUARD"]
        _cfg_module.reset_cache()
