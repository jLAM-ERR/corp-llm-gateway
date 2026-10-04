"""Discovery for the hand-reviewed negative-log inventory (``negative_log_checks.json``).

Finds every assertion that checks log output for an absence — ``X not in caplog.text``,
a ``getMessage()`` loop, an alias of either, ``… is None`` over a log haystack, a helper
taking ``log_text`` — and attributes it to the test that runs it (directly or through a
helper). Discovery only: the reviewed ``class`` of each site (``security``, ``behaviour``,
``not-a-log-check``) lives in the manifest, and a site with no review fails ``--check``.

``python -m tests._gates.negative_logs --write|--check``
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tests._gates.inventory import build as build_inventory
from tests._gates.inventory import modules

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "tests" / "_manifests" / "negative_log_checks.json"
_LOG = re.compile(r"caplog|getMessage")
_HELPER_PARAMS = {"log_text", "logs", "log_lines", "text"}


def _functions(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            yield stmt.name, stmt
        elif isinstance(stmt, ast.ClassDef):
            for sub in stmt.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield f"{stmt.name}::{sub.name}", sub


def _tainted(qual: str, fn: Any) -> set[str]:
    names = set()
    if qual.rsplit("::", 1)[-1].startswith("_"):
        names = {a.arg for a in [*fn.args.args, *fn.args.kwonlyargs] if a.arg in _HELPER_PARAMS}
    changed = True
    while changed:
        changed = False
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Assign):
                targets, value = sub.targets, sub.value
            elif isinstance(sub, ast.For | ast.comprehension):
                targets, value = [sub.target], sub.iter
            else:
                continue
            if _is_log(value, names):
                for target in targets:
                    for node in ast.walk(target):
                        if isinstance(node, ast.Name) and node.id not in names:
                            names.add(node.id)
                            changed = True
    return names


def _is_log(node: ast.AST, tainted: set[str]) -> bool:
    text = ast.unparse(node)
    return bool(_LOG.search(text)) or any(re.search(rf"\b{re.escape(t)}\b", text) for t in tainted)


def _negative(test: ast.expr, tainted: set[str]) -> bool:
    for node in ast.walk(test):
        if isinstance(node, ast.Compare) and _is_log(node, tainted):
            if any(isinstance(op, ast.NotIn) for op in node.ops):
                return True
            empty = any(
                isinstance(c, ast.List | ast.Set | ast.Tuple) and not c.elts
                for c in node.comparators
            )
            falsy = isinstance(node.ops[0], ast.Eq | ast.Is) and any(
                isinstance(c, ast.Constant) and c.value in (0, "", None) for c in node.comparators
            )
            if empty or falsy:
                return True
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, ast.Not)
            and _is_log(node.operand, tainted)
        ):
            return True
    return False


def sites() -> list[dict[str, Any]]:
    found = []
    for module in modules().values():
        for qual, fn in _functions(module.tree):
            tainted = _tainted(qual, fn)
            if not tainted and not _LOG.search(ast.unparse(fn)):
                continue
            for sub in ast.walk(fn):
                if isinstance(sub, ast.Assert) and _negative(sub.test, tainted):
                    found.append(
                        {
                            "site": f"{module.rel}:{sub.lineno}",
                            "owner": f"{module.rel}::{qual}",
                            "check": ast.unparse(sub.test)[:200],
                        }
                    )
    return sorted(found, key=lambda s: (s["owner"], s["check"]))


def _key(site: dict[str, Any]) -> str:
    return f"{site['owner']}|{site['check']}"


def node_ids(manifest: dict[str, Any]) -> list[str]:
    """Tests running a security negative check, directly or through a helper."""
    checks, _ = build_inventory()
    security = [s for s in manifest["sites"] if s["class"] == "security"]
    owners = {s["owner"] for s in security}
    helpers = [o for o in owners if o.rsplit("::", 1)[1].startswith("_")]
    helper_names = {owner.rsplit("::", 1)[1] for owner in helpers}
    helper_files = {owner.split("::", 1)[0] for owner in helpers}
    ids = owners - set(helpers)
    for node_id, entry in checks.items():
        if set(entry["helpers"]) & helper_names and node_id.split("::", 1)[0] in helper_files:
            ids.add(node_id)
    return sorted(ids)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    recorded = json.loads(PATH.read_text()) if PATH.exists() else {"sites": []}
    reviewed = {_key(s): s for s in recorded["sites"]}
    current = sites()
    missing = [s for s in current if _key(s) not in reviewed]
    if args.write:
        merged = []
        for site in current:
            old = reviewed.get(_key(site), {})
            review = {"class": old.get("class", "UNREVIEWED"), "note": old.get("note", "")}
            merged.append({**site, **review})
        recorded["sites"] = merged
        recorded["security_node_ids"] = node_ids(recorded)
        PATH.write_text(json.dumps(recorded, indent=1, ensure_ascii=False) + "\n")
        return 0
    problems = [f"unreviewed negative log check: {s['owner']} {s['check']}" for s in missing]
    problems += [
        f"reviewed site no longer present: {key}"
        for key in sorted(set(reviewed) - {_key(s) for s in current})
    ]
    for line in problems:
        print(f"NEGATIVE-LOGS: {line}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
