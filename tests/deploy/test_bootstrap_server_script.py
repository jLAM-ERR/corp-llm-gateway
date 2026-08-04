"""Asserts for the day-0 server bootstrap script (plan Task 7 / D1).

`main` installs Docker and writes under `/opt` as root, so it is never run
here. Two layers instead: content asserts on the script text (strict mode, the
repo's stderr helpers, no destructive command, no committed secret), and
behaviour tests that source the script and call single functions against a
tmp_path — the script only runs `main` when executed directly.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy" / "bootstrap-server.sh"
UNIT = ROOT / "scripts" / "deploy" / "corp-llm-gateway.service"

SUPPORTED_DISTRO_IDS = ("debian", "ubuntu", "rhel", "centos", "rocky", "almalinux", "fedora")


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text()


@pytest.fixture(scope="module")
def unit_text() -> str:
    return UNIT.read_text()


def _function_body(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\)\s*\{{\n(.*?)^\}}$", text, re.S | re.M)
    assert match is not None, f"no shell function named {name!r}"
    return match.group(1)


def test_script_is_executable_bash_with_strict_mode(script_text: str) -> None:
    assert SCRIPT.exists(), f"{SCRIPT} does not exist"
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "script is not executable"
    assert script_text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in script_text


def test_defines_repo_standard_helpers_writing_to_stderr(script_text: str) -> None:
    for name in ("fatal", "warn", "info"):
        body = _function_body(script_text, name)
        assert ">&2" in body, f"{name}() must write to stderr like scripts/demo.sh"
    assert "exit 1" in _function_body(script_text, "fatal")


def test_bash_syntax_is_valid() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not on PATH")
def test_shellcheck_clean() -> None:
    result = subprocess.run(["shellcheck", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_help_exits_zero_without_touching_the_host() -> None:
    result = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "bootstrap-server.sh" in result.stderr


def test_unknown_flag_is_refused() -> None:
    result = subprocess.run([str(SCRIPT), "--wipe-everything"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "--wipe-everything" in result.stderr


def test_target_dir_defaults_to_opt_corp_llm_gateway(script_text: str) -> None:
    assert re.search(r'^TARGET_DIR="\$\{[A-Z_]+:-/opt/corp-llm-gateway\}"', script_text, re.M)


def test_requires_root(script_text: str) -> None:
    body = _function_body(script_text, "require_root")
    assert "EUID" in body or "id -u" in body
    assert "fatal" in body


def test_env_is_seeded_from_example_at_0600_then_exits_one(script_text: str) -> None:
    body = _function_body(script_text, "seed_env_file")
    assert ".env.example" in body or "ENV_EXAMPLE" in body
    assert re.search(r"(install -m 0600|chmod 0?600)", body), "seeded .env must be mode 0600"
    seed = re.search(r"(install -m 0600|cp )", body)
    stop = re.search(r"^\s*exit 1$", body, re.M)
    assert seed is not None and stop is not None
    assert stop.start() > seed.start(), "must exit 1 AFTER seeding, so the operator edits .env"


def test_existing_env_file_is_never_overwritten(script_text: str) -> None:
    body = _function_body(script_text, "seed_env_file")
    guard = re.search(r'if\s+\[\[\s+-f\s+"\$ENV_FILE"\s+\]\]', body)
    assert guard is not None, "seed_env_file must test for an existing .env first"
    for copy in re.finditer(r"^\s*(cp|install -m 0600|mv)\s", body, re.M):
        assert copy.start() > guard.start(), "no copy may run before the existence guard"


def test_no_destructive_command_anywhere(script_text: str) -> None:
    assert not re.search(r"\brm\s+-[a-z]*[rR]", script_text), "no recursive delete"
    assert "docker compose down" not in script_text, "bootstrap must not stop a running stack"
    assert "systemctl start" not in script_text, "never start the stack before .env is edited"


def test_existing_systemd_unit_needs_an_explicit_force_flag(script_text: str) -> None:
    body = _function_body(script_text, "install_systemd_unit")
    assert re.search(r'\[\[\s+-f\s+"\$SYSTEMD_UNIT_PATH"\s+\]\]', body)
    assert "$FORCE" in body, "overwriting an existing unit must require --force"
    assert "--force" in script_text
    assert "systemctl daemon-reload" in body
    assert "systemctl enable" in body


def test_systemd_unit_is_opt_in(script_text: str) -> None:
    assert "--systemd" in script_text
    assert re.search(r"^INSTALL_SYSTEMD=0$", script_text, re.M), "systemd unit is opt-in"


def test_unsupported_distro_is_refused_with_an_actionable_message(script_text: str) -> None:
    body = _function_body(script_text, "install_docker")
    assert "/etc/os-release" in script_text
    for distro_id in SUPPORTED_DISTRO_IDS:
        assert distro_id in body, f"{distro_id} must be handled explicitly"
    assert "docs.docker.com/engine/install" in body, "refusal must point at the official docs"
    assert "fatal" in body


def test_docker_daemon_wait_mirrors_demo_sh_polling_loop(script_text: str) -> None:
    body = _function_body(script_text, "wait_for_docker_daemon")
    assert "local max_wait=" in body
    assert "local elapsed=0" in body
    assert "local interval=" in body
    assert "(( elapsed < max_wait ))" in body
    assert "(( elapsed += interval ))" in body
    assert "fatal" in body


def test_compose_v2_is_verified_not_assumed(script_text: str) -> None:
    assert "docker compose version" in script_text
    # The v1 binary, not the `docker-compose-plugin` package or a .yml filename.
    v1 = re.search(r"\bdocker-compose\b(?!-plugin)(?!\.ya?ml)", script_text)
    assert v1 is None, f"compose v1 (`docker-compose`) is not supported: {v1}"


def test_env_file_contents_are_never_read_or_printed(script_text: str) -> None:
    for line in script_text.splitlines():
        stripped = line.strip()
        if "$ENV_FILE" not in stripped or stripped.startswith("#"):
            continue
        assert not re.match(r"^(source|\.|cat|echo|printf|grep|sed|awk)\s", stripped), (
            f"must not read or print .env values: {stripped!r}"
        )


def test_no_secret_or_real_host_is_committed(script_text: str, unit_text: str) -> None:
    for name, text in (("script", script_text), ("unit", unit_text)):
        assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text), f"{name} has an IP literal"
        assert not re.search(r"\.(corp|lan|internal)\b", text), f"{name} has an internal host"
        secret = re.search(r"(?i)\b(password|secret|api_key|token)\s*=\s*[\"']?[A-Za-z0-9]", text)
        assert secret is None, f"{name} has a credential-looking assignment: {secret}"


def test_systemd_unit_runs_compose_from_the_target_dir(unit_text: str) -> None:
    assert "Requires=docker.service" in unit_text
    assert "After=docker.service" in unit_text
    assert "WorkingDirectory=/opt/corp-llm-gateway" in unit_text
    assert "Type=oneshot" in unit_text
    assert "RemainAfterExit=yes" in unit_text
    assert re.search(r"^ExecStart=.*docker compose up -d$", unit_text, re.M)
    assert re.search(r"^ExecStop=.*docker compose down$", unit_text, re.M)
    assert "WantedBy=multi-user.target" in unit_text
    assert "Environment=" not in unit_text, "the unit must not carry env values; .env does"


def test_unit_working_directory_follows_a_custom_target_dir(script_text: str) -> None:
    body = _function_body(script_text, "install_systemd_unit")
    assert "WorkingDirectory" in body, "a non-default --dir must be substituted into the unit"


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


@pytest.mark.skipif(os.geteuid() == 0, reason="running as root would mutate this host")
def test_running_as_non_root_is_refused(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(SCRIPT), "--dir", str(tmp_path / "deploy")], capture_output=True, text=True
    )

    assert result.returncode == 1
    assert "must run as root" in result.stderr
    assert not (tmp_path / "deploy").exists()
