import pytest

from corp_llm_gateway.cli.admin import main


@pytest.fixture(autouse=True)
def _bypass_rbac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_GATEWAY_RBAC", "0")


def test_team_create(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["team", "create", "--team-id", "t1", "--name", "Team One"])
    assert rc == 0
    assert "team.create team_id=t1 name=Team One" in capsys.readouterr().out


def test_team_set_rules(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["team", "set-rules", "--team-id", "t1", "--from-file", "rules.md"])
    assert rc == 0
    assert "team.set_rules team_id=t1 from_file=rules.md" in capsys.readouterr().out


def test_team_set_retention_defaults(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["team", "set-retention", "--team-id", "t1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "team.set_retention team_id=t1 hot_days=90 cold_years=7" in out


def test_team_set_retention_overrides(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["team", "set-retention", "--team-id", "t1", "--hot-days", "30", "--cold-years", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "team.set_retention team_id=t1 hot_days=30 cold_years=1" in out


def test_token_revoke(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["token", "revoke", "--user", "alice"])
    assert rc == 0
    assert "token.revoke user=alice" in capsys.readouterr().out


def test_missing_required_arg_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["team", "create", "--team-id", "t1"])
    assert excinfo.value.code != 0


def test_no_command_errors() -> None:
    with pytest.raises(SystemExit):
        main([])
