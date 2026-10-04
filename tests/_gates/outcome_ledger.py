"""Records what happened to every collected node id, from pytest's own reports.

Registered with ``-p tests._gates.outcome_ledger`` by scripts/test-gates.sh only; it is
not in ``addopts``. Writes ``--outcome-ledger=PATH``:

- ``outcomes``: node id -> ``passed`` | ``skipped:<reason>`` | ``failed`` | ``error`` |
  ``xfailed`` | ``xpassed`` — one entry per parametrised id, from the setup / call /
  teardown reports (never from ``-rA`` text, which folds parametrised skips together);
- ``collection_skipped``: module node id -> reason, for a module whose module-level
  ``importorskip`` / ``pytest.skip(allow_module_level=True)`` collected zero items;
- ``collection_errors``: node id -> the error's last line (the exception).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]

_RANK = {"passed": 0, "skipped": 1, "xfailed": 1, "xpassed": 1, "failed": 2, "error": 3}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--outcome-ledger", default=None, help="write the outcome ledger here")


def normalise_reason(text: str) -> str:
    text = text.strip()
    if text.startswith("Skipped: "):
        text = text[len("Skipped: ") :]
    return text.replace(str(ROOT), "<repo>")


def _skip_reason(report: Any) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return normalise_reason(str(longrepr[2]))
    return normalise_reason(str(longrepr))


def _error_line(report: Any) -> str:
    """The last line of the error: pytest puts the exception there."""
    text = str(report.longrepr or "").strip().splitlines()
    return text[-1] if text else "error"


class OutcomeLedger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.outcomes: dict[str, str] = {}
        self.collection_skipped: dict[str, str] = {}
        self.collection_errors: dict[str, str] = {}

    def _set(self, nodeid: str, outcome: str) -> None:
        previous = self.outcomes.get(nodeid)
        if previous is None or _RANK[outcome.split(":", 1)[0]] >= _RANK[previous.split(":", 1)[0]]:
            self.outcomes[nodeid] = outcome

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.skipped:
            self.collection_skipped[report.nodeid] = _skip_reason(report)
        elif report.failed:
            self.collection_errors[report.nodeid] = _error_line(report)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        nodeid = report.nodeid
        xfail = hasattr(report, "wasxfail")
        if report.when == "setup":
            if report.skipped:
                outcome = "xfailed" if xfail else f"skipped:{_skip_reason(report)}"
                self.outcomes[nodeid] = outcome
            elif report.failed:
                self._set(nodeid, "error")
        elif report.when == "call":
            if report.passed:
                self._set(nodeid, "xpassed" if xfail else "passed")
            elif report.skipped:
                self._set(nodeid, "xfailed" if xfail else f"skipped:{_skip_reason(report)}")
            else:
                self._set(nodeid, "failed")
        elif report.failed:
            self._set(nodeid, "error")

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        data = {
            "exitstatus": int(exitstatus),
            "outcomes": dict(sorted(self.outcomes.items())),
            "collection_skipped": dict(sorted(self.collection_skipped.items())),
            "collection_errors": dict(sorted(self.collection_errors.items())),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1) + "\n")


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("--outcome-ledger")
    if path:
        config.pluginmanager.register(OutcomeLedger(Path(path)), "outcome-ledger-writer")
