from typing import Any

from corp_llm_gateway.audit.event import AuditEvent
from corp_llm_gateway.audit.invariants import assert_no_never_fields
from corp_llm_gateway.audit.sinks import Sink


class AuditLogger:
    def __init__(self, sink: Sink, gateway_version: str) -> None:
        self._sink = sink
        self._gateway_version = gateway_version

    async def emit(self, event: AuditEvent) -> None:
        record = self._serialize(event)
        assert_no_never_fields(record)
        await self._sink.write(record)

    def _serialize(self, event: AuditEvent) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": event.timestamp.isoformat(),
            "request_id": event.request_id,
            "user_id": event.user_id,
            "team_id": event.team_id,
            "provider": event.provider,
            "model": event.model,
            "latency_ms": event.latency_ms,
            "prompt_token_count": event.prompt_token_count,
            "completion_token_count": event.completion_token_count,
            "redaction_count": event.redaction_count,
            "finding_label_counts": dict(event.finding_label_counts),
            "cache_a_hit": event.cache_a_hit,
            "gateway_version": self._gateway_version,
            "status": event.status,
        }
        if event.placeholder_list is not None:
            record["placeholder_list"] = list(event.placeholder_list)
        if event.error_code is not None:
            record["error_code"] = event.error_code
        if event.block_reason is not None:
            record["block_reason"] = event.block_reason
        if event.corp_llm_latency_ms is not None:
            record["corp_llm_latency_ms"] = event.corp_llm_latency_ms
        if event.pre_pass_latency_ms is not None:
            record["pre_pass_latency_ms"] = event.pre_pass_latency_ms
        if event.audit_buffer_full is not None:
            record["audit_buffer_full"] = event.audit_buffer_full
        return record
