"""Pins ci.yml: the unit suite on Python 3.14, the route gate on the real image, and
tests/e2e against its services with every skip turned into a failure."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from corp_llm_gateway.settings import parse_flag

ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
CONTAINER_SUITE = "tests/integration/test_route_gate_container.py"
NGINX_IMAGE_SUITE = "tests/integration/test_nginx_allowlist_image.py"
JOB = "integration-container"
UNIT_JOB = "test"
# The one interpreter CI runs, and the one .venv-bench runs.
E2E_JOB = "e2e"
E2E_SUITE = "tests/e2e"
# tests/e2e/conftest.py: set, a skipped or deselected e2e test fails.
E2E_GUARD = "CORP_REQUIRE_E2E"
E2E_CONFTEST = ROOT / "tests/e2e/conftest.py"
# Its upstream and proxy run in-process, so a nested run needs nothing started.
PROXY_E2E = "tests/e2e/test_proxy_pipeline.py"
CI_PYTHON = "3.14"
ALL_JOBS = ["lint", UNIT_JOB, JOB, E2E_JOB]


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
    assert NGINX_IMAGE_SUITE in shlex.split(pytest_steps[0]["run"])


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


# ── tests/e2e: its own job, against real Redis and the two mocks ────────────


def _e2e_pytest_steps() -> list[dict[str, Any]]:
    return [step for step in _runs(_jobs()[E2E_JOB]) if "pytest" in step["run"]]


def test_the_e2e_job_runs_the_e2e_suite_beside_the_other_jobs() -> None:
    job = _jobs()[E2E_JOB]

    assert job["runs-on"] == "ubuntu-latest"
    assert isinstance(job.get("timeout-minutes"), int)
    assert "needs" not in job
    assert "strategy" not in job
    steps = _e2e_pytest_steps()
    assert len(steps) == 1
    args = shlex.split(steps[0]["run"])
    assert E2E_SUITE in args
    assert "-rs" in args


def test_the_e2e_job_runs_redis_as_a_health_checked_service() -> None:
    redis = _jobs()[E2E_JOB]["services"]["redis"]

    assert str(redis["image"]).startswith("redis:")
    assert "--health-cmd" in redis["options"]


def test_the_e2e_job_cannot_pass_by_skipping() -> None:
    job = _jobs()[E2E_JOB]
    steps = _e2e_pytest_steps()

    assert steps
    assert "continue-on-error" not in job
    for step in job["steps"]:
        assert "continue-on-error" not in step, step
    for step in steps:
        env = {**(job.get("env") or {}), **(step.get("env") or {})}
        assert parse_flag(str(env.get(E2E_GUARD, "")))
        run = step["run"]
        assert "||" not in run
        assert "; true" not in run
        args = shlex.split(run)
        selectors = args[args.index("pytest") + 1 :]
        for arg in selectors:
            assert not arg.startswith(("-k", "-m", "--deselect", "--ignore")), arg


def _nested_pytest(cwd: Path, guard: bool, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in {"RUN_PROXY_E2E", E2E_GUARD}}
    env["PYTHONPATH"] = str(ROOT / "src")
    if guard:
        env[E2E_GUARD] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_e2e_guard_fails_a_skipped_test_in_tests_e2e() -> None:
    armed = _nested_pytest(ROOT, True, PROXY_E2E)
    unarmed = _nested_pytest(ROOT, False, PROXY_E2E)

    assert armed.returncode == 1, armed.stdout
    assert f"{E2E_GUARD} is set but this e2e test skipped" in armed.stdout
    assert "skipped" not in armed.stdout.splitlines()[-1]
    assert unarmed.returncode == 0, unarmed.stdout
    assert "skipped" in unarmed.stdout.splitlines()[-1]


_SCRATCH_MODULES = {
    "test_plain.py": "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n",
    "test_marked.py": (
        "import pytest\n\n\n@pytest.mark.skipif(True, reason='no service')\n"
        "def test_marked():\n    pass\n"
    ),
    "test_module_skip.py": (
        "import pytest\n\npytest.skip('down', allow_module_level=True)\n\n\n"
        "def test_x():\n    pass\n"
    ),
    "test_importorskip.py": (
        "import pytest\n\npytest.importorskip('no_such_module_for_the_e2e_guard')\n\n\n"
        "def test_y():\n    pass\n"
    ),
}


@pytest.mark.parametrize(
    ("args", "armed_exit", "message", "unarmed_word"),
    [
        pytest.param(["e2e/test_marked.py"], 1, "e2e test skipped", "skipped", id="marked"),
        pytest.param(
            ["e2e/test_plain.py", "e2e/test_module_skip.py"],
            2,
            "e2e module skipped",
            "skipped",
            id="module-skip",
        ),
        pytest.param(
            ["e2e/test_plain.py", "e2e/test_importorskip.py"],
            2,
            "e2e module skipped",
            "skipped",
            id="importorskip",
        ),
        pytest.param(
            ["e2e/test_plain.py", "-k", "test_a"], 4, "deselected", "deselected", id="dash-k"
        ),
        pytest.param(
            ["e2e/test_plain.py", "--deselect", "e2e/test_plain.py::test_b"],
            4,
            "deselected",
            "deselected",
            id="deselect",
        ),
    ],
)
def test_the_e2e_guard_fails_every_way_an_e2e_test_can_go_unrun(
    tmp_path: Path, args: list[str], armed_exit: int, message: str, unarmed_word: str
) -> None:
    scratch = tmp_path / "e2e"
    scratch.mkdir()
    (scratch / "conftest.py").write_text(E2E_CONFTEST.read_text())
    for name, body in _SCRATCH_MODULES.items():
        (scratch / name).write_text(body)

    armed = _nested_pytest(tmp_path, True, *args)
    unarmed = _nested_pytest(tmp_path, False, *args)

    assert armed.returncode == armed_exit, armed.stdout + armed.stderr
    assert f"{E2E_GUARD} is set but" in armed.stdout + armed.stderr
    assert message in armed.stdout + armed.stderr
    assert unarmed.returncode == 0, unarmed.stdout + unarmed.stderr
    assert unarmed_word in unarmed.stdout.splitlines()[-1]
