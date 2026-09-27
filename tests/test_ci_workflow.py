"""Pins ci.yml: the unit suite on Python 3.14, and the route gate on the real image."""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path
from typing import Any

import yaml

from corp_llm_gateway.settings import parse_flag

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
CONTAINER_SUITE = "tests/integration/test_route_gate_container.py"
JOB = "integration-container"
UNIT_JOB = "test"
# The one interpreter CI runs, and the one .venv-bench runs.
CI_PYTHON = "3.14"
ALL_JOBS = ["lint", UNIT_JOB, JOB]


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


# ── the unit suite: one job on one Python ───────────────────────────────────


def _uses(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    return [step for step in job["steps"] if str(step.get("uses", "")).startswith(action)]


def test_the_unit_suite_is_one_job_without_a_matrix() -> None:
    jobs = _jobs()
    job = jobs[UNIT_JOB]

    assert "strategy" not in job
    assert "name" not in job
    assert [name for name in jobs if name.startswith(UNIT_JOB)] == [UNIT_JOB]


def test_every_job_runs_python_3_14() -> None:
    jobs = _jobs()

    for name in ALL_JOBS:
        (setup,) = _uses(jobs[name], "actions/setup-python")
        assert str(setup["with"]["python-version"]) == CI_PYTHON, name


def test_the_workflow_never_names_python_3_12() -> None:
    assert "3.12" not in CI_WORKFLOW.read_text()


def test_the_unit_suite_installs_every_extra_it_needs() -> None:
    installs = [step["run"] for step in _runs(_jobs()[UNIT_JOB]) if "pip install -e" in step["run"]]

    assert len(installs) == 1
    extras = installs[0].split("[", 1)[1].split("]", 1)[0].split(",")
    assert set(extras) >= {"dev", "ner", "postgres", "oidc", "asgi", "metrics"}


def test_the_ner_extra_carries_the_gazetteer_lemmatizer() -> None:
    # rules/gazetteer.py imports pymorphy3 lazily; without it the lemma tests skip
    # and the gazetteer degrades to surface matching, so the extra has to bring it.
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]
    names = {spec.split(">", 1)[0].split("=", 1)[0].strip() for spec in extras["ner"]}

    assert {"pymorphy3", "pymorphy3-dicts-ru"} <= names


def test_the_container_job_stays_on_one_python() -> None:
    job = _jobs()[JOB]

    assert "strategy" not in job
