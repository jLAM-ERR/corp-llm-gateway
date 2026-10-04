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
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy" / "deploy.sh"
BOOTSTRAP = ROOT / "scripts" / "deploy" / "bootstrap-server.sh"

HOST = "deploy@example.test"

HEALTHY_PS = [
    {
        "Service": "litellm",
        "Name": "corp-llm-gateway-litellm-1",
        "State": "running",
        "Health": "healthy",
    },
    {
        "Service": "postgres",
        "Name": "corp-llm-gateway-postgres-1",
        "State": "running",
        "Health": "healthy",
    },
    {"Service": "vector", "Name": "corp-llm-gateway-vector-1", "State": "running", "Health": ""},
    # A `restart: "no"` one-shot that finished: `ps --all` keeps listing it.
    {
        "Service": "minio-init",
        "Name": "corp-llm-gateway-minio-init-1",
        "State": "exited",
        "Health": "",
        "ExitCode": 0,
    },
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
        'if [ -n "${COMPOSE_PROFILES+set}" ]; then\n'
        '    printf \'%s\\n\' "COMPOSE_PROFILES=${COMPOSE_PROFILES}" >> "$DOCKER_ENV_LOG"\n'
        "fi\n"
        'for a in "$@"; do\n'
        '    if [ "$a" = "ps" ]; then printf \'%s\\n\' "${FAKE_PS_JSON:-[]}"; exit 0; fi\n'
        '    if [ "$a" = "config" ]; then\n'
        '        [ -z "${FAKE_CONFIG_FAIL:-}" ] || exit 1\n'
        "        printf '%s\\n' \"$FAKE_SERVICES\"; exit 0\n"
        "    fi\n"
        "done\n",
    )
    return bin_dir


def _write_stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


def _env(tmp_path: Path, bin_dir: Path, ps: list[dict[str, str]] | str | None = None) -> dict:
    import os

    env = dict(os.environ)
    # The laptop's own value would reach the exec-mode ssh stub, which a real
    # ssh never forwards.
    env.pop("COMPOSE_PROFILES", None)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SSH_LOG"] = str(tmp_path / "ssh.log")
    env["RSYNC_LOG"] = str(tmp_path / "rsync.log")
    env["DOCKER_LOG"] = str(tmp_path / "docker.log")
    env["DOCKER_ENV_LOG"] = str(tmp_path / "docker-env.log")
    env["FAKE_SERVICES"] = "\n".join(row["Service"] for row in HEALTHY_PS)
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
    (compose / "docker-compose.oauth.yml").write_text("services: {}\n")
    (compose / "docker-compose.issuance.yml").write_text("services: {}\n")
    (compose / "gateway").mkdir()
    (compose / "gateway" / "config.toml.example").write_text("# example team map\n")
    (compose / "gateway" / "config.toml").write_text("# laptop copy\n")
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


@pytest.mark.requires_shellcheck
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
    mine = re.search(
        r'^REMOTE_DIR="\$\{[A-Z_]+:-(?:(?P<dir>[^$}]+)|\$(?P<var>[A-Z_]+))\}"', script_text, re.M
    )
    theirs = re.search(r'^TARGET_DIR="\$\{[A-Z_]+:-(?P<dir>[^}]+)\}"', BOOTSTRAP.read_text(), re.M)
    assert mine is not None and theirs is not None
    default = mine.group("dir")
    if default is None:
        named = re.search(rf'^{mine.group("var")}="(?P<dir>[^"]+)"', script_text, re.M)
        assert named is not None
        default = named.group("dir")
    assert default == theirs.group("dir") == "/opt/corp-llm-gateway"
    assignments = re.findall(r'^[A-Z_]+="[^"\n]*/opt/corp-llm-gateway', script_text, re.M)
    assert len(assignments) == 1, assignments


def test_only_the_production_compose_entrypoint_is_used(script_text: str) -> None:
    assert re.search(r'^COMPOSE_FILE="docker-compose\.yml"', script_text, re.M)
    build_overlay = [
        line
        for line in script_text.splitlines()
        if "docker-compose.build.yml" in line and not line.strip().startswith("#")
    ]
    for line in build_overlay:
        assert "exclude" in line, f"the dev-only build overlay must never be deployed: {line!r}"
    # The compose v1 binary, not the plugin package or a .yml filename. The
    # filename arm is generic (`docker-compose.<anything>.yml`) so adding an
    # overlay does not read as a v1 invocation.
    v1 = re.search(r"\bdocker-compose\b(?!-plugin)(?!(?:\.[A-Za-z0-9-]+)*\.ya?ml)", script_text)
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


def test_a_failed_one_shot_fails_at_once_and_names_it(tmp_path: Path) -> None:
    # Its `service_completed_successfully` dependents would sit `created` until the
    # timeout.
    ps = [
        {"Service": "litellm", "State": "running", "Health": "starting"},
        {"Service": "minio-init", "State": "exited", "Health": "", "ExitCode": 1},
    ]

    result = _call(
        "wait_for_healthcheck",
        tmp_path,
        ssh_mode="ps",
        ps=ps,
        extra="HEALTH_MAX_WAIT=10\nHEALTH_INTERVAL=1\n",
    )

    assert result.returncode == 1
    assert "minio-init exited with code 1; the services that wait for it" in result.stderr
    assert f"scripts/deploy/deploy.sh --host {HOST} logs minio-init\n" in result.stderr
    # One poll plus the status table.
    assert _log(tmp_path, "ssh.log").count("ps --all") == 2


def test_a_one_shot_without_an_exit_code_is_not_mistaken_for_done(tmp_path: Path) -> None:
    ps = [{"Service": "minio-init", "State": "exited", "Health": ""}]

    result = _call(
        "wait_for_healthcheck",
        tmp_path,
        ssh_mode="ps",
        ps=ps,
        extra="HEALTH_MAX_WAIT=1\nHEALTH_INTERVAL=1\n",
    )

    assert result.returncode == 1
    assert "within 1s — stuck: minio-init" in result.stderr


def _shell_array(text: str, name: str) -> list[str]:
    match = re.search(rf"^{re.escape(name)}=\((?P<items>[^)]*)\)$", text, re.M)
    assert match is not None, f"no shell array named {name!r}"
    return match.group("items").split()


def test_the_one_shot_list_is_every_restart_no_service(script_text: str) -> None:
    # Only a listed service counts as done once it exits 0, so a new `restart: "no"`
    # service must land here too, or every deploy times out waiting for it.
    base = ROOT / "compose" / "docker-compose.yml"
    # deploy.sh never syncs the build overlay, so only the production files count.
    skipped = {base.name, "docker-compose.build.yml"}
    overlays = sorted(
        path for path in base.parent.glob("docker-compose*.yml") if path.name not in skipped
    )
    restart: dict[str, object] = {}
    for path in (base, *overlays):
        services = (yaml.safe_load(path.read_text()) or {}).get("services") or {}
        for name, spec in services.items():
            if spec and "restart" in spec:
                restart[name] = spec["restart"]
    one_shots = {name for name, value in restart.items() if value in ("no", False)}

    assert one_shots == {"minio-init"}
    assert set(_shell_array(script_text, "ONE_SHOT_SERVICES")) == one_shots


@pytest.mark.parametrize(
    ("service", "state", "health", "exit_code", "done"),
    [
        ("minio-init", "exited", "", 0, True),
        # compose prints an empty Health for every exited container, one with a
        # healthcheck included, so only the one-shot list tells them apart.
        ("langfuse-web", "exited", "", 0, False),
        ("langfuse-web", "exited", "healthy", 0, False),
        ("langfuse-web", "exited", "unhealthy", 0, False),
        ("langfuse-web", "restarting", "", None, False),
        ("langfuse-web", "restarting", "healthy", None, False),
        ("langfuse-web", "running", "healthy", None, True),
    ],
)
def test_only_a_running_service_or_a_finished_one_shot_is_done(
    service: str,
    state: str,
    health: str,
    exit_code: int | None,
    done: bool,
    tmp_path: Path,
) -> None:
    row: dict[str, object] = {"Service": service, "State": state, "Health": health}
    if exit_code is not None:
        row["ExitCode"] = exit_code
    others = [r for r in HEALTHY_PS if r["Service"] != service]

    result = _call(
        "wait_for_healthcheck",
        tmp_path,
        ssh_mode="ps",
        ps=[*others, row],  # type: ignore[list-item]
        extra="HEALTH_MAX_WAIT=1\nHEALTH_INTERVAL=1\n",
    )

    if done:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1
        assert f"within 1s — stuck: {service} (state={state}" in result.stderr
        assert "exited with code" not in result.stderr


def test_a_failed_service_that_is_not_a_one_shot_fails_without_blaming_dependents(
    tmp_path: Path,
) -> None:
    ps = [*HEALTHY_PS, {"Service": "langfuse-web", "State": "exited", "Health": "", "ExitCode": 2}]

    result = _call(
        "wait_for_healthcheck",
        tmp_path,
        ssh_mode="ps",
        ps=ps,
        extra="HEALTH_MAX_WAIT=10\nHEALTH_INTERVAL=1\n",
    )

    assert result.returncode == 1
    assert "langfuse-web exited with code 2" in result.stderr
    assert "the services that wait for it" not in result.stderr
    assert f"scripts/deploy/deploy.sh --host {HOST} logs langfuse-web\n" in result.stderr


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        (["--mode", "virtual-keys", "--dir", "/x"], "--dir /x --mode virtual-keys"),
        (["--issuance", "--dir", "/x/"], "--dir /x --issuance"),
        (["--mode", "oauth", "--dir", "/opt/corp-llm-gateway"], ""),
    ],
    ids=["virtual-keys", "issuance", "defaults"],
)
@pytest.mark.parametrize(
    ("ps", "service"),
    [
        ([{"Service": "nginx", "State": "exited", "Health": ""}], "nginx"),
        ([{"Service": "minio-init", "State": "exited", "Health": "", "ExitCode": 3}], "minio-init"),
        ([{"Service": "litellm", "State": "running", "Health": "starting"}], ""),
    ],
    ids=["front-door", "one-shot", "timeout"],
)
def test_the_logs_hint_selects_the_same_stack(
    flags: list[str], expected: str, ps: list[dict[str, object]], service: str, tmp_path: Path
) -> None:
    # A `logs` run with a different file list or directory reads a different stack.
    argv = " ".join(["--host", HOST, *flags, "status"])

    result = _call(
        f"parse_args {argv}\nHEALTH_MAX_WAIT=1\nHEALTH_INTERVAL=1\nwait_for_healthcheck",
        tmp_path,
        ssh_mode="ps",
        ps=ps,  # type: ignore[arg-type]
    )

    assert result.returncode == 1
    parts = ("scripts/deploy/deploy.sh --host", HOST, expected, "logs", service)
    hint = " ".join(part for part in parts if part)
    assert f"{hint}\n" in result.stderr, result.stderr


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


def _run(
    repo: Path,
    tmp_path: Path,
    argv: list[str],
    ps: object = None,
    stdin: str = "",
    env_extra: dict[str, str] | None = None,
) -> object:
    bin_dir = _stub_bin(tmp_path, ssh_mode="exec")
    env = _env(tmp_path, bin_dir, ps)  # type: ignore[arg-type]
    env.update(env_extra or {})
    return subprocess.run(
        [str(repo / "scripts" / "deploy" / "deploy.sh"), *argv],
        capture_output=True,
        text=True,
        input=stdin,
        env=env,
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
    assert f"compose {BOTH_FILES} pull" in docker_log
    assert f"compose {BOTH_FILES} up -d" in docker_log
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
    assert f"compose {BOTH_FILES} down" in _log(tmp_path, "docker.log")
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
    assert f"compose {BOTH_FILES} restart" in _log(tmp_path, "docker.log")


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


# --------------------------------------------------------------------------- #
# --mode: which compose file list every remote call carries
# --------------------------------------------------------------------------- #

BOTH_FILES = "-f docker-compose.yml -f docker-compose.oauth.yml"


def test_oauth_mode_layers_the_overlay_on_every_remote_compose_call(tmp_path: Path) -> None:
    # A logs/status/down run that resolved a different file list than the `up`
    # would report on a stack that is not the running one — and an `up` with the
    # base file alone would recreate the containers in the virtual-key mode.
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "--mode", "oauth", "up"],
        ps=HEALTHY_PS,
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    docker_log = _log(tmp_path, "docker.log")
    assert f"compose {BOTH_FILES} pull" in docker_log
    assert f"compose {BOTH_FILES} up -d" in docker_log
    assert f"compose {BOTH_FILES} ps --all" in docker_log
    # No call may fall back to the base file alone.
    for line in docker_log.splitlines():
        if line.startswith("compose "):
            assert line.startswith(f"compose {BOTH_FILES}"), line


def test_the_default_mode_is_the_subscription_mode(tmp_path: Path) -> None:
    # Subscription mode is the production one; a bare `up` must deploy it, not
    # the virtual-key test posture.
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(repo, tmp_path, ["--host", HOST, "--dir", str(remote), "up"], ps=HEALTHY_PS)

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    docker_log = _log(tmp_path, "docker.log")
    assert f"compose {BOTH_FILES} up -d" in docker_log
    for line in docker_log.splitlines():
        if line.startswith("compose "):
            assert line.startswith(f"compose {BOTH_FILES}"), line
    assert (remote / "docker-compose.oauth.yml").exists()


def test_virtual_keys_mode_keeps_the_base_file_alone(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "--mode", "virtual-keys", "status"],
        ps=HEALTHY_PS,
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    docker_log = _log(tmp_path, "docker.log")
    assert "compose -f docker-compose.yml ps --all" in docker_log
    assert "docker-compose.oauth.yml" not in docker_log


def test_the_usage_names_oauth_as_the_default(script_text: str) -> None:
    assert re.search(r"^DEPLOY_MODE=\"oauth\"$", script_text, re.M)
    assert "oauth (default) or virtual-keys" in script_text


def test_the_oauth_overlay_is_synced_unlike_the_dev_only_build_overlay(tmp_path: Path) -> None:
    # Mode B is a deployable mode, so its overlay has to reach the server;
    # docker-compose.build.yml is a laptop convenience and must not.
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "--mode", "oauth", "up"],
        ps=HEALTHY_PS,
    )

    assert (remote / "docker-compose.oauth.yml").exists()
    assert not (remote / "docker-compose.build.yml").exists()


@pytest.mark.parametrize("mode", ["", "prod", "oauth2", "VIRTUAL-KEYS"])
def test_an_unknown_mode_is_refused_before_anything_remote_happens(
    mode: str, tmp_path: Path
) -> None:
    # Falling back to the default would deploy the virtual-key mode onto a host
    # whose .env has no master key, and the stack would then refuse to boot
    # citing a variable the operator never meant to use.
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(repo, tmp_path, ["--host", HOST, "--dir", str(remote), "--mode", mode, "status"])

    assert result.returncode == 1  # type: ignore[attr-defined]
    assert "--mode must be virtual-keys or oauth" in result.stderr  # type: ignore[attr-defined]
    assert _log(tmp_path, "ssh.log") == ""


# --------------------------------------------------------------------------- #
# --issuance: the third overlay and the server-owned gateway/config.toml
# --------------------------------------------------------------------------- #

THREE_FILES = f"{BOTH_FILES} -f docker-compose.issuance.yml"


def _server_config(remote: Path) -> Path:
    (remote / "gateway").mkdir(exist_ok=True)
    config = remote / "gateway" / "config.toml"
    config.write_text("# server copy\n")
    return config


def _compose_lines(tmp_path: Path) -> list[str]:
    lines = _log(tmp_path, "docker.log").splitlines()
    return [line for line in lines if line.startswith("compose ")]


def test_the_default_run_carries_no_issuance_overlay(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(repo, tmp_path, ["--host", HOST, "--dir", str(remote), "up"], ps=HEALTHY_PS)

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    assert f"compose {BOTH_FILES} up -d" in _log(tmp_path, "docker.log")
    assert "docker-compose.issuance.yml" not in _log(tmp_path, "docker.log")


@pytest.mark.parametrize(
    ("argv", "env_extra"),
    [
        (["--issuance"], {}),
        (["--issuance", "--mode", "oauth"], {}),
        ([], {"DEPLOY_ISSUANCE": "1"}),
    ],
    ids=["flag", "flag-explicit-oauth", "env"],
)
def test_issuance_layers_the_third_overlay_after_oauth_on_every_call(
    argv: list[str], env_extra: dict[str, str], tmp_path: Path
) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)
    _server_config(remote)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), *argv, "up"],
        ps=HEALTHY_PS,
        env_extra=env_extra,
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    docker_log = _log(tmp_path, "docker.log")
    assert f"compose {THREE_FILES} pull" in docker_log
    assert f"compose {THREE_FILES} up -d" in docker_log
    assert f"compose {THREE_FILES} ps --all" in docker_log
    lines = _compose_lines(tmp_path)
    assert lines
    for line in lines:
        assert line.startswith(f"compose {THREE_FILES} "), line
    assert (remote / "docker-compose.issuance.yml").exists()


def test_issuance_keeps_the_file_list_on_read_only_runs(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    argv = ["--host", HOST, "--dir", str(remote), "--issuance", "status"]

    result = _run(repo, tmp_path, argv, ps=HEALTHY_PS)

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    assert f"compose {THREE_FILES} ps --all" in _log(tmp_path, "docker.log")


@pytest.mark.parametrize(
    ("argv", "env_extra"),
    [
        (["--issuance", "--mode", "virtual-keys"], {}),
        (["--mode", "virtual-keys", "--issuance"], {}),
        (["--mode=virtual-keys"], {"DEPLOY_ISSUANCE": "1"}),
    ],
    ids=["flag-first", "mode-first", "env"],
)
def test_issuance_is_refused_outside_the_subscription_mode(
    argv: list[str], env_extra: dict[str, str], tmp_path: Path
) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)
    _server_config(remote)

    result = _run(
        repo, tmp_path, ["--host", HOST, "--dir", str(remote), *argv, "up"], env_extra=env_extra
    )

    assert result.returncode == 1  # type: ignore[attr-defined]
    assert "--issuance needs --mode oauth" in result.stderr  # type: ignore[attr-defined]
    assert _log(tmp_path, "ssh.log") == ""
    assert _log(tmp_path, "rsync.log") == ""


@pytest.mark.parametrize("value", ["yes", "true", "2", "on"])
def test_a_bad_deploy_issuance_value_is_refused(value: str, tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "status"],
        env_extra={"DEPLOY_ISSUANCE": value},
    )

    assert result.returncode == 1  # type: ignore[attr-defined]
    assert "DEPLOY_ISSUANCE must be 0 or 1" in result.stderr  # type: ignore[attr-defined]
    assert _log(tmp_path, "ssh.log") == ""


@pytest.mark.parametrize("value", ["0", ""])
def test_deploy_issuance_off_values_keep_two_files(value: str, tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "status"],
        ps=HEALTHY_PS,
        env_extra={"DEPLOY_ISSUANCE": value},
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    assert f"compose {BOTH_FILES} ps --all" in _log(tmp_path, "docker.log")
    assert "docker-compose.issuance.yml" not in _log(tmp_path, "docker.log")


@pytest.mark.parametrize("shape", ["absent", "directory"])
def test_issuance_up_without_a_server_config_fails_before_anything_changes(
    shape: str, tmp_path: Path
) -> None:
    # A missing bind-mount source can come up as an empty directory, which the
    # config loader skips: issuance would boot silently off.
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)
    if shape == "directory":
        (remote / "gateway" / "config.toml").mkdir(parents=True)

    result = _run(
        repo, tmp_path, ["--host", HOST, "--dir", str(remote), "--issuance", "up"], ps=HEALTHY_PS
    )

    assert result.returncode == 1  # type: ignore[attr-defined]
    assert "compose/gateway/config.toml.example" in result.stderr  # type: ignore[attr-defined]
    assert f"{remote}/gateway/config.toml" in result.stderr  # type: ignore[attr-defined]
    assert _log(tmp_path, "rsync.log") == "", "nothing may be synced before the check passes"
    assert "pull" not in _log(tmp_path, "docker.log")
    assert "up -d" not in _log(tmp_path, "docker.log")
    assert not (remote / ".deploy.lock").exists()


def test_sync_never_overwrites_the_servers_config_toml(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)
    config = _server_config(remote)

    result = _call(
        "sync_compose",
        tmp_path,
        remote_dir=remote,
        compose_dir=repo / "compose",
        script=repo / "scripts" / "deploy" / "deploy.sh",
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert config.read_text() == "# server copy\n"
    assert (remote / "gateway" / "config.toml.example").read_text() == "# example team map\n"
    assert "--exclude=gateway/config.toml\n" in _log(tmp_path, "rsync.log")


def test_sync_never_creates_a_server_config_toml_from_the_laptop(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _call(
        "sync_compose",
        tmp_path,
        remote_dir=remote,
        compose_dir=repo / "compose",
        script=repo / "scripts" / "deploy" / "deploy.sh",
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert not (remote / "gateway" / "config.toml").exists()
    assert (remote / "gateway" / "config.toml.example").exists()


def test_help_documents_issuance() -> None:
    result = subprocess.run([str(SCRIPT), "--help"], capture_output=True, text=True)

    assert result.returncode == 0
    assert re.search(r"^\s+--issuance\b", result.stderr, re.M)
    assert "DEPLOY_ISSUANCE=1" in result.stderr
    assert "gateway/config.toml" in result.stderr


# --------------------------------------------------------------------------- #
# the nginx front door: .env-driven profile, out-of-band certificates
# --------------------------------------------------------------------------- #

CERT_EXCLUDES = ("*.pem", "*.crt", "*.key", "*.p12", "*.pfx")
# Names NGINX_TLS_CERT / NGINX_TLS_KEY accept that no extension rule catches.
NGINX_CERT_FILES = ("x.pem", "x.key", "privkey", "server.cer")
UNIT = ROOT / "scripts" / "deploy" / "corp-llm-gateway.service"


def _with_nginx_tree(repo: Path) -> Path:
    """The real compose/nginx/ with a laptop's certificate files in certs/."""
    nginx = repo / "compose" / "nginx"
    shutil.copytree(
        ROOT / "compose" / "nginx",
        nginx,
        ignore=lambda folder, names: (
            [name for name in names if name != "README.md"]
            if Path(folder).name == "certs"
            else ["__pycache__"]
        ),
    )
    for name in NGINX_CERT_FILES:
        (nginx / "certs" / name).write_text("-----BEGIN PRIVATE KEY-----\n")
    return nginx


def _itemized_files(stdout: str) -> set[str]:
    return set(re.findall(r"^>f\S*\s+(\S.*)$", stdout, re.M))


def test_rsync_keeps_the_five_extension_excludes_and_adds_the_certs_dir(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(repo, tmp_path, ["--host", HOST, "--dir", str(remote), "--dry-run", "up"])

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    args = _log(tmp_path, "rsync.log").splitlines()
    for pattern in CERT_EXCLUDES:
        assert f"--exclude={pattern}" in args, pattern
    # rsync applies the first matching rule: the README include must come first.
    readme = args.index("--include=nginx/certs/README.md")
    assert readme < args.index("--exclude=nginx/certs/*")


def test_a_dry_run_would_send_the_nginx_config_and_no_certificate(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)
    nginx = _with_nginx_tree(repo)
    (nginx / "leftover.pem").write_text("-----BEGIN CERTIFICATE-----\n")

    result = _run(repo, tmp_path, ["--host", HOST, "--dir", str(remote), "--dry-run", "up"])

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    sent = _itemized_files(result.stdout)  # type: ignore[attr-defined]
    templates = {
        path.relative_to(repo / "compose").as_posix()
        for path in (nginx / "templates").rglob("*")
        if path.is_file()
    }
    assert templates, "the copied tree has no templates"
    expected = {"nginx/nginx.conf", "nginx/entrypoint.sh", "nginx/certs/README.md", *templates}
    assert expected <= sent, expected - sent
    for name in NGINX_CERT_FILES:
        assert f"nginx/certs/{name}" not in sent, name
    assert "nginx/leftover.pem" not in sent, "the extension excludes still hold outside certs/"
    assert not (remote / "nginx").exists(), "a dry run transfers nothing"


def test_a_sync_keeps_the_servers_certificates_and_sends_none_of_the_laptops(
    tmp_path: Path,
) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)
    _with_nginx_tree(repo)
    (remote / "nginx" / "certs").mkdir(parents=True)
    (remote / "nginx" / "certs" / "gateway.key").write_text("server key\n")

    result = _call(
        "sync_compose",
        tmp_path,
        remote_dir=remote,
        compose_dir=repo / "compose",
        script=repo / "scripts" / "deploy" / "deploy.sh",
    )

    assert result.returncode == 0, result.stderr + result.stdout
    certs = remote / "nginx" / "certs"
    assert sorted(path.name for path in certs.iterdir()) == ["README.md", "gateway.key"]
    assert (certs / "gateway.key").read_text() == "server key\n"
    assert (remote / "nginx" / "entrypoint.sh").exists()


def test_the_script_never_sets_a_profile(script_text: str) -> None:
    code = _code_lines(script_text)
    assert "--profile" not in code
    for pattern in (
        r"\bCOMPOSE_PROFILES=",
        r"\bexport\s+[^\n]*\bCOMPOSE_PROFILES\b",
        r"\bunset\s+[^\n]*\bCOMPOSE_PROFILES\b",
        r"\benv\b[^\n]*(-u|--unset)[= ]*COMPOSE_PROFILES\b",
        r"\benv\s+-i\b",
    ):
        assert not re.search(pattern, code), pattern


@pytest.mark.parametrize(
    "argv",
    [["up"], ["--yes", "down"], ["--yes", "restart"], ["logs"], ["status"]],
    ids=["up", "down", "restart", "logs", "status"],
)
def test_no_remote_command_carries_a_profile(argv: list[str], tmp_path: Path) -> None:
    # The server's .env is the only switch; a flag or an environment value here
    # would override it and diverge from what the boot-time unit starts.
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(repo, tmp_path, ["--host", HOST, "--dir", str(remote), *argv], ps=HEALTHY_PS)

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    remote_commands = _log(tmp_path, "ssh.log") + _log(tmp_path, "docker.log")
    assert "compose" in _log(tmp_path, "docker.log")
    assert "--profile" not in remote_commands
    assert "COMPOSE_PROFILES" not in remote_commands
    assert _log(tmp_path, "docker-env.log") == "", "COMPOSE_PROFILES reached docker"


def test_up_refuses_both_front_doors_before_the_pull(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "up"],
        ps=HEALTHY_PS,
        env_extra={"FAKE_SERVICES": "litellm\nnginx\npostgres\nnginx-ports"},
    )

    assert result.returncode == 1  # type: ignore[attr-defined]
    stderr = result.stderr  # type: ignore[attr-defined]
    assert "both nginx nginx-ports" in stderr
    assert "Pick ONE" in stderr
    docker_log = _log(tmp_path, "docker.log")
    assert f"compose {BOTH_FILES} config --services" in docker_log
    assert "pull" not in docker_log
    assert "up -d" not in docker_log
    assert not (remote / ".deploy.lock").exists(), "the lock must be released on a refusal"


@pytest.mark.parametrize("front_door", ["nginx", "nginx-ports"])
def test_up_accepts_one_front_door(front_door: str, tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)
    ps = [*HEALTHY_PS, {"Service": front_door, "State": "running", "Health": "healthy"}]

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "up"],
        ps=ps,
        env_extra={"FAKE_SERVICES": f"litellm\n{front_door}\npostgres"},
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    assert f"compose {BOTH_FILES} up -d" in _log(tmp_path, "docker.log")


def _front_door_row(service: str, name: str | None = None) -> dict[str, str]:
    return {
        "Service": service,
        "Name": f"corp-llm-gateway-{service}-1" if name is None else name,
        "State": "running",
        "Health": "healthy",
    }


def _docker_lines(tmp_path: Path) -> list[str]:
    return _log(tmp_path, "docker.log").splitlines()


def _index(lines: list[str], needle: str) -> int:
    return next(i for i, line in enumerate(lines) if needle in line)


def test_up_removes_the_front_door_the_env_no_longer_selects(tmp_path: Path) -> None:
    # Profiles only filter what `up` starts: the old nginx-ports would keep
    # NGINX_PORT, and the newly selected nginx would fail to bind it.
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)
    ps = [*HEALTHY_PS, _front_door_row("nginx"), _front_door_row("nginx-ports")]

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "up"],
        ps=ps,
        env_extra={"FAKE_SERVICES": "litellm\nnginx\npostgres"},
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    lines = _docker_lines(tmp_path)
    removals = [line for line in lines if line.startswith("rm ")]
    assert removals == ["rm -f corp-llm-gateway-nginx-ports-1"]
    removed_at = _index(lines, "rm -f corp-llm-gateway-nginx-ports-1")
    assert removed_at < _index(lines, f"compose {BOTH_FILES} pull")
    assert removed_at < _index(lines, f"compose {BOTH_FILES} up -d")
    assert "corp-llm-gateway-nginx-ports-1" in result.stderr  # type: ignore[attr-defined]


def test_up_without_a_profile_removes_a_leftover_nginx(tmp_path: Path) -> None:
    # The leftover would keep serving the public port, and `restart:
    # unless-stopped` would bring it back after every reboot.
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "up"],
        ps=[*HEALTHY_PS, _front_door_row("nginx")],
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    lines = _docker_lines(tmp_path)
    assert [line for line in lines if line.startswith("rm ")] == ["rm -f corp-llm-gateway-nginx-1"]
    assert _index(lines, "rm -f") < _index(lines, f"compose {BOTH_FILES} pull")


@pytest.mark.parametrize("front_door", ["nginx", "nginx-ports"])
def test_up_keeps_the_front_door_the_env_selects(front_door: str, tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "up"],
        ps=[*HEALTHY_PS, _front_door_row(front_door)],
        env_extra={"FAKE_SERVICES": f"litellm\n{front_door}\npostgres"},
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    assert not [line for line in _docker_lines(tmp_path) if line.startswith("rm ")]


def test_down_removes_every_front_door_container_compose_down_leaves(tmp_path: Path) -> None:
    # `compose down` only stops the services of the active profiles.
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)
    ps = [*HEALTHY_PS, _front_door_row("nginx"), _front_door_row("nginx-ports")]

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "--yes", "down"],
        ps=ps,
        env_extra={"FAKE_SERVICES": "litellm\nnginx\npostgres"},
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    lines = _docker_lines(tmp_path)
    removals = [line for line in lines if line.startswith("rm ")]
    assert sorted(removals) == [
        "rm -f corp-llm-gateway-nginx-1",
        "rm -f corp-llm-gateway-nginx-ports-1",
    ]
    down_at = _index(lines, f"compose {BOTH_FILES} down")
    assert all(_index(lines, removal) > down_at for removal in removals)


@pytest.mark.parametrize(
    "name",
    ["corp;id", "-rf", "$(id)", "a b", "", "_leading", "x\ny"],
    ids=["semicolon", "flag", "subst", "space", "empty", "underscore", "newline"],
)
@pytest.mark.parametrize("argv", [["up"], ["--yes", "down"]], ids=["up", "down"])
def test_a_container_name_outside_the_charset_is_refused(
    name: str, argv: list[str], tmp_path: Path
) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)
    ps = [*HEALTHY_PS, _front_door_row("nginx-ports"), _front_door_row("nginx", name)]

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), *argv],
        ps=ps,
        env_extra={"FAKE_SERVICES": "litellm\npostgres"},
    )

    assert result.returncode == 1  # type: ignore[attr-defined]
    assert "container name" in result.stderr  # type: ignore[attr-defined]
    docker_log = _log(tmp_path, "docker.log")
    assert "rm " not in docker_log, "nothing may be removed once one name is refused"
    assert "pull" not in docker_log
    assert not (remote / ".deploy.lock").exists()


@pytest.mark.parametrize("argv", [["up"], ["--yes", "down"]], ids=["up", "down"])
def test_a_dry_run_removes_no_container(argv: list[str], tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "--dry-run", *argv],
        ps=[*HEALTHY_PS, _front_door_row("nginx"), _front_door_row("nginx-ports")],
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    assert "rm " not in _log(tmp_path, "docker.log")


def test_up_stops_before_the_pull_when_compose_cannot_resolve_the_stack(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "up"],
        ps=HEALTHY_PS,
        env_extra={"FAKE_CONFIG_FAIL": "1"},
    )

    assert result.returncode == 1  # type: ignore[attr-defined]
    assert "could not resolve the stack" in result.stderr  # type: ignore[attr-defined]
    assert "pull" not in _log(tmp_path, "docker.log")


def test_a_dry_run_does_not_check_profiles_against_the_old_files(tmp_path: Path) -> None:
    # Nothing was synced, so the server still holds the previous compose files.
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "--dry-run", "up"],
        env_extra={"FAKE_SERVICES": "nginx\nnginx-ports"},
    )

    assert result.returncode == 0, result.stderr  # type: ignore[attr-defined]
    assert "config --services" not in _log(tmp_path, "docker.log")


@pytest.mark.parametrize(
    ("service", "state", "health"),
    [
        ("nginx", "exited", ""),
        ("nginx", "restarting", ""),
        ("nginx", "restarting", "unhealthy"),
        ("nginx", "running", "unhealthy"),
        ("nginx-ports", "exited", ""),
        ("nginx-ports", "restarting", "starting"),
    ],
)
def test_up_fails_at_once_on_a_dead_front_door_and_names_it(
    service: str, state: str, health: str, tmp_path: Path
) -> None:
    repo = _fake_repo(tmp_path)
    remote = _remote_dir(tmp_path)
    # litellm still starting sorts first: the dead front door must be named anyway.
    ps = [
        {"Service": "litellm", "State": "running", "Health": "starting"},
        {"Service": service, "State": state, "Health": health},
    ]

    result = _run(
        repo,
        tmp_path,
        ["--host", HOST, "--dir", str(remote), "up"],
        ps=ps,
        env_extra={"FAKE_SERVICES": f"litellm\n{service}", "CORP_GATEWAY_HEALTH_MAX_WAIT": "10"},
    )

    assert result.returncode == 1  # type: ignore[attr-defined]
    stderr = result.stderr  # type: ignore[attr-defined]
    assert f"the nginx front door is down: {service} (state={state}" in stderr
    assert f"scripts/deploy/deploy.sh --host {HOST} --dir {remote} logs {service}\n" in stderr
    # One poll plus the status table: an entrypoint refusal never heals by waiting.
    after_up = _log(tmp_path, "docker.log").split("up -d", 1)[1]
    assert after_up.count("ps --all") == 2
    assert not (remote / ".deploy.lock").exists()


@pytest.mark.parametrize(
    ("state", "health"),
    [
        ("running", "healthy"),
        ("running", "starting"),
        ("created", ""),
        ("running", ""),
        ("paused", "healthy"),
    ],
)
def test_a_live_front_door_is_polled_like_any_service(
    state: str, health: str, tmp_path: Path
) -> None:
    # nginx declares a healthcheck, so only "healthy" counts. A crash-looping
    # front door shows `running` with an empty Health for an instant between
    # restarts; accepting that let one poll end the wait on a broken NGINX_* key.
    ps = [*HEALTHY_PS, {"Service": "nginx", "State": state, "Health": health}]

    result = _call(
        "wait_for_healthcheck",
        tmp_path,
        ssh_mode="ps",
        ps=ps,
        extra="HEALTH_MAX_WAIT=1\nHEALTH_INTERVAL=1\n",
    )

    if (state, health) == ("running", "healthy"):
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1
        assert f"within 1s — stuck: nginx (state={state}" in result.stderr
        assert "front door is down" not in result.stderr


def test_only_the_front_door_fails_fast_on_exited(tmp_path: Path) -> None:
    ps = [*HEALTHY_PS, {"Service": "litellm-exited-lookalike", "State": "exited", "Health": ""}]

    result = _call(
        "wait_for_healthcheck",
        tmp_path,
        ssh_mode="ps",
        ps=ps,
        extra="HEALTH_MAX_WAIT=1\nHEALTH_INTERVAL=1\n",
    )

    assert result.returncode == 1
    assert "front door is down" not in result.stderr


def test_the_boot_time_unit_still_runs_a_bare_compose_up() -> None:
    exec_start = [line for line in UNIT.read_text().splitlines() if line.startswith("ExecStart=")]
    assert exec_start == ["ExecStart=/usr/bin/docker compose up -d"]


@pytest.mark.parametrize(
    "compose_file",
    [
        None,
        "docker-compose.yml:docker-compose.oauth.yml",
        "docker-compose.yml:docker-compose.oauth.yml:docker-compose.issuance.yml",
    ],
    ids=["mode-a", "mode-b", "mode-b-issuance"],
)
@pytest.mark.parametrize("profiles", [None, "nginx", "nginx-ports"])
def test_the_reboot_path_starts_what_the_env_file_selects(
    profiles: str | None, compose_file: str | None, tmp_path: Path
) -> None:
    # The unit's bare command, no -f and no --profile: only the .env decides.
    from tests.compose.nginx_support import (
        CONFIG_EXAMPLE,
        REQUIRED_ENV,
        bare_compose_env,
        require_compose_cli,
    )

    require_compose_cli()
    project = tmp_path / "compose"
    shutil.copytree(ROOT / "compose", project)
    shutil.copy(CONFIG_EXAMPLE, project / "gateway" / "config.toml")
    lines = [f"{key}=render-fixture" for key in REQUIRED_ENV]
    if compose_file is not None:
        lines.append(f"COMPOSE_FILE={compose_file}")
    if profiles is not None:
        lines.append(f"COMPOSE_PROFILES={profiles}")
    (project / ".env").write_text("\n".join(lines) + "\n")

    result = subprocess.run(
        ["docker", "compose", "config", "--services"],
        cwd=project,
        capture_output=True,
        text=True,
        env=bare_compose_env(),
        check=False,
    )

    assert result.returncode == 0, result.stderr
    services = set(result.stdout.split())
    assert "litellm" in services
    assert services & {"nginx", "nginx-ports"} == ({profiles} if profiles else set())
