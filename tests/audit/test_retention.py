from corp_llm_gateway.audit import lifecycle_configuration, lifecycle_rule_for
from corp_llm_gateway.team_config import TeamConfig


def _team(team_id: str = "t1", **overrides: object) -> TeamConfig:
    base: dict[str, object] = {"team_id": team_id, "name": f"Team {team_id}"}
    base.update(overrides)
    return TeamConfig(**base)  # type: ignore[arg-type]


def test_default_team_lifecycle_rule_shape() -> None:
    rule = lifecycle_rule_for(_team("t1"))
    assert rule["ID"] == "corp-llm-gateway-t1"
    assert rule["Status"] == "Enabled"
    assert rule["Filter"] == {"Prefix": "t1/"}


def test_default_retention_transitions_at_90_days() -> None:
    rule = lifecycle_rule_for(_team("t1"))
    assert rule["Transitions"][0]["Days"] == 90
    assert rule["Transitions"][0]["StorageClass"] == "GLACIER"


def test_default_retention_expires_after_7_years() -> None:
    rule = lifecycle_rule_for(_team("t1"))
    assert rule["Expiration"]["Days"] == 90 + 7 * 365


def test_team_overrides_retention_values() -> None:
    rule = lifecycle_rule_for(
        _team("t-short", retention_hot_days=30, retention_cold_years=1)
    )
    assert rule["Transitions"][0]["Days"] == 30
    assert rule["Expiration"]["Days"] == 30 + 365


def test_per_team_prefix_scoped() -> None:
    rule = lifecycle_rule_for(_team("team-x"))
    assert rule["Filter"]["Prefix"] == "team-x/"


def test_zero_hot_days_clamped_to_one() -> None:
    rule = lifecycle_rule_for(_team("t1", retention_hot_days=0))
    assert rule["Transitions"][0]["Days"] >= 1


def test_lifecycle_configuration_one_rule_per_team() -> None:
    cfg = lifecycle_configuration((_team("a"), _team("b"), _team("c")))
    assert len(cfg["Rules"]) == 3
    ids = {r["ID"] for r in cfg["Rules"]}
    assert ids == {
        "corp-llm-gateway-a",
        "corp-llm-gateway-b",
        "corp-llm-gateway-c",
    }


def test_lifecycle_configuration_empty() -> None:
    cfg = lifecycle_configuration(())
    assert cfg == {"Rules": []}
