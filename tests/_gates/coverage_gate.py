"""Per-file line and branch-arc coverage baseline of ``src/`` (supporting gate).

``tests/_manifests/coverage.<env>.json``: per source file, the executed lines as ranges
and the executed arcs. A fresh run may cover more; any line or arc the baseline covers
and the run does not is a drop and fails. The baseline is the intersection of clean
whole-suite runs on 807831a's src (seven in minimal, four in full), so a line only a lucky
interleaving reaches is not in it; an arc shown timing-dependent outside those runs is
taken out by hand and listed in docs/testing/must-keep.md.

``python -m tests._gates.coverage_gate write|intersect|check <env> COVERAGE.json``
(``COVERAGE.json`` is pytest-cov's ``--cov-report=json`` with ``--cov-branch``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = ROOT / "tests" / "_manifests"
BASELINE = "807831a"


def baseline_path(env: str) -> Path:
    return MANIFESTS / f"coverage.{env}.json"


def _ranges(lines: list[int]) -> str:
    out: list[str] = []
    for line in sorted(set(lines)):
        if out and "-" in out[-1] and int(out[-1].split("-")[1]) == line - 1:
            out[-1] = f"{out[-1].split('-')[0]}-{line}"
        elif out and "-" not in out[-1] and int(out[-1]) == line - 1:
            out[-1] = f"{out[-1]}-{line}"
        else:
            out.append(str(line))
    return ",".join(out)


def _unranges(text: str) -> set[int]:
    lines: set[int] = set()
    for part in filter(None, text.split(",")):
        start, _, end = part.partition("-")
        lines |= set(range(int(start), int(end or start) + 1))
    return lines


def _arcs(arcs: list[list[int]]) -> str:
    return ",".join(f"{a}>{b}" for a, b in sorted({tuple(x) for x in arcs}))


def _unarcs(text: str) -> set[tuple[int, int]]:
    return {
        (int(a), int(b)) for a, _, b in (p.partition(">") for p in filter(None, text.split(",")))
    }


def _relative(path: str) -> str:
    absolute = Path(path)
    if absolute.is_absolute():
        try:
            return absolute.relative_to(ROOT).as_posix()
        except ValueError:
            return absolute.as_posix()
    return absolute.as_posix()


def from_report(report: dict[str, Any]) -> dict[str, dict[str, set]]:
    files = {}
    for path, data in report["files"].items():
        files[_relative(path)] = {
            "lines": set(data["executed_lines"]),
            "arcs": {tuple(a) for a in data.get("executed_branches", [])},
        }
    return files


def load(env: str) -> dict[str, dict[str, set]]:
    data = json.loads(baseline_path(env).read_text())
    return {
        path: {"lines": _unranges(entry["lines"]), "arcs": _unarcs(entry["arcs"])}
        for path, entry in data["files"].items()
    }


def save(env: str, files: dict[str, dict[str, set]]) -> None:
    data = {
        "baseline": BASELINE,
        "env": env,
        "files": {
            path: {"lines": _ranges(list(entry["lines"])), "arcs": _arcs(list(entry["arcs"]))}
            for path, entry in sorted(files.items())
        },
    }
    baseline_path(env).write_text(json.dumps(data, indent=0, sort_keys=True) + "\n")


def drops(baseline: dict[str, dict[str, set]], current: dict[str, dict[str, set]]) -> list[str]:
    problems = []
    for path, entry in sorted(baseline.items()):
        now = current.get(path, {"lines": set(), "arcs": set()})
        lost_lines = entry["lines"] - now["lines"]
        lost_arcs = entry["arcs"] - now["arcs"]
        if lost_lines:
            problems.append(f"{path}: lines no longer covered: {_ranges(list(lost_lines))}")
        if lost_arcs:
            problems.append(f"{path}: branch arcs no longer taken: {_arcs(list(lost_arcs))}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("write", "intersect", "check"))
    parser.add_argument("env", choices=("minimal", "full"))
    parser.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    current = from_report(json.loads(args.report.read_text()))
    if args.command == "write":
        save(args.env, current)
        return 0
    baseline = load(args.env)
    if args.command == "intersect":
        merged = {
            path: {
                "lines": entry["lines"] & current.get(path, {"lines": set()})["lines"],
                "arcs": entry["arcs"] & current.get(path, {"arcs": set()})["arcs"],
            }
            for path, entry in baseline.items()
        }
        for line in drops(baseline, current):
            print(f"COVERAGE {args.env} (dropped from the baseline as unstable): {line}")
        save(args.env, merged)
        return 0
    problems = drops(baseline, current)
    for line in problems:
        print(f"COVERAGE {args.env}: {line}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
