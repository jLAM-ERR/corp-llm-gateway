"""Asserts for the day-N deploy script (plan Task 8 / D2).

The script drives a production server over SSH, so it is never pointed at a
real host here. Three layers: content asserts on the script text (strict mode,
the repo's stderr helpers, no destructive flag, no committed secret), function
level tests that source the script against a tmp_path, and end-to-end runs of a
copy of the script inside a fake checkout with `ssh`, `rsync` and `docker`
stubs on PATH. The rsync stub calls the real rsync into a local directory, so
the ".env is never synced" claims are semantic, not textual.
"""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy" / "deploy.sh"
BOOTSTRAP = ROOT / "scripts" / "deploy" / "bootstrap-server.sh"

HOST = "deploy@example.test"

HEALTHY_PS = [
    {"Service": "litellm", "State": "running", "Health": "healthy"},
    {"Service": "postgres", "State": "running", "Health": "healthy"},
    {"Service": "vector", "State": "running", "Health": ""},
]
UNHEALTHY_PS = [
    {"Service": "litellm", "State": "restarting", "Health": "unhealthy"},
]
STARTING_PS = [
    {"Service": "litellm", "State": "running", "Health": "starting"},
    {"Service": "postgres", "State": "running", "Health": "healthy"},
]
NO_HEALTHCHECK_PS = [
    {"Service": "vector", "State": "running", "Health": ""},
]


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text()


def _function_body(text: str, name: str) -> str:
    match = re.search(rf"^{re.escape(name)}\(\)\s*\{{\n(.*?)^\}}$", text, re.S | re.M)
    assert match is not None, f"no shell function named {name!r}"
    return match.group(1)


# --------------------------------------------------------------------------- #
# stubs
# --------------------------------------------------------------------------- #


def _stub_bin(tmp_path: Path, *, ssh_mode: str = "exec") -> Path:
    """Build a PATH directory with ssh / rsync / docker stubs.

    ssh_mode="exec" runs the remote command locally (so mkdir-based locking and
    the remote probes keep their real semantics); ssh_mode="ps" answers every
    command with $FAKE_PS_JSON.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    if ssh_mode == "exec":
        ssh_body = 'shift\nexec bash -c "$*"\n'
    else:
        ssh_body = "printf '%s\\n' \"${FAKE_PS_JSON:-[]}\"\n"
    _write_stub(bin_dir / "ssh", 'printf \'%s\\n\' "$*" >> "$SSH_LOG"\n' + ssh_body)

    _write_stub(
        bin_dir / "rsync",
        'printf \'%s\\n\' "$@" >> "$RSYNC_LOG"\n'
        "args=()\n"
        'for a in "$@"; do args+=("${a/#$RSYNC_HOST_PREFIX/}"); done\n'
        'exec "$RSYNC_REAL" "${args[@]}"\n',
    )

    _write_stub(
        bin_dir / "docker",
        'printf \'%s\\n\' "$*" >> "$DOCKER_LOG"\n'
        'for a in "$@"; do\n'
        '    if [ "$a" = "ps" ]; then printf \'%s\\n\' "${FAKE_PS_JSON:-[]}"; exit 0; fi\n'
        "done\n",
    )
    return bin_dir


def _write_stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


def _env(tmp_path: Path, bin_dir: Path, ps: list[dict[str, str]] | str | None = None) -> dict:
    import os

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SSH_LOG"] = str(tmp_path / "ssh.log")
    env["RSYNC_LOG"] = str(tmp_path / "rsync.log")
    env["DOCKER_LOG"] = str(tmp_path / "docker.log")
    real_rsync = shutil.which("rsync", path="/usr/bin:/bin:/usr/local/bin")
    env["RSYNC_REAL"] = real_rsync or "/usr/bin/rsync"
    env["RSYNC_HOST_PREFIX"] = f"{HOST}:"
    env["FAKE_PS_JSON"] = ps if isinstance(ps, str) else json.dumps(ps or [])
    return env


def _log(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    return path.read_text() if path.exists() else ""


def _call(
    snippet: str,
    tmp_path: Path,
    *,
    remote_dir: Path | None = None,
    compose_dir: Path | None = None,
    ssh_mode: str = "exec",
    ps: list[dict[str, str]] | str | None = None,
    extra: str = "",
    script: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Source the script and call one function against throwaway directories."""
    bin_dir = _stub_bin(tmp_path, ssh_mode=ssh_mode)
    preamble = (
        f'source "{script or SCRIPT}"\n'
        f'HOST="{HOST}"\n'
        f'REMOTE_DIR="{remote_dir or tmp_path / "remote"}"\n'
        f'LOCK_DIR="{remote_dir or tmp_path / "remote"}/.deploy.lock"\n'
        f'COMPOSE_DIR="{compose_dir or tmp_path / "compose"}"\n'
        f"{extra}"
    )
    return subprocess.run(
        ["bash", "-c", preamble + snippet],
        capture_output=True,
        text=True,
        env=_env(tmp_path, bin_dir, ps),
    )


def _fake_repo(tmp_path: Path) -> Path:
    """A minimal checkout: a copy of the script, a compose tree, the schema."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "deploy").mkdir(parents=True)
    copy = repo / "scripts" / "deploy" / "deploy.sh"
    shutil.copy2(SCRIPT, copy)

    compose = repo / "compose"
    (compose / "postgres" / "initdb").mkdir(parents=True)
    (compose / "certs").mkdir(parents=True)
    (compose / ".env").write_text("POSTGRES_PASSWORD=laptop-placeholder\n")
    (compose / ".env.example").write_text("POSTGRES_PASSWORD=\n")
    (compose / "docker-compose.yml").write_text("services: {}\n")
    (compose / "docker-compose.build.yml").write_text("services: {}\n")
    (compose / "certs" / "server.key").write_text("-----BEGIN PRIVATE KEY-----\n")
    (compose / "postgres" / "initdb" / "README.md").write_text("staged at deploy time\n")

    tokens = repo / "src" / "corp_llm_gateway" / "tokens"
    tokens.mkdir(parents=True)
    (tokens / "schema.sql").write_text("CREATE TABLE corp_tokens (id text);\n")
    return repo


def _remote_dir(tmp_path: Path, *, with_env: bool = True) -> Path:
    remote = tmp_path / "remote"
    remote.mkdir(exist_ok=True)
    if with_env:
        (remote / ".env").write_text("POSTGRES_PASSWORD=real-server-secret\n")
    return remote


# --------------------------------------------------------------------------- #
# content asserts
# --------------------------------------------------------------------------- #


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


def test_help_documents_every_subcommand_and_the_log_warning() -> None:
    result = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "deploy.sh" in result.stderr
    for subcommand in ("up", "down", "restart", "logs", "status"):
        assert re.search(rf"^\s+{subcommand}\b", result.stderr, re.M), f"{subcommand} undocumented"
    assert "--host" in result.stderr
    assert re.search(r"(?i)logs.*(may contain|whatever the stack logs)", result.stderr), (
        "help must warn that remote logs carry whatever the stack logs"
    )


def test_missing_host_is_refused() -> None:
    result = subprocess.run([str(SCRIPT), "status"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "--host" in result.stderr


def test_unknown_option_and_unknown_subcommand_are_refused() -> None:
    for argv in (["--host", HOST, "--wipe-everything", "up"], ["--host", HOST, "nuke"]):
        result = subprocess.run([str(SCRIPT), *argv], capture_output=True, text=True)
        assert result.returncode == 1, result.stdout
        assert argv[-1] in result.stderr or argv[2] in result.stderr


def test_remote_dir_default_matches_bootstrap_server(script_text: str) -> None:
    mine = re.search(r'^REMOTE_DIR="\$\{[A-Z_]+:-(?P<dir>[^}]+)\}"', script_text, re.M)
    theirs = re.search(r'^TARGET_DIR="\$\{[A-Z_]+:-(?P<dir>[^}]+)\}"', BOOTSTRAP.read_text(), re.M)
    assert mine is not None and theirs is not None
    assert mine.group("dir") == theirs.group("dir") == "/opt/corp-llm-gateway"


def test_only_the_production_compose_entrypoint_is_used(script_text: str) -> None:
    assert re.search(r'^COMPOSE_FILE="docker-compose\.yml"', script_text, re.M)
    build_overlay = [
        line
        for line in script_text.splitlines()
        if "docker-compose.build.yml" in line and not line.strip().startswith("#")
    ]
    for line in build_overlay:
        assert "exclude" in line, f"the dev-only build overlay must never be deployed: {line!r}"
    # The compose v1 binary, not the plugin package or a .yml filename.
    v1 = re.search(r"\bdocker-compose\b(?!-plugin)(?!\.(?:build\.)?ya?ml)", script_text)
    assert v1 is None, f"compose v1 (`docker-compose`) is not supported: {v1}"


def _code_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def test_no_destructive_flag_anywhere(script_text: str) -> None:
    code = _code_lines(script_text)
    assert not re.search(r"\brm\s+-[a-z]*[rR]", code), "no recursive delete"
    assert "--delete" not in code, "rsync --delete could remove the server's .env or certs"
    assert not re.search(r"docker compose[^\n]*\bdown\b[^\n]*\s-v\b", code), (
        "never remove production volumes"
    )
    assert "down --volumes" not in code
    assert "prune" not in code


def test_env_file_contents_are_never_read_or_printed(script_text: str) -> None:
    for line in script_text.splitlines():
        stripped = line.strip()
        if ".env" not in stripped or stripped.startswith("#"):
            continue
        reader = r"^(source|\.|cat|echo|printf|grep|sed|awk)\s+[^|]*\.env\b"
        assert not re.match(reader, stripped), f"must not print .env values: {stripped!r}"


def test_no_secret_or_real_host_is_committed(script_text: str) -> None:
    assert not re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", script_text), "script has an IP literal"
    assert not re.search(r"\.(corp|lan|internal)\b", script_text), "script has an internal host"
    credential = r"(?i)\b(password|secret|api_key|token)\s*=\s*[\"']?[A-Za-z0-9]"
    secret = re.search(credential, script_text)
    assert secret is None, f"script has a credential-looking assignment: {secret}"


def test_healthcheck_polling_mirrors_demo_sh(script_text: str) -> None:
    body = _function_body(script_text, "wait_for_healthcheck")
    assert "local max_wait=" in body
    assert "local elapsed=0" in body
    assert "local interval=" in body
    assert "(( elapsed < max_wait ))" in body
    assert "(( elapsed += interval ))" in body


def test_pull_gates_the_up(script_text: str) -> None:
    assert re.search(r"pull\s*&&\s*docker compose[^\n]*up -d", script_text), (
        "a failed pull must abort before `up -d`"
    )


# --------------------------------------------------------------------------- #
# argument validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--dir", "relative/path"], "absolute path"),
        (["--dir", "/opt/x; rm -rf /"], "may only contain"),
        (["--dir", "/opt/$(id)"], "may only contain"),
        (["--dir", "/"], "root"),
        (["--dir", "//"], "root"),
    ],
)
def test_bad_remote_dir_is_refused(argv: list[str], expected: str) -> None:
    result = subprocess.run(
        [str(SCRIPT), "--host", HOST, *argv, "status"], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert expected in result.stderr


@pytest.mark.parametrize("host", ["a b", "h;rm -rf /", "$(id)@host"])
def test_bad_host_is_refused(host: str) -> None:
    result = subprocess.run([str(SCRIPT), "--host", host, "status"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "--host" in result.stderr


# --------------------------------------------------------------------------- #
# schema staging
# --------------------------------------------------------------------------- #


def test_schema_is_staged_from_source_before_sync(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    script = repo / "scripts" / "deploy" / "deploy.sh"

    result = _call("stage_schema", tmp_path, compose_dir=repo / "compose", script=script)

    staged = repo / "compose" / "postgres" / "initdb" / "01-schema.sql"
    assert result.returncode == 0, result.stderr
    assert staged.read_text() == "CREATE TABLE corp_tokens (id text);\n"


def test_missing_schema_source_is_fatal(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    (repo / "src" / "corp_llm_gateway" / "tokens" / "schema.sql").unlink()
    script = repo / "scripts" / "deploy" / "deploy.sh"

    result = _call("stage_schema", tmp_path, compose_dir=repo / "compose", script=script)

    assert result.returncode == 1
    assert "schema.sql" in result.stderr
    assert not (repo / "compose" / "postgres" / "initdb" / "01-schema.sql").exists()


# --------------------------------------------------------------------------- #
# rsync — the two named hazards
# --------------------------------------------------------------------------- #


def test_sync_never_uploads_the_local_env_and_never_clobbers_the_servers(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)
    (repo / "compose" / "postgres" / "initdb" / "01-schema.sql").write_text("CREATE TABLE t();\n")

    result = _call(
        "sync_compose",
        tmp_path,
        remote_dir=remote,
        compose_dir=repo / "compose",
        script=repo / "scripts" / "deploy" / "deploy.sh",
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert (remote / ".env").read_text() == "POSTGRES_PASSWORD=real-server-secret\n"
    assert (remote / "docker-compose.yml").exists()
    assert (remote / ".env.example").exists(), "the example is needed by bootstrap-server.sh"
    assert (remote / "postgres" / "initdb" / "01-schema.sql").exists()
    assert not (remote / "docker-compose.build.yml").exists(), "dev-only overlay must not deploy"
    assert not (remote / "certs" / "server.key").exists(), "local key material must not be uploaded"
    rsync_args = _log(tmp_path, "rsync.log")
    assert "--exclude=.env\n" in rsync_args
    assert "--delete" not in rsync_args


def test_sync_refuses_if_the_env_exclude_is_ever_dropped(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _call(
        'assert_env_excluded --archive --exclude=".DS_Store"',
        tmp_path,
        remote_dir=remote,
        compose_dir=repo / "compose",
        script=repo / "scripts" / "deploy" / "deploy.sh",
    )

    assert result.returncode == 1
    assert ".env" in result.stderr


# --------------------------------------------------------------------------- #
# remote preconditions
# --------------------------------------------------------------------------- #


def test_missing_remote_dir_points_at_the_bootstrap_script(tmp_path: Path) -> None:
    result = _call("ensure_remote_ready", tmp_path, remote_dir=tmp_path / "absent")

    assert result.returncode == 1
    assert "bootstrap-server.sh" in result.stderr
    assert _log(tmp_path, "rsync.log") == "", "nothing may be synced to an unprepared host"


def test_missing_remote_env_is_fatal_and_never_uploaded(tmp_path: Path) -> None:
    remote = _remote_dir(tmp_path, with_env=False)

    result = _call("ensure_remote_ready", tmp_path, remote_dir=remote)

    assert result.returncode == 1
    assert ".env" in result.stderr
    assert not (remote / ".env").exists(), "the script must never create the server's .env"


def test_ready_remote_passes(tmp_path: Path) -> None:
    remote = _remote_dir(tmp_path)

    result = _call("ensure_remote_ready", tmp_path, remote_dir=remote)

    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# locking
# --------------------------------------------------------------------------- #


def test_a_second_concurrent_deploy_is_refused(tmp_path: Path) -> None:
    remote = _remote_dir(tmp_path)

    first = _call("acquire_lock", tmp_path, remote_dir=remote)
    second = _call("acquire_lock", tmp_path, remote_dir=remote)

    assert first.returncode == 0, first.stderr
    assert (remote / ".deploy.lock").is_dir()
    assert second.returncode == 1
    assert "another deploy" in second.stderr.lower()
    assert (remote / ".deploy.lock").is_dir(), "a refused run must not steal the lock"


def test_lock_owner_tag_cannot_inject_a_remote_command(tmp_path: Path) -> None:
    # The owner tag is built from local `id`/`hostname` output and lands inside
    # a remote command string.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_stub(bin_dir / "hostname", f"printf '%s' \"laptop'; touch {tmp_path}/pwned; echo '\"\n")
    remote = _remote_dir(tmp_path)

    result = _call("acquire_lock", tmp_path, remote_dir=remote)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "pwned").exists(), "the owner tag must not reach the remote shell raw"
    owner = (remote / ".deploy.lock" / "owner").read_text()
    assert "'" not in owner and ";" not in owner


def test_release_lock_is_a_noop_when_not_held(tmp_path: Path) -> None:
    remote = _remote_dir(tmp_path)
    (remote / ".deploy.lock").mkdir()

    result = _call("release_lock", tmp_path, remote_dir=remote)

    assert result.returncode == 0, result.stderr
    assert (remote / ".deploy.lock").is_dir(), "only the holder may release the lock"


def test_force_unlock_clears_a_stale_lock(tmp_path: Path) -> None:
    remote = _remote_dir(tmp_path)
    (remote / ".deploy.lock").mkdir()
    (remote / ".deploy.lock" / "owner").write_text("someone\n")

    result = _call("release_lock", tmp_path, remote_dir=remote, extra="LOCK_HELD=1\n")

    assert result.returncode == 0, result.stderr
    assert not (remote / ".deploy.lock").exists()


# --------------------------------------------------------------------------- #
# health polling / status
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("shape", ["array", "lines"])
def test_healthy_stack_stops_polling(tmp_path: Path, shape: str) -> None:
    payload = (
        json.dumps(HEALTHY_PS)
        if shape == "array"
        else "\n".join(json.dumps(row) for row in HEALTHY_PS)
    )

    result = _call(
        "wait_for_healthcheck",
        tmp_path,
        ssh_mode="ps",
        ps=payload,
        extra="HEALTH_MAX_WAIT=5\nHEALTH_INTERVAL=1\n",
    )

    assert result.returncode == 0, result.stderr


def test_unhealthy_stack_fails_after_the_timeout(tmp_path: Path) -> None:
    result = _call(
        "wait_for_healthcheck",
        tmp_path,
        ssh_mode="ps",
        ps=UNHEALTHY_PS,
        extra="HEALTH_MAX_WAIT=1\nHEALTH_INTERVAL=1\n",
    )

    assert result.returncode == 1
    assert "litellm" in result.stderr, "the stuck service must be named"


def test_a_starting_healthcheck_is_not_mistaken_for_healthy(tmp_path: Path) -> None:
    # `state=running health=starting` on the first poll used to end the wait, so a
    # service that flipped to unhealthy a second later still released the lock on a
    # "successful" deploy.
    result = _call(
        "wait_for_healthcheck",
        tmp_path,
        ssh_mode="ps",
        ps=STARTING_PS,
        extra="HEALTH_MAX_WAIT=1\nHEALTH_INTERVAL=1\n",
    )

    assert result.returncode == 1
    assert "litellm" in result.stderr, "the service still starting must be named"
    assert "postgres" not in result.stderr.split("stuck:")[-1]


def test_a_service_without_a_healthcheck_is_healthy_when_running(tmp_path: Path) -> None:
    # The fallback the stricter rule must keep: compose reports an empty Health for
    # a service that declares no healthcheck.
    result = _call(
        "wait_for_healthcheck",
        tmp_path,
        ssh_mode="ps",
        ps=NO_HEALTHCHECK_PS,
        extra="HEALTH_MAX_WAIT=5\nHEALTH_INTERVAL=1\n",
    )

    assert result.returncode == 0, result.stderr


def test_empty_ps_output_is_not_mistaken_for_healthy(tmp_path: Path) -> None:
    result = _call(
        "wait_for_healthcheck",
        tmp_path,
        ssh_mode="ps",
        ps="[]",
        extra="HEALTH_MAX_WAIT=1\nHEALTH_INTERVAL=1\n",
    )

    assert result.returncode == 1


def test_status_summary_lists_services_without_env_values(tmp_path: Path) -> None:
    result = _call("print_status", tmp_path, ssh_mode="ps", ps=HEALTHY_PS)

    assert result.returncode == 0, result.stderr
    combined = result.stdout + result.stderr
    assert "litellm" in combined and "healthy" in combined
    assert "PASSWORD" not in combined and "secret" not in combined.lower()


# --------------------------------------------------------------------------- #
# end-to-end runs against the stubs
# --------------------------------------------------------------------------- #


def _run(repo: Path, tmp_path: Path, argv: list[str], ps: object = None, stdin: str = "") -> object:
    bin_dir = _stub_bin(tmp_path, ssh_mode="exec")
    return subprocess.run(
        [str(repo / "scripts" / "deploy" / "deploy.sh"), *argv],
        capture_output=True,
        text=True,
        input=stdin,
        env=_env(tmp_path, bin_dir, ps),  # type: ignore[arg-type]
    )


def test_up_stages_syncs_pulls_and_reports(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "up"],
        ps=HEALTHY_PS,
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    assert (repo / "compose" / "postgres" / "initdb" / "01-schema.sql").exists()
    assert (remote / "postgres" / "initdb" / "01-schema.sql").exists()
    assert (remote / ".env").read_text() == "POSTGRES_PASSWORD=real-server-secret\n"
    docker_log = _log(tmp_path, "docker.log")
    assert "compose -f docker-compose.yml pull" in docker_log
    assert "compose -f docker-compose.yml up -d" in docker_log
    assert not (remote / ".deploy.lock").exists(), "the lock must be released on exit"


def test_up_on_an_unprepared_host_changes_nothing(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)

    result = _run(repo, tmp_path, ["--host", HOST, "--dir", str(tmp_path / "absent"), "up"])

    assert result.returncode == 1  # type: ignore[attr-defined]
    assert "bootstrap-server.sh" in result.stderr  # type: ignore[attr-defined]
    assert "compose" not in _log(tmp_path, "docker.log")


def test_down_needs_confirmation_and_never_removes_volumes(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    refused = _run(repo, tmp_path, ["--host", HOST, "--dir", str(remote), "down"])
    after_refusal = _log(tmp_path, "docker.log")
    confirmed = _run(repo, tmp_path, ["--host", HOST, "--dir", str(remote), "--yes", "down"])

    assert refused.returncode == 1  # type: ignore[attr-defined]
    assert "--yes" in refused.stderr  # type: ignore[attr-defined]
    assert "down" not in after_refusal, "an unconfirmed run must not touch the stack"
    assert confirmed.returncode == 0, confirmed.stderr  # type: ignore[attr-defined]
    assert "compose -f docker-compose.yml down" in _log(tmp_path, "docker.log")
    assert " -v" not in _log(tmp_path, "docker.log")


def test_restart_without_a_service_argument_works(tmp_path: Path) -> None:
    # Empty-array expansion under `set -u` on bash 3.2, the macOS default.
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "--yes", "restart"],
        ps=HEALTHY_PS,
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    assert "compose -f docker-compose.yml restart" in _log(tmp_path, "docker.log")


def test_a_service_argument_that_could_inject_a_command_is_refused(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(repo, tmp_path, ["--host", HOST, "--dir", str(remote), "logs", "litellm;id"])

    assert result.returncode == 1  # type: ignore[attr-defined]
    assert "service names" in result.stderr  # type: ignore[attr-defined]
    assert _log(tmp_path, "docker.log") == ""


def test_dry_run_touches_nothing_on_the_remote(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "--dry-run", "up"],
        ps=HEALTHY_PS,
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    assert not (remote / "docker-compose.yml").exists(), "dry-run must not transfer files"
    assert "up -d" not in _log(tmp_path, "docker.log")
