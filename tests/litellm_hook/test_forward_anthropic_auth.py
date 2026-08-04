"""Provider-gated Anthropic OAuth bridge in pre_call (`forward_anthropic_auth`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
from corp_llm_gateway.metrics import MetricsExporter
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo
from tests.test_litellm_hook import _corp_llm_returning, _StaticRules

_OAUTH_TOKEN = "sk-ant-oat01-abcdef"
_ANTHROPIC_MODEL = "claude-sonnet-4-5"


class _RecordingMetrics(MetricsExporter):
    def __init__(self) -> None:
        self.blocks: list[str] = []
        self.failures: list[str] = []

    def record_block(self, block_reason: str) -> None:
        self.blocks.append(block_reason)

    def record_failure(self, component: str) -> None:
        self.failures.append(component)

    def observe_request_latency(self, seconds: float, *, status: str) -> None:
        return None


def _guardrail(
    *, forward_anthropic_auth: bool = True
) -> tuple[CorpLlmGuardrail, ListSink, _RecordingMetrics]:
    store = InMemoryTokenStore()
    now = datetime.now(UTC)
    store.upsert(
        TokenInfo(
            corp_token="tok-1",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    orch = SanitizationOrchestrator(
        _corp_llm_returning([]),
        InMemoryMappingStore(),
        _StaticRules(),
    )
    sink = ListSink()
    metrics = _RecordingMetrics()
    g = CorpLlmGuardrail(
        orch,
        AuthMiddleware(store),
        AuditLogger(sink, gateway_version="0.0.1"),
        forward_anthropic_auth=forward_anthropic_auth,
        metrics=metrics,
    )
    return g, sink, metrics


def _request(
    *, model: str = _ANTHROPIC_MODEL, authorization: str = f"Bearer {_OAUTH_TOKEN}"
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "headers": {
            "X-Corp-Auth": "tok-1",
            "Authorization": authorization,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-cli/2.1.220",
            "Host": "127.0.0.1:4000",
            "X-Unrelated": "do-not-forward",
        },
    }


async def test_bridge_lifts_oauth_token_into_api_key() -> None:
    g, _, _ = _guardrail()

    out = await g.pre_call(_request())
    upstream = {name.lower(): value for name, value in out["extra_headers"].items()}

    assert out["api_key"] == _OAUTH_TOKEN
    assert "authorization" not in upstream
    assert upstream["anthropic-version"] == "2023-06-01"
    assert upstream["anthropic-beta"] == "oauth-2025-04-20"
    assert upstream["user-agent"] == "claude-cli/2.1.220"
    assert "x-corp-auth" not in upstream
    assert "host" not in upstream
    assert "x-unrelated" not in upstream


async def test_bridge_reads_the_merged_litellm_header_buckets() -> None:
    g, _, _ = _guardrail()
    data: dict[str, Any] = {
        "model": _ANTHROPIC_MODEL,
        "messages": [{"role": "user", "content": "hello"}],
        "headers": {"X-Corp-Auth": "tok-1"},
        "secret_fields": {
            "raw_headers": {
                "Authorization": f"Bearer {_OAUTH_TOKEN}",
                "anthropic-version": "2023-06-01",
                "X-Corp-Auth": "tok-1",
            }
        },
    }

    out = await g.pre_call(data)
    upstream = {name.lower(): value for name, value in out["extra_headers"].items()}

    assert out["api_key"] == _OAUTH_TOKEN
    assert "authorization" not in upstream
    assert upstream["anthropic-version"] == "2023-06-01"
    assert "x-corp-auth" not in upstream


async def test_bridge_scrubs_metadata_and_top_level_user() -> None:
    g, _, _ = _guardrail()
    data = _request()
    data["metadata"] = {"user_api_key_user_id": "alice", "requester_metadata": {"trace": "abc"}}
    data["user"] = "alice@corp.example"

    out = await g.pre_call(data)

    assert "metadata" not in out
    assert "user" not in out
    # Correlation survives the drop.
    assert out["litellm_metadata"]["_corp_gateway_request_id"]


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param("", id="missing"),
        pytest.param(f"Basic {_OAUTH_TOKEN}", id="non_bearer"),
        pytest.param("Bearer ", id="empty_bearer"),
        pytest.param("Bearer sk-ant-api03-abcdef", id="plain_api_key"),
    ],
)
async def test_malformed_bearer_is_rejected_with_audit_and_metric(authorization: str) -> None:
    g, sink, metrics = _guardrail()
    data = _request(authorization=authorization)
    if not authorization:
        del data["headers"]["Authorization"]

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)

    assert ei.value.status_code == 401
    assert ei.value.error_code == "E_PROVIDER_AUTH"
    assert metrics.failures == ["auth"]
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec["status"] == "failed"
    assert rec["error_code"] == "E_PROVIDER_AUTH"
    assert "api_key" not in data


async def test_flag_off_leaves_the_request_untouched() -> None:
    g, _, _ = _guardrail(forward_anthropic_auth=False)
    data = _request()
    data["metadata"] = {"requester_metadata": {"trace": "abc"}}
    data["user"] = "alice@corp.example"

    out = await g.pre_call(data)

    assert "api_key" not in out
    assert "extra_headers" not in out
    assert out["metadata"]["requester_metadata"] == {"trace": "abc"}
    assert out["user"] == "alice@corp.example"


@pytest.mark.parametrize(
    "model",
    [
        pytest.param("gpt-5.6-sol", id="openai"),
        pytest.param("corp-glm-5.1", id="corp_vllm"),
    ],
)
async def test_non_anthropic_route_never_receives_the_oauth_token(model: str) -> None:
    """The gate keeps the subscription token off another provider's upstream call.

    Scoped to ``api_key`` + ``extra_headers`` on purpose: the inbound
    ``Authorization`` header itself is deliberately preserved in every header
    bucket (invariant 3, pinned by tests/invariants/test_no_originals_leak.py),
    so a blanket "token absent from data" assertion would contradict it.
    """
    g, _, _ = _guardrail()
    data = _request(model=model)
    data["api_key"] = "deployment-key"

    out = await g.pre_call(data)

    assert out["api_key"] == "deployment-key"
    extra_headers: dict[str, str] = out.get("extra_headers") or {}
    assert _OAUTH_TOKEN not in extra_headers.values()
