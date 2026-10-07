"""The e2e skip guard.

Every e2e test skips unless its services' env vars are set, so a CI job that
lost one would report green over nothing. CI's e2e job sets
``CORP_REQUIRE_E2E=1``, which turns any skip under ``tests/e2e`` into a
failure. The unit job runs these tests without the services and without the
switch, so there they still skip.
"""

from __future__ import annotations

import os

import pytest

from corp_llm_gateway.settings import parse_flag

REQUIRE_ENV_VAR = "CORP_REQUIRE_E2E"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    outcome = yield
    report = outcome.get_result()
    if report.skipped and parse_flag(os.environ.get(REQUIRE_ENV_VAR)):
        reason = report.longrepr[-1] if isinstance(report.longrepr, tuple) else report.longrepr
        report.outcome = "failed"
        report.longrepr = f"{REQUIRE_ENV_VAR} is set but this e2e test skipped: {reason}"
