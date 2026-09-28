"""The container harness's Postgres readiness gate and its psql retry.

No docker needed: the gate is a pure function of the container log, and
``_run_sql`` is driven through a fake ``docker`` on PATH.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration import conftest as harness

TEMP_READY = "LOG:  database system is ready to accept connections"
INIT_DONE = "PostgreSQL init process complete; ready for start up."

# The official image's first boot, abridged: temporary server, init, final server.
FIRST_BOOT = "\n".join(
    [
        "waiting for server to start....",
        TEMP_READY,
        " done",
        "CREATE DATABASE",
        "waiting for server to shut down.... done",
        "",
        INIT_DONE,
        "",
        "LOG:  starting PostgreSQL 16",
        TEMP_READY,
    ]
)


def test_the_gate_opens_once_the_final_server_is_ready() -> None:
    assert harness.postgres_init_done(FIRST_BOOT)


@pytest.mark.parametrize(
    "log",
    [
        "",
        "waiting for server to start....\n" + TEMP_READY,
        FIRST_BOOT.rsplit(TEMP_READY, 1)[0],
        TEMP_READY + "\n" + TEMP_READY,
    ],
    ids=["empty", "temporary-server-only", "init-done-no-final-server", "ready-twice-no-init"],
)
def test_the_gate_stays_shut_on_the_temporary_server(log: str) -> None:
    # pg_isready passes here too: that window caused both CI failures.
    assert not harness.postgres_init_done(log)


FAKE_DOCKER = """#!/usr/bin/env bash
n=$(( $(cat "$FAKE_COUNT" 2>/dev/null || echo 0) + 1 ))
printf '%s\\n' "$n" > "$FAKE_COUNT"
printf '%s\\n' "$*" >> "$FAKE_ARGS"
cat > /dev/null
if [ "$n" -le "$FAKE_FAILURES" ]; then
    printf '%s\\n' "$FAKE_STDERR" >&2
    exit 2
fi
exit 0
"""


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_COUNT", str(tmp_path / "count"))
    monkeypatch.setenv("FAKE_ARGS", str(tmp_path / "args"))
    monkeypatch.setattr(harness, "RUN_SQL_RETRY_SECONDS", 0)
    return tmp_path


def _calls(tmp_path: Path) -> int:
    count = tmp_path / "count"
    return int(count.read_text()) if count.exists() else 0


@pytest.mark.parametrize(
    "stderr",
    [
        'psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432"'
        " failed: Connection refused",
        "psql: error: server closed the connection unexpectedly",
    ],
    ids=["refused", "closed"],
)
def test_run_sql_retries_a_server_that_is_not_listening(
    stderr: str, fake_docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FAILURES", "3")
    monkeypatch.setenv("FAKE_STDERR", stderr)

    harness._run_sql("pg", "SELECT 1;")

    assert _calls(fake_docker) == 4
    assert "--single-transaction" in (fake_docker / "args").read_text()


def test_run_sql_does_not_retry_a_missing_database(
    fake_docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A config bug, not a timing one: retrying would hide it.
    monkeypatch.setenv("FAKE_FAILURES", "99")
    monkeypatch.setenv("FAKE_STDERR", 'psql: error: FATAL:  database "litellm" does not exist')

    with pytest.raises(pytest.fail.Exception, match="does not exist"):
        harness._run_sql("pg", "SELECT 1;")

    assert _calls(fake_docker) == 1


def test_run_sql_gives_up_after_the_bounded_window(
    fake_docker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_FAILURES", "999")
    monkeypatch.setenv("FAKE_STDERR", "psql: error: Connection refused")

    with pytest.raises(pytest.fail.Exception, match="Connection refused"):
        harness._run_sql("pg", "SELECT 1;")

    assert _calls(fake_docker) == harness.RUN_SQL_ATTEMPTS
