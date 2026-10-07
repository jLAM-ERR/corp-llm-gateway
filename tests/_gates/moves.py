"""Moved and renamed tests, mapped back to their baseline node ids (``moves.json``).

Every manifest is keyed by the baseline (``807831a``) node id. A move PR records each
moved test here, current -> baseline, and every gate translates a current id with
``to_baseline`` before it keys, compares or shards, so a true move leaves the manifests
as they were:

- ``files``: ``{current path: baseline path}``, a renamed test module; its tests keep
  their names;
- ``ids``: ``{current function-level id: baseline function-level id}``, a test moved to
  another module or renamed. ``ids`` wins over ``files``.

A function-level id is ``path::name`` or ``path::Class::name``, never with a parametrize
suffix, so a move PR cannot re-parametrise. Inside a suffix (a test parametrised over cited
test ids, like the acceptance matrix) every ``ids`` key and ``files`` key is translated too;
nothing else in it changes.

``python -m tests._gates.moves --check``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "tests" / "_manifests" / "moves.json"
# The last commit whose expected-outcome ledgers recorded every test; Task 10 narrowed them
# to the must-keep ids, so a moves.json value is looked up in these as well.
FULL_LEDGERS_AT = "8936dc6"
NO_FULL_LEDGERS = (
    f"{FULL_LEDGERS_AT} is not in this clone (shallow?): moves.json values outside the "
    "must-keep ledgers cannot be checked against the baseline collection"
)


class MoveCollisionError(ValueError):
    pass


def claim(seen: dict[str, str], current: str) -> str:
    """Record ``current`` under its baseline id in ``seen``; a second current id landing on
    the same baseline id would silently replace the first, so it is refused."""
    baseline = to_baseline(current)
    if baseline in seen and seen[baseline] != current:
        raise MoveCollisionError(
            f"moves.json: {seen[baseline]} and {current} both translate to {baseline}"
        )
    seen[baseline] = current
    return baseline


@dataclass(frozen=True)
class Moves:
    files: dict[str, str]
    ids: dict[str, str]


@cache
def load() -> Moves:
    if not PATH.exists():
        return Moves({}, {})
    data = json.loads(PATH.read_text())
    return Moves(dict(data.get("files", {})), dict(data.get("ids", {})))


def _cut(rest: str) -> tuple[str, str]:
    """``Cls::test_x[p]`` -> (``Cls::test_x``, ``[p]``)."""
    bracket = rest.find("[")
    return (rest, "") if bracket == -1 else (rest[:bracket], rest[bracket:])


def to_baseline_path(path: str) -> str:
    return load().files.get(path, path)


# Keyed by object identity; each value holds its Moves, so the id cannot be reused by a
# reloaded map while the entry lives.
_PATTERNS: dict[int, tuple[Moves, re.Pattern[str] | None]] = {}


def _pattern(moves: Moves) -> re.Pattern[str] | None:
    """Every ``ids`` and ``files`` key as a whole token: longest first, ``ids`` before
    ``files`` at equal length; a key is never matched inside a longer name or path."""
    cached = _PATTERNS.get(id(moves))
    if cached is None or cached[0] is not moves:
        keys = sorted(
            [(k, 0) for k in moves.ids] + [(k, 1) for k in moves.files],
            key=lambda item: (-len(item[0]), item[1]),
        )
        alternatives = "|".join(re.escape(k) for k, _ in keys)
        compiled = re.compile(rf"(?<![\w./])(?:{alternatives})(?!\w)") if keys else None
        cached = _PATTERNS[id(moves)] = (moves, compiled)
    return cached[1]


def _param(moves: Moves, param: str) -> str:
    """The parametrize suffix with every moved id and module path in it translated."""
    pattern = _pattern(moves) if param else None
    if pattern is None:
        return param
    return pattern.sub(lambda m: moves.ids.get(m.group(0)) or moves.files[m.group(0)], param)


def to_baseline(node_id: str) -> str:
    """The baseline id of a current node id (a bare path is a module-level entry)."""
    moves = load()
    if not moves.files and not moves.ids:
        return node_id
    path, sep, rest = node_id.partition("::")
    if not sep:
        return moves.files.get(path, path)
    function, param = _cut(rest)
    param = _param(moves, param)
    moved = moves.ids.get(f"{path}::{function}")
    if moved is not None:
        return moved + param
    return f"{moves.files.get(path, path)}::{function}{param}"


# Keyed by object identity; each value holds its Moves, so the id cannot be reused by a
# reloaded map while the entry lives.
_REVERSED: dict[int, tuple[Moves, Moves]] = {}


def _reversed(moves: Moves) -> Moves:
    cached = _REVERSED.get(id(moves))
    if cached is None or cached[0] is not moves:
        flipped = Moves(
            {v: k for k, v in moves.files.items()}, {v: k for k, v in moves.ids.items()}
        )
        cached = _REVERSED[id(moves)] = (moves, flipped)
    return cached[1]


def from_baseline(node_id: str) -> str:
    """Where a baseline id lives now; itself when the map names no current id for it."""
    moves = load()
    if not moves.files and not moves.ids:
        return node_id
    back = _reversed(moves)
    path, sep, rest = node_id.partition("::")
    if not sep:
        return back.files.get(path, path)
    function, param = _cut(rest)
    param = _param(back, param)
    candidate = back.ids.get(f"{path}::{function}")
    if candidate is not None:
        candidate += param
    else:
        candidate = f"{back.files.get(path, path)}::{function}{param}"
    return candidate if to_baseline(candidate) == node_id else node_id


@cache
def full_ledger_ids() -> frozenset[str] | None:
    """Every id the two ledgers recorded at ``FULL_LEDGERS_AT``; None without that commit."""
    from tests._gates import ledger

    found: set[str] = set()
    for env in ledger.ENVS:
        shown = subprocess.run(
            ["git", "show", f"{FULL_LEDGERS_AT}:tests/_manifests/expected_outcomes.{env}.json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if shown.returncode:
            return None
        found |= set(ledger.flat(json.loads(shown.stdout)))
    return frozenset(found)


def _function_ids(ids: set[str]) -> set[str]:
    return {i.partition("::")[0] + "::" + _cut(i.partition("::")[2])[0] for i in ids if "::" in i}


def problems() -> list[str]:
    """The refusals: every entry must map a test that moved, 1:1, straight to its baseline."""
    from tests._gates import ledger
    from tests._gates.inventory import _tests_in, suite_modules

    moves = load()
    # What the inventory walks: the modules pytest collects.
    tree_paths = {module.rel for module in suite_modules()}
    tree_ids = {f"{m.rel}::{qual}" for m in suite_modules() for qual, _, _ in _tests_in(m)}
    recorded = {i for env in ledger.ENVS for i in ledger.ids_with_outcome(env)}
    found: list[str] = []
    full = full_ledger_ids()
    if full is not None:
        recorded |= full
    elif os.environ.get("CI"):
        found.append(NO_FULL_LEDGERS)
    else:
        warnings.warn(NO_FULL_LEDGERS, stacklevel=2)
    baseline_ids = _function_ids(recorded)
    baseline_paths = {i.split("::", 1)[0] for i in recorded}

    def refuse(kind: str, key: str, value: str, why: str) -> None:
        found.append(f"moves.json {kind}: {key!r} -> {value!r}: {why}")

    for kind, entries in (("files", moves.files), ("ids", moves.ids)):
        is_ids = kind == "ids"
        present, baseline = (tree_ids, baseline_ids) if is_ids else (tree_paths, baseline_paths)
        seen: dict[str, str] = {}
        for key, value in entries.items():
            if "[" in key or "[" in value:
                refuse(kind, key, value, "a parametrize suffix; map the function-level id")
            if is_ids != ("::" in key) or is_ids != ("::" in value):
                what = "a function-level node id" if is_ids else "a module path"
                refuse(kind, key, value, f"each side must be {what}")
            if key not in present:
                refuse(kind, key, value, "the key is not in the current tree")
            if value in present:
                refuse(kind, key, value, "the value is still in the current tree (no move)")
            if value not in baseline and full is not None:
                where = "collection (ledgers)" if is_ids else "ledgers' paths"
                refuse(kind, key, value, f"the value is not in the baseline {where}")
            if value in seen:
                refuse(kind, key, value, f"the value is also mapped from {seen[value]!r} (not 1:1)")
            seen.setdefault(value, key)
            chained = value in entries or (is_ids and value.split("::", 1)[0] in moves.files)
            if chained:
                refuse(kind, key, value, "the value is itself moved; map to the baseline id")
    translated: dict[str, str] = {}
    for node_id in sorted(tree_ids):
        baseline = to_baseline(node_id)
        if baseline in translated:
            found.append(
                f"moves.json: {translated[baseline]!r} and {node_id!r} both translate to "
                f"{baseline!r}"
            )
        translated.setdefault(baseline, node_id)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", required=True)
    parser.parse_args(argv)
    found = problems()
    for line in found:
        print(f"MOVES: {line}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
