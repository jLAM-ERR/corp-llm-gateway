"""The e2e skip guard.

Every e2e test skips unless its services' env vars are set, so a CI job that
lost one would report green over nothing. CI's e2e job sets
``CORP_REQUIRE_E2E=1``, which turns any skip under ``tests/e2e`` into a
failure: a test's skip (setup or call), a module's skip at collection, and a
test deselected by ``-k`` / ``-m`` / ``--deselect``. The unit job runs these
tests without the services and without the switch, so there they still skip.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from corp_llm_gateway.settings import parse_flag

REQUIRE_ENV_VAR = "CORP_REQUIRE_E2E"
HERE = Path(__file__).resolve().parent


def _armed() -> bool:
    return parse_flag(os.environ.get(REQUIRE_ENV_VAR))


def _reason(longrepr: object) -> object:
    return longrepr[-1] if isinstance(longrepr, tuple) else longrepr


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    outcome = yield
    report = outcome.get_result()
    if report.skipped and _armed():
        report.outcome = "failed"
        report.longrepr = (
            f"{REQUIRE_ENV_VAR} is set but this e2e test skipped: {_reason(report.longrepr)}"
        )


# Rewritten where it is made: by pytest_collectreport the session has already counted it.
@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector: pytest.Collector):
    outcome = yield
    report = outcome.get_result()
    if report.skipped and _armed():
        report.outcome = "failed"
        report.longrepr = (
            f"{REQUIRE_ENV_VAR} is set but this e2e module skipped: {_reason(report.longrepr)}"
        )


def pytest_deselected(items: list[pytest.Item]) -> None:
    ours = [item.nodeid for item in items if HERE in item.path.resolve().parents]
    if ours and _armed():
        raise pytest.UsageError(f"{REQUIRE_ENV_VAR} is set but e2e tests were deselected: {ours}")
