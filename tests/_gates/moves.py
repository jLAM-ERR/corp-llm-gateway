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
suffix: the suffix is carried over verbatim, so a move PR cannot re-parametrise.

``python -m tests._gates.moves --check``
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "tests" / "_manifests" / "moves.json"


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


def to_baseline(node_id: str) -> str:
    """The baseline id of a current node id (a bare path is a module-level entry)."""
    moves = load()
    if not moves.files and not moves.ids:
        return node_id
    path, sep, rest = node_id.partition("::")
    if not sep:
        return moves.files.get(path, path)
    function, param = _cut(rest)
    moved = moves.ids.get(f"{path}::{function}")
    if moved is not None:
        return moved + param
    if path in moves.files:
        return f"{moves.files[path]}::{rest}"
    return node_id


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
    candidate = back.ids.get(f"{path}::{function}")
    if candidate is not None:
        candidate += param
    elif path in back.files:
        candidate = f"{back.files[path]}::{rest}"
    else:
        return node_id
    return candidate if to_baseline(candidate) == node_id else node_id


def _function_ids(ids: set[str]) -> set[str]:
    return {i.partition("::")[0] + "::" + _cut(i.partition("::")[2])[0] for i in ids if "::" in i}


def problems() -> list[str]:
    """The refusals: every entry must map a test that moved, 1:1, straight to its baseline."""
    from tests._gates import ledger
    from tests._gates.inventory import _tests_in, modules

    moves = load()
    tree_paths = {module.rel for module in modules().values()}
    tree_ids = {f"{m.rel}::{qual}" for m in modules().values() for qual, _, _ in _tests_in(m)}
    recorded = {i for env in ledger.ENVS for i in ledger.ids_with_outcome(env)}
    baseline_ids = _function_ids(recorded)
    baseline_paths = {i.split("::", 1)[0] for i in recorded}
    found: list[str] = []

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
            if value not in baseline:
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
