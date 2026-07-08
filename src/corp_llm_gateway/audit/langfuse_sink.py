"""LangfuseSink — transforms AuditEvent → Langfuse public-API events
and POSTs them via /api/public/ingestion.

Plan ref: M3-4. The Vector path (helm configmap) is the production
default; this Python sink is for direct-to-Langfuse paths (e.g., the
in-process audit pipeline used by tests, or a pod that opts out of
Vector for low-volume / debug traffic).

Authentication: HTTP Basic with `public_key:secret_key`. Set per-team
override via env or per-deploy secret.
"""

from __future__ import annotations

import base64
import uuid
from typing import Any

import httpx

from corp_llm_gateway.audit.event import AuditEvent
from corp_llm_gateway.audit.invariants import assert_no_never_fields
from corp_llm_gateway.audit.sinks import Sink
from corp_llm_gateway.healthz import HealthStatus


class LangfuseIngestionError(Exception):
    pass


class LangfuseSink(Sink):
    def __init__(
        self,
        base_url: str,
        *,
        public_key: str,
        secret_key: str,
        http: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_header = "Basic " + base64.b64encode(
            f"{public_key}:{secret_key}".encode()
        ).decode("ascii")
        self._timeout = timeout
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._owned_http = http is None

    async def write(self, record: dict[str, Any]) -> None:
        assert_no_never_fields(record)
        batch = _records_to_batch([record])
        await self._post(batch)

    async def write_event(self, event: AuditEvent) -> None:
        record = _event_to_record(event)
        assert_no_never_fields(record)
        await self._post(_records_to_batch([record]))

    async def _post(self, batch: dict[str, Any]) -> None:
        try:
            resp = await self._http.post(
                f"{self._base_url}/api/public/ingestion",
                json=batch,
                headers={
                    "Authorization": self._auth_header,
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            raise LangfuseIngestionError(f"transport error: {exc}") from exc
        if resp.status_code >= 400:
            raise LangfuseIngestionError(
                f"langfuse ingestion {resp.status_code}: {resp.text[:300]}"
            )

    async def health(self) -> HealthStatus:
        """Cheap liveness probe against Langfuse's public health endpoint.

        Reuses the sink's own AsyncClient — the extension registry caches this
        live instance (audit/factory.py), so polling never opens a new client.
        """
        try:
            resp = await self._http.get(
                f"{self._base_url}/api/public/health", timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            return HealthStatus(False, f"langfuse_unreachable:{type(exc).__name__}")
        if resp.status_code >= 500:
            return HealthStatus(False, f"langfuse_http_{resp.status_code}")
        return HealthStatus(True, "langfuse_ok")

    async def aclose(self) -> None:
        if self._owned_http:
            await self._http.aclose()


def _event_to_record(event: AuditEvent) -> dict[str, Any]:
    """Mirror the AuditLogger serializer here so a LangfuseSink can be
    fed directly without going through AuditLogger."""
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
        "status": event.status,
    }
    if event.placeholder_list is not None:
        record["placeholder_list"] = list(event.placeholder_list)
    if event.error_code is not None:
        record["error_code"] = event.error_code
    if event.corp_llm_latency_ms is not None:
        record["corp_llm_latency_ms"] = event.corp_llm_latency_ms
    if event.pre_pass_latency_ms is not None:
        record["pre_pass_latency_ms"] = event.pre_pass_latency_ms
    if event.audit_buffer_full is not None:
        record["audit_buffer_full"] = event.audit_buffer_full
    return record


def _records_to_batch(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Map our flat audit records onto Langfuse's batch schema.

    Each record becomes one trace-create event + one generation-create
    event. Langfuse uses traces as the request envelope and generations
    as the LLM call within. We model the gateway request as the trace
    and the upstream Anthropic/OpenAI call as the generation.
    """
    events: list[dict[str, Any]] = []
    for record in records:
        trace_id = str(record.get("request_id", uuid.uuid4()))
        ts = str(record.get("timestamp", ""))
        events.append(
            {
                "id": str(uuid.uuid4()),
                "type": "trace-create",
                "timestamp": ts,
                "body": {
                    "id": trace_id,
                    "name": "corp-llm-gateway",
                    "userId": record.get("user_id"),
                    "metadata": {
                        "team_id": record.get("team_id"),
                        "redaction_count": record.get("redaction_count", 0),
                        "cache_a_hit": record.get("cache_a_hit", False),
                        "finding_label_counts": record.get("finding_label_counts", {}),
                        "gateway_version": record.get("gateway_version"),
                        "status": record.get("status"),
                        "error_code": record.get("error_code"),
                    },
                    "tags": [f"team:{record.get('team_id')}", f"provider:{record.get('provider')}"],
                },
            }
        )
        events.append(
            {
                "id": str(uuid.uuid4()),
                "type": "generation-create",
                "timestamp": ts,
                "body": {
                    "id": str(uuid.uuid4()),
                    "traceId": trace_id,
                    "name": f"{record.get('provider')}-call",
                    "model": record.get("model"),
                    "startTime": ts,
                    "usage": {
                        "input": record.get("prompt_token_count", 0),
                        "output": record.get("completion_token_count", 0),
                        "total": (
                            int(record.get("prompt_token_count", 0))
                            + int(record.get("completion_token_count", 0))
                        ),
                        "unit": "TOKENS",
                    },
                    "metadata": {
                        "latency_ms": record.get("latency_ms"),
                        "corp_llm_latency_ms": record.get("corp_llm_latency_ms"),
                        "pre_pass_latency_ms": record.get("pre_pass_latency_ms"),
                    },
                },
            }
        )
    return {"batch": events}
