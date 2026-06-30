from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Provider = Literal["anthropic", "openai"]
Status = Literal["ok", "failed", "degraded"]


@dataclass(frozen=True)
class AuditEvent:
    """Per-request audit event. Schema: docs/audit-schema.md.

    NEVER fields (mapping, original content, credentials) are NOT exposed
    here as attributes — callers cannot construct an event that violates
    the schema. The serializer at AuditLogger emits ALWAYS fields and the
    set CONDITIONAL fields; nothing else.
    """

    timestamp: datetime
    request_id: str
    user_id: str
    team_id: str
    provider: Provider
    model: str
    latency_ms: int
    prompt_token_count: int
    completion_token_count: int
    redaction_count: int
    finding_label_counts: dict[str, int] = field(default_factory=dict)
    cache_a_hit: bool = False
    status: Status = "ok"

    placeholder_list: tuple[str, ...] | None = None
    error_code: str | None = None
    block_reason: str | None = None
    corp_llm_latency_ms: int | None = None
    pre_pass_latency_ms: int | None = None
    audit_buffer_full: bool | None = None
