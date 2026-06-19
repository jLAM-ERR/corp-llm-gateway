import json
from pathlib import Path
from unittest.mock import patch

import pytest

from corp_llm_gateway.cli.status import _gather_status, main


@pytest.fixture(autouse=True)
def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


def test_no_token_file_unhealthy(tmp_path: Path) -> None:
    info = _gather_status(
        gateway_url="https://x",
        token_file=tmp_path / "nope",
        version_file=tmp_path / "nope-v",
    )
    assert info["token_present"] is False
    assert info["healthy"] is False


def test_token_present_but_gateway_down_unhealthy(tmp_path: Path) -> None:
    token = tmp_path / "token"
    token.write_text("ct_xyz")
    info = _gather_status(
        gateway_url="http://127.0.0.1:1",  # nothing listening
        token_file=token,
        version_file=tmp_path / "VERSION",
    )
    assert info["token_present"] is True
    assert info["live"] is False
    assert info["healthy"] is False


def test_version_file_read(tmp_path: Path) -> None:
    (tmp_path / "VERSION").write_text("v1.2.3\n")
    (tmp_path / "token").write_text("ct_xyz")
    info = _gather_status(
        gateway_url="http://127.0.0.1:1",
        token_file=tmp_path / "token",
        version_file=tmp_path / "VERSION",
    )
    assert info["version"] == "v1.2.3"


def test_main_returns_nonzero_when_unhealthy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(
        [
            "--gateway-url",
            "http://127.0.0.1:1",
            "--token-file",
            str(tmp_path / "missing"),
            "--version-file",
            str(tmp_path / "missing-v"),
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "unhealthy" in out


def test_main_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "--json",
            "--gateway-url",
            "http://127.0.0.1:1",
            "--token-file",
            str(tmp_path / "missing"),
            "--version-file",
            str(tmp_path / "missing-v"),
        ]
    )
    assert rc == 1
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["healthy"] is False
    assert parsed["live"] is False


def test_main_healthy_when_live_and_token_present(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "token").write_text("ct_xyz")
    with patch("corp_llm_gateway.cli.status._probe_live", return_value=True):
        rc = main(
            [
                "--gateway-url",
                "https://gateway.example",
                "--token-file",
                str(tmp_path / "token"),
                "--version-file",
                str(tmp_path / "missing-v"),
            ]
        )
    assert rc == 0
    out = capsys.readouterr().out
    assert "healthy" in out


def test_update_check_reports_newer_version(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("ct_xyz")
    (tmp_path / "VERSION").write_text("v0.0.1\n")
    with (
        patch("corp_llm_gateway.cli.status._probe_live", return_value=True),
        patch("corp_llm_gateway.cli.status._fetch_latest_version", return_value="v0.0.2"),
    ):
        info = _gather_status(
            gateway_url="https://x",
            token_file=tmp_path / "token",
            version_file=tmp_path / "VERSION",
            update_check=True,
            latest_version_url="https://x/VERSION",
        )
    assert info["latest_version"] == "v0.0.2"
    assert info["update_available"] is True


def test_update_check_no_update_when_same_version(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("ct_xyz")
    (tmp_path / "VERSION").write_text("v0.0.1\n")
    with (
        patch("corp_llm_gateway.cli.status._probe_live", return_value=True),
        patch("corp_llm_gateway.cli.status._fetch_latest_version", return_value="v0.0.1"),
    ):
        info = _gather_status(
            gateway_url="https://x",
            token_file=tmp_path / "token",
            version_file=tmp_path / "VERSION",
            update_check=True,
            latest_version_url="https://x/VERSION",
        )
    assert info["update_available"] is False


def test_update_check_disabled_no_fetch(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("ct_xyz")
    (tmp_path / "VERSION").write_text("v0.0.1\n")
    with (
        patch("corp_llm_gateway.cli.status._probe_live", return_value=True),
        patch("corp_llm_gateway.cli.status._fetch_latest_version") as mock_fetch,
    ):
        info = _gather_status(
            gateway_url="https://x",
            token_file=tmp_path / "token",
            version_file=tmp_path / "VERSION",
            update_check=False,
            latest_version_url="https://x/VERSION",
        )
    mock_fetch.assert_not_called()
    assert "latest_version" not in info
