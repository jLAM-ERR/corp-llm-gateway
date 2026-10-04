"""Expected outcome per (node id, environment), built from two outcome-ledger runs.

``tests/_manifests/expected_outcomes.<env>.json`` maps file -> test -> outcome, one entry
per parametrised id (``{"[case]": outcome}``):

- ``passed`` / ``skipped:<reason>``;
- ``collection-skipped:<reason>`` for every id a module expands to in the other
  environment when its module-level ``importorskip`` collects zero items here (a
  module collection-skipped in both environments is a file-level string);
- ``not-applicable:<note>`` for a reviewed case: ``not_applicable.json`` keeps, per
  environment it applies to, the outcome it must still have (``skipped:<reason>``, or
  ``not-collected`` for a case parametrised only where its dependency is installed).

``python -m tests._gates.ledger write --minimal RUN.json… --full RUN.json…`` (baseline) and
``python -m tests._gates.ledger check <env> RUN.json [--scope PATH ...]``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "tests" / "_manifests"
NOT_APPLICABLE_PATH = MANIFESTS / "not_applicable.json"
ENVS = ("minimal", "full")
BASELINE = "807831a"
COLLECTION_SKIPPED = "collection-skipped:"
NOT_APPLICABLE = "not-applicable:"
NOT_COLLECTED = "not-collected"


def expected_path(env: str) -> Path:
    return MANIFESTS / f"expected_outcomes.{env}.json"


def split_id(node_id: str) -> tuple[str, str, str]:
    """(file, test, parameter suffix) — ``tests/a.py``, ``Cls::test_x``, ``[p1-p2]``."""
    path, _, rest = node_id.partition("::")
    bracket = rest.find("[")
    if bracket == -1:
        return path, rest, ""
    return path, rest[:bracket], rest[bracket:]


def join_id(path: str, test: str, param: str) -> str:
    return f"{path}::{test}{param}"


def not_applicable() -> dict[str, dict[str, str]]:
    if not NOT_APPLICABLE_PATH.exists():
        return {}
    return json.loads(NOT_APPLICABLE_PATH.read_text())["ids"]


def flat(expected: dict[str, Any]) -> dict[str, str]:
    """node id -> outcome; a file-level string is keyed by the file itself."""
    out: dict[str, str] = {}
    for path, tests in expected["files"].items():
        if isinstance(tests, str):
            out[path] = tests
            continue
        for test, value in tests.items():
            if isinstance(value, str):
                out[join_id(path, test, "")] = value
            else:
                for param, outcome in value.items():
                    out[join_id(path, test, param)] = outcome
    return out


def nest(outcomes: dict[str, str]) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for node_id, outcome in sorted(outcomes.items()):
        if "::" not in node_id:
            files[node_id] = outcome
            continue
        path, test, param = split_id(node_id)
        tests = files.setdefault(path, {})
        if param:
            slot = tests.setdefault(test, {})
            slot[param] = outcome
        else:
            tests[test] = outcome
    return files


def _expand(run: dict[str, Any], other: dict[str, Any]) -> dict[str, str]:
    outcomes = dict(run["outcomes"])
    for module, reason in run["collection_skipped"].items():
        ids = [i for i in other["outcomes"] if i.split("::", 1)[0] == module]
        if ids:
            for node_id in ids:
                outcomes[node_id] = f"{COLLECTION_SKIPPED}{reason}"
        else:
            outcomes[module] = f"{COLLECTION_SKIPPED}{reason}"
    return outcomes


def _apply_not_applicable(env: str, outcomes: dict[str, str]) -> dict[str, str]:
    for node_id, entry in not_applicable().items():
        if env not in entry:
            continue
        actual = outcomes.get(node_id, NOT_COLLECTED)
        if actual != entry[env]:
            raise SystemExit(
                f"not_applicable.json: {node_id} is {actual!r} in {env}, recorded as {entry[env]!r}"
            )
        outcomes[node_id] = f"{NOT_APPLICABLE}{entry['note']}"
    return outcomes


def _problems_of_run(env: str, run: dict[str, Any], scope: Iterable[str] = ()) -> list[str]:
    found = [
        f"{env}: collection error in {k}: {v}"
        for k, v in run["collection_errors"].items()
        if _in_scope(k, scope)
    ]
    for node_id, outcome in run["outcomes"].items():
        bad = outcome.split(":", 1)[0] in {"failed", "error", "xpassed", "xfailed"}
        if bad and _in_scope(node_id, scope):
            found.append(f"{env}: {node_id} {outcome}")
    return found


def _dump(env: str, files: dict[str, Any]) -> str:
    """One line per test file, so a PR's diff reads file by file."""
    lines = [
        f"  {json.dumps(path)}: "
        f"{json.dumps(tests, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
        for path, tests in sorted(files.items())
    ]
    files_text = "{\n" + ",\n".join(lines) + "\n }"
    return f'{{\n "baseline": "{BASELINE}",\n "env": "{env}",\n "files": {files_text}\n}}\n'


def merge(paths: list[Path]) -> dict[str, Any]:
    """Later runs override earlier ones per id (a scoped re-run of a few files)."""
    merged: dict[str, Any] = {"outcomes": {}, "collection_skipped": {}, "collection_errors": {}}
    for path in paths:
        run = json.loads(path.read_text())
        for key in merged:
            merged[key].update(run[key])
    return merged


def write(runs: dict[str, dict[str, Any]], *, provisional: bool = False) -> list[str]:
    problems = [p for env in ENVS for p in _problems_of_run(env, runs[env])]
    if problems and not provisional:
        return problems
    for env in ENVS:
        other = runs["full" if env == "minimal" else "minimal"]
        outcomes = _apply_not_applicable(env, _expand(runs[env], other))
        expected_path(env).write_text(_dump(env, nest(outcomes)))
    return problems if not provisional else []


def _in_scope(node_id: str, scope: Iterable[str]) -> bool:
    scope = list(scope)
    path = node_id.split("::", 1)[0]
    return not scope or any(path == s or path.startswith(s.rstrip("/") + "/") for s in scope)


def compare(env: str, run: dict[str, Any], scope: Iterable[str] = ()) -> list[str]:
    """Every difference between a fresh run and the committed expected outcomes."""
    scope = list(scope)
    recorded = flat(json.loads(expected_path(env).read_text()))
    problems = _problems_of_run(env, run, scope)
    actual = dict(run["outcomes"])
    skipped_modules = dict(run["collection_skipped"])
    by_module: dict[str, list[str]] = {}
    for node_id in recorded:
        by_module.setdefault(node_id.split("::", 1)[0], []).append(node_id)
    for module, reason in skipped_modules.items():
        ids = by_module.get(module, [module])
        for node_id in ids:
            actual[node_id] = f"{COLLECTION_SKIPPED}{reason}"
    na = not_applicable()
    for node_id in sorted(set(recorded) | set(actual)):
        if not _in_scope(node_id, scope):
            continue
        want, got = recorded.get(node_id), actual.get(node_id)
        if want is not None and want.startswith(NOT_APPLICABLE) and env in na.get(node_id, {}):
            want = na[node_id][env]
            got = got or NOT_COLLECTED
        if want == got:
            continue
        if want is None:
            problems.append(f"{env}: new id not in the expected outcomes: {node_id} ({got})")
        elif got is None:
            problems.append(f"{env}: missing id (deselected, lost or not collected): {node_id}")
        else:
            problems.append(f"{env}: {node_id}: expected {want!r}, got {got!r}")
    return problems


def ids_with_outcome(env: str) -> set[str]:
    return set(flat(json.loads(expected_path(env).read_text())))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    write_cmd = sub.add_parser("write")
    write_cmd.add_argument("--minimal", type=Path, nargs="+", required=True)
    write_cmd.add_argument("--full", type=Path, nargs="+", required=True)
    write_cmd.add_argument("--provisional", action="store_true")
    check_cmd = sub.add_parser("check")
    check_cmd.add_argument("env", choices=ENVS)
    check_cmd.add_argument("run", type=Path)
    check_cmd.add_argument("--scope", nargs="*", default=[])
    args = parser.parse_args(argv)
    if args.command == "write":
        runs = {"minimal": merge(args.minimal), "full": merge(args.full)}
        problems = write(runs, provisional=args.provisional)
    else:
        problems = compare(args.env, json.loads(args.run.read_text()), args.scope)
    for line in problems:
        print(f"OUTCOMES {line}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
