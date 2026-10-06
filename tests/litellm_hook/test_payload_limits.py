"""Request size limits in the pre-call: the oversize policy (fail-closed, chunk, deliver-flag)
and the max_output_tokens cap."""

import logging
import re
import time

import pytest

from corp_llm_gateway.detectors import RegexChecksumDetector
from corp_llm_gateway.litellm_hook import GuardrailHttpException
from tests.hook_fixtures import (
    _build_guardrail,
    _build_guardrail_oversize,
    _build_guardrail_with_cap,
    _corp_llm_email_per_segment,
    _data_with_token,
)


async def test_pre_call_oversize_on_instructions_second_field_fails_closed() -> None:
    """Error branches must fire per field, not just for whichever field the old
    ternary happened to pick: oversize on `instructions` (the SECOND field in
    iteration order) while `system` is small and clean."""
    secret = "sk-" + "a" * 40
    g, _ = _build_guardrail_oversize(threshold=64)
    data = {
        "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "hello"}],
        "system": "fine",
        "instructions": f"{secret} " + "x" * 200,
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_OVERSIZE_BLOCKED"


async def test_oversize_message_leaf_fails_closed_not_leaked() -> None:
    """F1: an oversize message leaf is refused (fail-closed), never forwarded verbatim.

    Replaces the old M1-11 deliver-and-flag behaviour, which egressed the email
    inside an oversize message UNREDACTED — the exact leak F1 closes.
    """
    from corp_llm_gateway.payload import DEFAULT_THRESHOLD_BYTES

    email_in_big_msg = "overflow@corp.example"
    padding = "x" * (DEFAULT_THRESHOLD_BYTES + 1)
    big_content = f"{padding} {email_in_big_msg}"

    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token("tok-1", content=big_content, system="admin is a@corp.example")
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_OVERSIZE_BLOCKED"


async def test_pre_call_oversize_message_leaf_repro_blocked() -> None:
    """F1 repro (message leaf): an oversize leaf carrying an sk- key must not egress."""
    secret = "sk-" + "a" * 40
    g, _ = _build_guardrail_oversize(threshold=64)
    big = f"{secret} " + "x" * 200
    data = _data_with_token("tok-1", content=big)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_OVERSIZE_BLOCKED"


async def test_pre_call_oversize_document_source_data_repro_blocked() -> None:
    """F1 repro (document.source.data leaf): oversize document data must not egress."""
    secret = "sk-" + "b" * 40
    g, _ = _build_guardrail_oversize(threshold=64)
    big = f"{secret} " + "y" * 200
    doc = [{"type": "document", "source": {"type": "text", "data": big}}]
    data = _data_with_token("tok-1", content=doc)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_OVERSIZE_BLOCKED"


async def test_pre_call_oversize_unmanaged_input_fails_closed_before_scanning() -> None:
    """Round-4 IMPORTANT 6: the unmanaged (embeddings/etc.) DLP/Stage-0 scan
    joined and regex-scanned the WHOLE input with no size gate — the same
    threshold policy the managed (messages) path enforces via sanitize_one
    must also apply here, blocking BEFORE the expensive scan runs."""
    from corp_llm_gateway.payload import DEFAULT_THRESHOLD_BYTES

    padding = "x" * (DEFAULT_THRESHOLD_BYTES + 1)
    g, _ = _build_guardrail()
    data = {
        "model": "text-embedding-3-small",
        "input": padding,
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data, call_type="embedding")
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_OVERSIZE_BLOCKED"


async def test_pre_call_oversize_message_chunk_policy_sanitizes() -> None:
    """chunk policy at the hook level: an oversize leaf's email is redacted, not leaked."""
    email = "chunky@corp.example"
    g, _ = _build_guardrail_oversize(
        threshold=64, policy="chunk", local_detectors=[RegexChecksumDetector()]
    )
    big = "prefix " + email + " " + "x" * 200
    data = _data_with_token("tok-1", content=big)
    out = await g.pre_call(data)
    body = out["messages"][0]["content"]
    assert email not in body, "chunk policy leaked the email"
    assert re.search(r"\[EMAIL_\d+\]", body), f"email not redacted: {body[:80]!r}"


async def test_oversize_deliver_flag_marks_audit_block_reason() -> None:
    """M1: a deliver-flag egress is marked oversize:delivered in the audit record."""
    g, sink = _build_guardrail_oversize(
        threshold=64, policy="deliver-flag", deliver_teams=frozenset({"t1"})
    )
    clean = "the quick brown fox jumps over the lazy dog and keeps running along here"
    assert len(clean.encode("utf-8")) > 64
    data = _data_with_token("tok-1", content=clean)
    out = await g.pre_call(data)
    assert out["messages"][0]["content"] == clean, "clean oversize leaf must be delivered"
    start = time.time()
    await g.async_log_success_event(
        kwargs={"data": data},
        response_obj={"choices": [{"message": {"content": "ok"}}]},
        start_time=start,
        end_time=start + 0.100,
    )
    assert len(sink.records) == 1
    assert sink.records[0]["block_reason"] == "oversize:delivered"


async def test_normal_request_audit_has_no_block_reason() -> None:
    """M1 control: a normal zero/non-zero-redaction request carries no marker."""
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
    assert "block_reason" not in sink.records[0]


async def test_pre_call_max_tokens_clamped_when_over_cap(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A request's `max_tokens` above the configured cap is clamped down before
    sanitization/upstream, and the clamp is logged with the before/after values."""
    g, _ = _build_guardrail_with_cap(4096)
    data = _data_with_token("tok-1", content="hello")
    data["max_tokens"] = 64000

    with caplog.at_level(logging.INFO):
        out = await g.pre_call(data)

    assert out["max_tokens"] == 4096
    assert "litellm_pre_call_max_tokens_clamped" in caplog.text
    assert "requested=64000" in caplog.text
    assert "capped=4096" in caplog.text


async def test_pre_call_max_tokens_not_clamped_when_at_or_under_cap() -> None:
    """`max_tokens` at or under the cap is left untouched."""
    g, _ = _build_guardrail_with_cap(4096)
    data = _data_with_token("tok-1", content="hello")
    data["max_tokens"] = 4096

    out = await g.pre_call(data)

    assert out["max_tokens"] == 4096


async def test_pre_call_max_tokens_cap_none_by_default() -> None:
    """No `max_output_tokens_cap` configured (the default) leaves max_tokens alone."""
    g, _ = _build_guardrail()
    data = _data_with_token("tok-1", content="hello")
    data["max_tokens"] = 64000

    out = await g.pre_call(data)

    assert out["max_tokens"] == 64000


async def test_system_oversize_deliver_flag_logs_system_oversize_delivered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The deliver-flag oversize policy on data["system"] takes the
    `_sanitize_prompt_field` skipped branch and logs
    litellm_pre_call_system_oversize_delivered."""
    g, sink = _build_guardrail_oversize(
        threshold=64, policy="deliver-flag", deliver_teams=frozenset({"t1"})
    )
    clean_system = "the quick brown fox jumps over the lazy dog and keeps running along here"
    assert len(clean_system.encode("utf-8")) > 64
    data = _data_with_token("tok-1", content="hi", system=clean_system)

    with caplog.at_level(logging.WARNING):
        out = await g.pre_call(data)

    assert out["system"] == clean_system, "clean oversize system leaf must be delivered"
    assert "litellm_pre_call_system_oversize_delivered" in caplog.text
    assert "field=system" in caplog.text
    delivered = [
        r
        for r in caplog.records
        if r.getMessage().startswith("litellm_pre_call_system_oversize_delivered ")
    ]
    assert len(delivered) == 1
    assert delivered[0].levelno == logging.WARNING
    tokens = delivered[0].getMessage().split()
    assert "field=system" in tokens
    # docs/security.md: every deliver-flag egress is auditable in the log line too.
    assert "block_reason=oversize:delivered" in tokens

    start = time.time()
    await g.async_log_success_event(
        kwargs={"data": data},
        response_obj={"choices": [{"message": {"content": "ok"}}]},
        start_time=start,
        end_time=start + 0.100,
    )
    assert len(sink.records) == 1
    assert sink.records[0]["block_reason"] == "oversize:delivered"
