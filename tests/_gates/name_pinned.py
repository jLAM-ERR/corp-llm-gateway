"""Name-pinned reference index: every test the acceptance matrix or a doc cites by name.

A move or rename of one of these is a documentation change in the same PR. The index
(``tests/_manifests/name_pinned.json``) maps each pinned node id to the ``file:line``
sites citing it, and each cited test path to its sites; ``allow`` lists citations that
are deliberately not real tests (illustrative placeholders).

``python -m tests._gates.name_pinned --write|--check``
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
INDEX_PATH = ROOT / "tests" / "_manifests" / "name_pinned.json"
MATRIX = "tests/litellm_hook/test_acceptance_matrix.py"
# The citation sources, one segment per `*` (docs/testing/ is not one: it documents the
# gates and cites tests as examples of their rules).
DOC_GLOBS = (
    "CLAUDE.md",
    "README.md",
    "docs/*.md",
    "docs/ops/*.md",
    "compose/*.md",
    "compose/nginx/*.md",
)
# Ignored by git, so absent on a clean checkout; never a citation source.
UNTRACKED_DOCS = {"docs/remaining-steps.md", "docs/requirements-compliance.md"}

_TOKEN = re.compile(
    r"(?P<path>(?:tests/[\w./{},*-]*|(?<![\w/.-])test_[\w{},*-]*)\.py)"
    r"(?:::(?P<pname>test_[\w{},]+))?"
    r"|(?P<colon>::)?(?<![\w/.-])(?P<name>test_[\w{},]+)(?![\w{},]*(?:\.py|\*|/))"
)


def _expand(text: str) -> list[str]:
    match = re.search(r"\{([^{}]*)\}", text)
    if not match:
        return [text]
    head, tail = text[: match.start()], text[match.end() :]
    return [v for option in match.group(1).split(",") for v in _expand(head + option + tail)]


def _sources(paths: list[str]) -> list[str]:
    return sorted(p for p in set(paths) if p not in UNTRACKED_DOCS and (ROOT / p).is_file())


def git_doc_files() -> list[str]:
    # `:(glob)` keeps `*` inside one path segment, as Path.glob does.
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", *(f":(glob){g}" for g in DOC_GLOBS)],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout
    return _sources([p for p in out.decode().split("\0") if p])


def tree_doc_files() -> list[str]:
    return _sources([p.relative_to(ROOT).as_posix() for g in DOC_GLOBS for p in ROOT.glob(g)])


def doc_files() -> list[str]:
    try:
        return git_doc_files()
    except (OSError, subprocess.SubprocessError):
        return tree_doc_files()


@cache
def _test_ids() -> dict[str, list[str]]:
    """test function name -> function-level node ids, from the AST inventory walker."""
    from tests._gates.inventory import _tests_in, suite_modules

    by_name: dict[str, list[str]] = {}
    for module in suite_modules():
        for qual, _, _ in _tests_in(module):
            by_name.setdefault(qual.rsplit("::", 1)[-1], []).append(f"{module.rel}::{qual}")
    return by_name


@cache
def _test_paths() -> list[str]:
    from tests._gates.inventory import suite_files

    return [p.relative_to(ROOT).as_posix() for p in suite_files()]


def _resolve_path(token: str) -> list[str]:
    if token.startswith("tests/"):
        if "*" in token:
            return sorted(p.relative_to(ROOT).as_posix() for p in ROOT.glob(token))
        return [token]
    pattern = re.compile(re.escape(token).replace(r"\*", r"[\w-]*") + "$")
    return sorted(p for p in _test_paths() if pattern.search(p.rsplit("/", 1)[-1]))


def _ids_for(name: str, context: list[str]) -> list[str]:
    candidates = _test_ids().get(name, [])
    in_context = [c for c in candidates if c.split("::", 1)[0] in context]
    return in_context or candidates


def citations() -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """(id -> sites, path -> sites, unresolved citation -> sites)."""
    ids: dict[str, set[str]] = {}
    paths: dict[str, set[str]] = {}
    unresolved: dict[str, set[str]] = {}
    for doc in doc_files():
        context: list[str] = []
        for number, line in enumerate((ROOT / doc).read_text().splitlines(), 1):
            if not line.strip():
                context = []
                continue
            site = f"{doc}:{number}"
            for match in _TOKEN.finditer(line):
                if match.group("path"):
                    resolved = [r for t in _expand(match.group("path")) for r in _resolve_path(t)]
                    if not resolved:
                        unresolved.setdefault(f"{doc}:{match.group('path')}", set()).add(site)
                    for path in resolved:
                        paths.setdefault(path, set()).add(site)
                    context = resolved or context
                    names = _expand(match.group("pname")) if match.group("pname") else []
                    scope = resolved
                else:
                    names = _expand(match.group("name"))
                    scope = context
                for name in names:
                    found = _ids_for(name, scope)
                    if not found:
                        where = f"{scope[0]}::" if scope else ""
                        unresolved.setdefault(f"{doc}:{where}{name}", set()).add(site)
                    for node_id in found:
                        ids.setdefault(node_id, set()).add(site)
    for node_id, site in _matrix_ids():
        ids.setdefault(node_id, set()).add(site)
    return ids, paths, unresolved


def _matrix_ids() -> list[tuple[str, str]]:
    from tests.litellm_hook.test_acceptance_matrix import _ALL

    text = (ROOT / MATRIX).read_text().splitlines()
    out = []
    for node_id in _ALL:
        name = node_id.rsplit("::", 1)[1]
        line = next((n for n, row in enumerate(text, 1) if re.search(rf"::{name}\b", row)), 0)
        path = node_id.split("::", 1)[0]
        resolved = _ids_for(name, [path]) or [node_id]
        out += [(r, f"{MATRIX}:{line}") for r in resolved if r.split("::", 1)[0] == path]
        if not any(r.split("::", 1)[0] == path for r in resolved):
            out.append((node_id, f"{MATRIX}:{line}"))
    return out


def build() -> dict[str, Any]:
    ids, paths, unresolved = citations()
    return {
        "ids": {k: sorted(v, key=_site_key) for k, v in sorted(ids.items())},
        "paths": {k: sorted(v, key=_site_key) for k, v in sorted(paths.items())},
        "unresolved": {k: sorted(v, key=_site_key) for k, v in sorted(unresolved.items())},
    }


def _site_key(site: str) -> tuple[str, int]:
    path, _, line = site.rpartition(":")
    return path, int(line)


def problems(recorded: dict[str, Any], current: dict[str, Any]) -> list[str]:
    found = []
    known = {i for ids in _test_ids().values() for i in ids}
    for node_id in recorded["ids"]:
        if node_id not in known:
            found.append(f"pinned test is gone: {node_id} (cited at {recorded['ids'][node_id]})")
    for path in recorded["paths"]:
        if not (ROOT / path).exists():
            found.append(f"cited test path does not exist: {path}")
    allow = recorded.get("allow", {})
    for citation, sites in current["unresolved"].items():
        if citation not in allow:
            found.append(f"citation names no test: {citation} at {sites}")
    for node_id in sorted(set(current["ids"]) - set(recorded["ids"])):
        found.append(f"newly cited test not in the index: {node_id} at {current['ids'][node_id]}")
    for node_id in sorted(set(recorded["ids"]) - set(current["ids"])):
        if node_id in known:
            found.append(f"indexed test no longer cited anywhere: {node_id}")
    for path in sorted(set(current["paths"]) ^ set(recorded["paths"])):
        found.append(f"cited test paths changed: {path}")
    for citation in sorted(set(allow) - set(current["unresolved"])):
        found.append(f"allow-listed citation no longer present: {citation}")
    return found


def write(current: dict[str, Any]) -> None:
    previous = json.loads(INDEX_PATH.read_text()) if INDEX_PATH.exists() else {}
    data = {
        "about": (
            "Tests cited by name (acceptance matrix _ALL; docs/*.md, docs/ops/*.md, CLAUDE.md, "
            "README.md, compose/*.md, compose/nginx/*.md). Renaming or moving one updates every "
            "site in the same PR."
        ),
        "allow": previous.get("allow", {}),
        "ids": current["ids"],
        "paths": current["paths"],
    }
    INDEX_PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    current = build()
    if args.write:
        write(current)
        for citation, sites in current["unresolved"].items():
            print(f"UNRESOLVED {citation} {sites}")
        return 0
    found = problems(json.loads(INDEX_PATH.read_text()), current)
    for line in found:
        print(f"NAME-PINNED: {line}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
