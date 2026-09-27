"""Pins the ci.yml job that runs the route gate on the real image."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import yaml

from corp_llm_gateway.settings import parse_flag

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
CONTAINER_SUITE = "tests/integration/test_route_gate_container.py"
JOB = "integration-container"


def _jobs() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]


def _runs(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job["steps"] if step.get("run")]


def test_the_container_job_exists_and_runs_the_container_suite() -> None:
    job = _jobs()[JOB]

    assert job["runs-on"] == "ubuntu-latest"
    assert isinstance(job.get("timeout-minutes"), int)
    pytest_steps = [step for step in _runs(job) if "pytest" in step["run"]]
    assert len(pytest_steps) == 1
    assert CONTAINER_SUITE in shlex.split(pytest_steps[0]["run"])


def test_the_container_job_cannot_pass_by_skipping() -> None:
    job = _jobs()[JOB]
    for step in _runs(job):
        if "pytest" not in step["run"]:
            continue
        env = {**(job.get("env") or {}), **(step.get("env") or {})}
        assert parse_flag(str(env.get("CORP_REQUIRE_PROXY_CAPTURE", "")))


def test_the_container_job_builds_through_the_fixtures_own_helper() -> None:
    runs = [step["run"] for step in _runs(_jobs()[JOB])]
    build = [index for index, run in enumerate(runs) if "tests.integration.gateway_image" in run]
    suite = [index for index, run in enumerate(runs) if CONTAINER_SUITE in run]

    assert build
    assert build[0] < suite[0]


def test_the_container_job_runs_after_lint_and_beside_the_unit_suite() -> None:
    needs = _jobs()[JOB]["needs"]
    needs = [needs] if isinstance(needs, str) else needs

    assert "lint" in needs
    assert "test" not in needs
