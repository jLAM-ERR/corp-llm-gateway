from dataclasses import dataclass, field
from typing import Literal

DEFAULT_RETENTION_HOT_DAYS = 90
DEFAULT_RETENTION_COLD_YEARS = 7

# Single source of truth for the M4 fail-policy vocabulary.
FailPolicy = Literal["fail-closed", "continue"]
AuditSinkDownPolicy = FailPolicy
AuditBufferFullPolicy = FailPolicy
PrePassDownPolicy = FailPolicy


@dataclass(frozen=True)
class FailPolicyOverrides:
    """Per-team overrides on the fail-policy matrix from M4.

    Only the columns the matrix marks as override-allowed are exposed.
    Defaults match the matrix default; teams may relax the audit-related
    paths to `continue` if their compliance posture allows it.
    """

    pre_pass_down: PrePassDownPolicy = "continue"
    audit_sink_down: AuditSinkDownPolicy = "continue"
    audit_buffer_full: AuditBufferFullPolicy = "fail-closed"


@dataclass(frozen=True)
class TeamConfig:
    team_id: str
    name: str
    replace_md_path: str | None = None
    # Ordered profile set this team selects (resolver expands `extends`).
    # Empty == today's behavior: no profile layers.
    profile_ids: tuple[str, ...] = ()
    retention_hot_days: int = DEFAULT_RETENTION_HOT_DAYS
    retention_cold_years: int = DEFAULT_RETENTION_COLD_YEARS
    fail_policy: FailPolicyOverrides = field(default_factory=FailPolicyOverrides)
