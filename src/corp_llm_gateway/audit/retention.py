"""S3 lifecycle policy generator (M3-7).

Each team's `team_config.retention_hot_days` and
`team_config.retention_cold_years` map to one S3 lifecycle rule. The
gateway emits records under `s3://{bucket}/{team_id}/...`, so the
prefix-scoped rule is the right granularity.
"""

from typing import Any

from corp_llm_gateway.team_config import TeamConfig


def lifecycle_rule_for(config: TeamConfig) -> dict[str, Any]:
    """One S3 lifecycle rule per team. AWS S3 LifecycleConfiguration
    JSON schema."""
    days_to_glacier = max(1, config.retention_hot_days)
    days_to_expire = days_to_glacier + config.retention_cold_years * 365
    return {
        "ID": f"corp-llm-gateway-{config.team_id}",
        "Status": "Enabled",
        "Filter": {"Prefix": f"{config.team_id}/"},
        "Transitions": [
            {
                "Days": days_to_glacier,
                "StorageClass": "GLACIER",
            }
        ],
        "Expiration": {"Days": days_to_expire},
    }


def lifecycle_configuration(configs: tuple[TeamConfig, ...]) -> dict[str, Any]:
    """A LifecycleConfiguration containing one rule per team config."""
    return {"Rules": [lifecycle_rule_for(c) for c in configs]}
