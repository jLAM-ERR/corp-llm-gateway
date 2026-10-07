"""Pins ci.yml's e2e job: tests/e2e on Python 3.14 against a Redis service, beside
the other jobs, and unable to pass by skipping. The Python pin itself is
test_ci_workflow.py::test_every_job_runs_python_3_14."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from corp_llm_gateway.settings import parse_flag

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
E2E_JOB = "e2e"
E2E_SUITE = "tests/e2e"
# tests/e2e/conftest.py: set, a skipped e2e test fails.
E2E_GUARD = "CORP_REQUIRE_E2E"
# Its upstream and proxy run in-process, so the nested run needs nothing started.
PROXY_E2E = "tests/e2e/test_proxy_pipeline.py"


def _job() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW.read_text())["jobs"][E2E_JOB]


def _pytest_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job["steps"] if "pytest" in (step.get("run") or "")]


def test_the_e2e_job_runs_the_e2e_suite_beside_the_other_jobs() -> None:
    job = _job()

    assert job["runs-on"] == "ubuntu-latest"
    assert isinstance(job.get("timeout-minutes"), int)
    assert "needs" not in job
    assert "strategy" not in job
    steps = _pytest_steps(job)
    assert len(steps) == 1
    args = shlex.split(steps[0]["run"])
    assert E2E_SUITE in args
    assert "-rs" in args


def test_the_e2e_job_runs_redis_as_a_health_checked_service() -> None:
    redis = _job()["services"]["redis"]

    assert str(redis["image"]).startswith("redis:")
    assert "--health-cmd" in redis["options"]


def test_the_e2e_job_cannot_pass_by_skipping() -> None:
    job = _job()
    steps = _pytest_steps(job)

    assert steps
    for step in steps:
        env = {**(job.get("env") or {}), **(step.get("env") or {})}
        assert parse_flag(str(env.get(E2E_GUARD, "")))


def _run_proxy_e2e_without_its_service(guard: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in {"RUN_PROXY_E2E", E2E_GUARD}}
    env["PYTHONPATH"] = "src"
    if guard is not None:
        env[E2E_GUARD] = guard
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", PROXY_E2E],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_e2e_guard_turns_a_skipped_e2e_test_into_a_failure() -> None:
    armed = _run_proxy_e2e_without_its_service("1")
    unarmed = _run_proxy_e2e_without_its_service(None)

    assert armed.returncode == 1, armed.stdout
    assert f"{E2E_GUARD} is set but this e2e test skipped" in armed.stdout
    assert "skipped" not in armed.stdout.splitlines()[-1]
    assert unarmed.returncode == 0, unarmed.stdout
    assert "skipped" in unarmed.stdout.splitlines()[-1]
