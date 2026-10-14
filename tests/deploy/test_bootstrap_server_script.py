"""Asserts for the day-0 server bootstrap script (plan Task 7 / D1).

`main` installs Docker and writes under `/opt` as root, so it is never run
here. The tests source the script and call single functions against a tmp_path —
the script only runs `main` when executed directly.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy" / "bootstrap-server.sh"


def test_help_exits_zero_without_touching_the_host() -> None:
    result = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "bootstrap-server.sh" in result.stderr


def test_unknown_flag_is_refused() -> None:
    result = subprocess.run([str(SCRIPT), "--wipe-everything"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "--wipe-everything" in result.stderr


def _call(snippet: str, target: Path) -> subprocess.CompletedProcess[str]:
    """Source the script and call one function against a throwaway target dir."""
    preamble = (
        f'source "{SCRIPT}"\n'
        f'SCRIPT_DIR="{target}"\n'
        f'TARGET_DIR="{target}"\n'
        f'ENV_FILE="{target}/.env"\n'
        f'ENV_EXAMPLE="{target}/.env.example"\n'
    )
    return subprocess.run(["bash", "-c", preamble + snippet], capture_output=True, text=True)


def test_seed_env_writes_0600_and_stops_for_editing(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("GATEWAY_PORT=4000\nPOSTGRES_PASSWORD=\n")

    result = _call("seed_env_file", tmp_path)

    env_file = tmp_path / ".env"
    assert result.returncode == 1, result.stderr
    assert env_file.exists()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert env_file.read_text() == (tmp_path / ".env.example").read_text()


def test_seed_env_keeps_an_existing_env_untouched(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("GATEWAY_PORT=4000\n")
    env_file = tmp_path / ".env"
    env_file.write_text("GATEWAY_PORT=9999\n")

    result = _call("seed_env_file", tmp_path)

    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == "GATEWAY_PORT=9999\n"


def test_seed_env_refuses_when_no_example_has_been_synced(tmp_path: Path) -> None:
    result = _call("seed_env_file", tmp_path)

    assert result.returncode == 1
    assert "no .env.example found" in result.stderr
    assert not (tmp_path / ".env").exists()


def test_target_dir_is_created_0750_and_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "deploy"

    first = _call("ensure_target_dir", target)
    (target / "keep-me").write_text("x")
    second = _call("ensure_target_dir", target)

    assert first.returncode == 0, first.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    assert second.returncode == 0, second.stderr
    assert (target / "keep-me").exists()


def test_relative_target_dir_is_refused(tmp_path: Path) -> None:
    # fatal() exits the shell, so the refusal is an exit code, not a return.
    result = _call("parse_args --dir relative/path", tmp_path)

    assert result.returncode == 1
    assert "must be an absolute path" in result.stderr


@pytest.mark.not_root
def test_running_as_non_root_is_refused(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(SCRIPT), "--dir", str(tmp_path / "deploy")], capture_output=True, text=True
    )

    assert result.returncode == 1
    assert "must run as root" in result.stderr
    assert not (tmp_path / "deploy").exists()
