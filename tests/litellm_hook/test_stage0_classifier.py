"""Stage 0: the payload classifier refuses config / log shaped requests before egress."""

import time

import pytest

from corp_llm_gateway.litellm_hook import GuardrailHttpException
from tests.hook_fixtures import _build_guardrail, _corp_llm_unreachable, _data_with_token


async def test_stage0_blocks_env_dump_in_responses_custom_tool_call_input() -> None:
    """Stage 0 must see custom_tool_call.input — previously invisible (defect #1)."""
    g, _ = _build_guardrail([])
    data = {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "type": "custom_tool_call",
                "call_id": "call_1",
                "name": "apply_patch",
                "input": "DB_HOST=prod.internal\nDB_USER=admin\nDB_PASSWORD=hunter2\n",
            }
        ],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer oauth"},
    }
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_POLICY_BLOCKED"


async def test_stage0_env_payload_raises_policy_blocked() -> None:
    """.env content is blocked before the sanitizer / upstream is called."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    env_content = (
        "DATABASE_URL=postgres://admin:hunter2@db.corp.lan:5432/prod\n"
        "SECRET_KEY=supersecretvalue-abc123\n"
        "DEBUG=False\n"
        "REDIS_URL=redis://cache.corp.lan:6379/0\n"
        "ALLOWED_HOSTS=*.corp.lan\n"
    )
    data = _data_with_token("tok-1", content=env_content)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_POLICY_BLOCKED"


async def test_stage0_kube_payload_raises_policy_blocked() -> None:
    """Kubernetes manifest is blocked before egress."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    kube_content = (
        "apiVersion: apps/v1\n"
        "kind: Deployment\n"
        "metadata:\n"
        "  name: my-app\n"
        "  namespace: production\n"
        "spec:\n"
        "  replicas: 3\n"
        "  selector:\n"
        "    matchLabels:\n"
        "      app: my-app\n"
    )
    data = _data_with_token("tok-1", content=kube_content)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_POLICY_BLOCKED"


async def test_stage0_log_dump_raises_policy_blocked() -> None:
    """Application log dump is blocked before egress."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    log_lines = "\n".join(
        [
            "2024-01-15 10:00:01 INFO  Starting application server",
            "2024-01-15 10:00:02 INFO  Loaded configuration from /etc/app.conf",
            "2024-01-15 10:00:03 DEBUG Database pool initialized connections=10",
            "2024-01-15 10:00:15 INFO  Listening on 0.0.0.0:8080",
            "2024-01-15 10:01:22 ERROR Failed to connect to cache host=redis port=6379",
            "2024-01-15 10:01:23 WARN  Retrying connection attempt=1",
            "2024-01-15 10:01:25 WARN  Retrying connection attempt=2",
            "2024-01-15 10:01:30 ERROR Max retries exceeded",
        ]
    )
    data = _data_with_token("tok-1", content=log_lines)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_POLICY_BLOCKED"


async def test_stage0_upstream_not_called_for_blocked_request() -> None:
    """The orchestrator/upstream is NOT invoked for a Stage-0 blocked request.

    The guardrail uses _corp_llm_unreachable() — if sanitize() were called,
    the transport raises ConnectTimeout → E_CORP_LLM_DOWN, not E_POLICY_BLOCKED.
    Seeing E_POLICY_BLOCKED proves the upstream path was never reached.
    """
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    env_content = (
        "DATABASE_URL=postgres://user:secret@db.lan/prod\n"
        "SECRET_KEY=abc123-secret-value\n"
        "DEBUG=0\n"
        "LOG_LEVEL=WARNING\n"
        "MAX_WORKERS=4\n"
    )
    data = _data_with_token("tok-1", content=env_content)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    # E_POLICY_BLOCKED proves we never reached the unreachable upstream.
    assert ei.value.error_code == "E_POLICY_BLOCKED"


async def test_stage0_audit_record_emitted_with_block_reason() -> None:
    """A Stage-0 block must produce an audit record carrying block_reason."""
    g, sink = _build_guardrail()
    env_content = (
        "DATABASE_URL=postgres://admin:pass@db.corp.lan/prod\n"
        "SECRET_KEY=supersecretvalue\n"
        "DEBUG=False\n"
        "REDIS_URL=redis://cache.corp.lan\n"
        "LOG_LEVEL=ERROR\n"
    )
    data = _data_with_token("tok-1", content=env_content)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.error_code == "E_POLICY_BLOCKED"
    # litellm does NOT fire async_log_failure_event for a pre_call rejection, so
    # the block is audited INLINE — exactly one record right after pre_call.
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec.get("block_reason") == "config:env"
    assert rec.get("status") == "failed"
    assert rec.get("error_code") == "E_POLICY_BLOCKED"
    # Attribution fields must be real (not "unknown") because state was created before Stage 0.
    assert rec.get("user_id") == "alice"
    assert rec.get("team_id") == "t1"
    # Idempotency: if a future litellm ALSO fired the failure event, no 2nd record.
    _start = time.time()
    await g.audit(data, None, start_time=_start, end_time=_start + 0.05, status="failed")
    assert len(sink.records) == 1


async def test_stage0_clean_request_passes_through() -> None:
    """A clean prose message is NOT blocked by Stage 0."""
    g, _ = _build_guardrail([("alice", "[N1]")])
    data = _data_with_token("tok-1", content="Please help me debug my Python function.")
    # Should not raise — corp LLM is a no-op mock returning no pairs.
    out = await g.pre_call(data)
    assert out["messages"][0]["content"] == "Please help me debug my Python function."


async def test_stage0_exception_message_is_generic() -> None:
    """The GuardrailHttpException message must NOT contain any raw payload content."""
    g, _ = _build_guardrail(corp_llm=_corp_llm_unreachable())
    secret_content = "DATABASE_URL=postgres://admin:hunter2@db.corp.lan/prod\n" * 3 + (
        "SECRET_KEY=sk-very-secret\nDEBUG=0\nREDIS_URL=redis://cache\n"
    )
    data = _data_with_token("tok-1", content=secret_content)
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    exc_msg = str(ei.value)
    assert "hunter2" not in exc_msg
    assert "sk-very-secret" not in exc_msg
    assert "admin" not in exc_msg


async def test_stage0_disabled_by_flag_allows_through() -> None:
    """When CORP_LLM_BLOCK_PAYLOADS=0 the classifier is skipped entirely."""
    import os

    from corp_llm_gateway import config as _cfg_module

    env_content = (
        "DATABASE_URL=postgres://admin:pass@db/prod\n"
        "SECRET_KEY=abc123\nDEBUG=False\nREDIS_URL=redis://cache\nLOG_LEVEL=ERROR\n"
    )
    os.environ["CORP_LLM_BLOCK_PAYLOADS"] = "0"
    _cfg_module.reset_cache()
    try:
        g, _ = _build_guardrail([])  # corp-LLM returns no pairs
        data = _data_with_token("tok-1", content=env_content)
        # Should NOT raise — flag disabled the classifier
        out = await g.pre_call(data)
        assert out["messages"][0]["content"] == env_content
    finally:
        del os.environ["CORP_LLM_BLOCK_PAYLOADS"]
        _cfg_module.reset_cache()


async def test_stage0_scans_unmanaged_call_type_input_too() -> None:
    """Round-4 IMPORTANT 5: Stage 0 iterated only `messages` (empty for an
    unmanaged call_type), so a config/secret dump posted to /v1/embeddings
    was never blocked, while identical text in `messages` would be."""
    g, _ = _build_guardrail()
    env_content = (
        "DATABASE_URL=postgres://admin:pass@db.corp.lan/prod\n"
        "SECRET_KEY=supersecretvalue\n"
        "DEBUG=False\n"
        "REDIS_URL=redis://cache.corp.lan\n"
        "LOG_LEVEL=ERROR\n"
    )
    data = {
        "model": "text-embedding-3-small",
        "input": env_content,
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data, call_type="embedding")
    assert ei.value.error_code == "E_POLICY_BLOCKED"
