"""What the hook logs: no original, no injected role or request id in a log line."""

import logging
import re

import pytest

from tests.hook_fixtures import (
    _async_iter,
    _build_guardrail,
    _corp_llm_email_per_segment,
    _data_with_token,
)
from tests.response_restore import restore_stream


async def test_pre_call_no_leak_original_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """M1-14: original PII must never leak into logs.

    Feed a known secret through the sanitization pipeline and assert
    that the secret string never appears in any log record emitted,
    only redaction counts and byte sizes.
    """
    secret = "alice.smith@corp.internal"
    g, _ = _build_guardrail([(secret, "[EMAIL_001]")])
    data = _data_with_token("tok-1", content=f"Contact: {secret}")

    with caplog.at_level(logging.INFO):
        await g.pre_call(data)

    # Assert the original secret does NOT appear anywhere in captured logs.
    for record in caplog.records:
        msg_text = record.getMessage()
        assert secret not in msg_text, f"LEAK DETECTED: secret '{secret}' found in log: {msg_text}"
    # Verify logs DO contain redaction metadata (byte size, count).
    log_text = caplog.text
    assert "litellm_pre_call_message_sanitize_start" in log_text
    assert "content_bytes=" in log_text
    assert "[EMAIL_001]" not in log_text


async def test_pre_call_no_leak_original_in_system_logs(caplog: pytest.LogCaptureFixture) -> None:
    """M1-14: system field sanitization logs never leak the original."""
    secret_env = "DB_PASSWORD=hunter2secret"
    g, _ = _build_guardrail([(secret_env, "[SECRET_001]")])
    data = _data_with_token("tok-1", content="hello", system=secret_env)

    with caplog.at_level(logging.INFO):
        await g.pre_call(data)

    for record in caplog.records:
        msg = record.getMessage()
        assert secret_env not in msg, f"LEAK in system logs: {msg}"
        assert "hunter2secret" not in msg


async def test_pre_call_logs_sanitize_done_per_block(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each text block sanitized logs a sanitize_done entry with redaction count."""
    g, _ = _build_guardrail([("alice", "[N1]"), ("bob", "[N2]")])
    content = [
        {"type": "text", "text": "hi alice"},
        {"type": "text", "text": "bye bob"},
    ]
    data = _data_with_token("tok-1", content=content)

    with caplog.at_level(logging.INFO):
        await g.pre_call(data)

    # Verify the request completes and sanitization logs are emitted.
    assert data["messages"][0]["content"][0]["text"] == "hi [N1]"
    assert data["messages"][0]["content"][1]["text"] == "bye [N2]"
    # Verify log output contains sanitization info
    assert "litellm_pre_call_message_sanitize_done" in caplog.text
    assert "redaction_count" in caplog.text


async def test_user_typed_placeholder_literal_preserved_not_collided(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Security fix: a user-typed [FAMILY_NNN] literal must not collide with a real redaction.

    The real email a@corp.example must be assigned a DIFFERENT token (e.g.
    [EMAIL_002]), because [EMAIL_001] is already present verbatim in the input.
    On the reverse pass, [EMAIL_001] is NOT in the mapping, so it stays unchanged
    in the response (the user's literal is preserved). The real redaction token
    ([EMAIL_002]) IS in the mapping and restores correctly.
    """
    g, _ = _build_guardrail(corp_llm=_corp_llm_email_per_segment())
    data = _data_with_token(
        "tok-1", content="My email a@corp.example and the marker [EMAIL_001] in docs"
    )

    with caplog.at_level(logging.WARNING):
        out = await g.pre_call(data)

    sanitized = out["messages"][0]["content"]

    # The real email must be redacted to a token OTHER than [EMAIL_001].
    assert "a@corp.example" not in sanitized, f"email not redacted: {sanitized!r}"
    real_ph = re.search(r"\[EMAIL_\d+\]", sanitized.replace("[EMAIL_001]", ""))
    assert real_ph is not None, f"no redaction token found: {sanitized!r}"
    real_token = real_ph.group(0)
    assert real_token != "[EMAIL_001]", (
        "collision: real email got [EMAIL_001] despite user typing it verbatim"
    )

    # The user's literal "[EMAIL_001]" still appears verbatim in egress.
    assert "[EMAIL_001]" in sanitized, f"user literal was removed: {sanitized!r}"

    # On post_call_stream: a response with both tokens restores correctly.
    chunks_in = [{"choices": [{"delta": {"content": f"{real_token} and [EMAIL_001]"}}]}]
    out_text = ""
    async for chunk in restore_stream(g, data, _async_iter(chunks_in)):
        out_text += chunk["choices"][0]["delta"]["content"]
    # The real token is restored; the user's literal is unchanged (not in mapping).
    assert "a@corp.example" in out_text, f"real email not restored: {out_text!r}"
    assert "[EMAIL_001]" in out_text, f"user literal was reversed: {out_text!r}"

    # Breadcrumb: warning was emitted with count=1 and contains NO email content.
    breadcrumb_lines = [
        r.getMessage()
        for r in caplog.records
        if "input_placeholder_literal_detected" in r.getMessage()
    ]
    assert breadcrumb_lines, "breadcrumb warning not emitted"
    assert "count=1" in breadcrumb_lines[0], f"expected count=1 in: {breadcrumb_lines[0]!r}"
    assert "a@corp.example" not in breadcrumb_lines[0], "email leaked into breadcrumb log"
    assert "[EMAIL_001]" not in breadcrumb_lines[0], "literal leaked into breadcrumb log"


async def test_tool_use_input_no_leak_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    """M1-14: PII inside tool_use.input must not appear in any log record."""
    secret = "tool-secret@corp.internal"
    g, _ = _build_guardrail([(secret, "[EMAIL_001]")])
    content = [
        {
            "type": "tool_use",
            "id": "t1",
            "name": "fn",
            "input": {"to": secret},
        }
    ]
    data = _data_with_token("tok-1", content=content)

    with caplog.at_level(logging.INFO):
        await g.pre_call(data)

    for record in caplog.records:
        msg = record.getMessage()
        assert secret not in msg, f"LEAK in logs: {msg!r}"


async def test_pre_call_unknown_role_logs_invalid_not_verbatim(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unbounded/adversarial `role` value must not reach the log line verbatim
    (litellm_hook.py:672 hardening) — only the known role set is logged as-is."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    injected = "user\nlitellm_pre_call_forged_line request_id=evil"
    data = {
        "model": "claude",
        "messages": [{"role": injected, "content": "hi alice"}],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    with caplog.at_level(logging.INFO):
        await g.pre_call(data)
    assert injected not in caplog.text
    assert "role=invalid" in caplog.text


async def test_pre_call_known_role_logs_verbatim() -> None:
    """A well-formed, known role is logged unchanged — no behavior regression."""
    from corp_llm_gateway.litellm_hook import _safe_role_for_log

    assert _safe_role_for_log({"role": "assistant"}) == "assistant"
    assert _safe_role_for_log({"role": "user"}) == "user"
    assert _safe_role_for_log({}) == "unknown"
    assert _safe_role_for_log({"role": None}) == "unknown"
    assert _safe_role_for_log({"role": 42}) == "invalid"


async def test_pre_call_newline_bearing_litellm_call_id_falls_back_to_uuid() -> None:
    """A caller-controlled `litellm_call_id` carrying a newline (log-injection
    attempt) must not be used verbatim as the request id — fall back to a
    generated UUID instead (litellm_hook.py:_ensure_request_id hardening)."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    forged = "abc\nlitellm_pre_call_forged_line request_id=evil"
    data = _data_with_token("tok-1", content="hi alice")
    data["litellm_call_id"] = forged

    out = await g.pre_call(data)

    assert out["_corp_gateway_request_id"] != forged
    assert "\n" not in out["_corp_gateway_request_id"]


async def test_pre_call_oversize_litellm_call_id_falls_back_to_uuid() -> None:
    """A `litellm_call_id` far beyond any realistic length must not be trusted
    verbatim — fall back to a generated UUID."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="hi alice")
    data["litellm_call_id"] = "x" * 1000

    out = await g.pre_call(data)

    assert out["_corp_gateway_request_id"] != data["litellm_call_id"]
    assert len(out["_corp_gateway_request_id"]) < 1000


async def test_pre_call_wellformed_litellm_call_id_used_verbatim() -> None:
    """A normal litellm_call_id is unaffected by the new validation."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    call_id = "litellm-call-abc123"
    data = _data_with_token("tok-1", content="hi alice")
    data["litellm_call_id"] = call_id

    out = await g.pre_call(data)

    assert out["_corp_gateway_request_id"] == call_id
