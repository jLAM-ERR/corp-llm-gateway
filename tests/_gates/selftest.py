"""Self-tests of the gates: each one breaks or bends what a gate relies on and the gate must
notice. The working tree is restored after every mutation.

Suite index (``tests/_gates/inventory.py``, any venv, synthetic modules): (g) the same
synthetic module re-parsed with another body reaches its own helper every time (no stale
cache entry survives a re-parse); (h) a helper reached only through a function-local
``from tests.… import`` is in the importing test's closure and not in a sibling's that names
it without the import; (i) an import in a nested def, or in another method of a class, binds
in that scope only, so the module-level helper the test calls stays reached; (j) a name one
scope imports from two modules reaches both helpers; (k) an unaliased ``import tests[.x]``,
dotted or bare, is refused with an error; and (l) a nested def or class body that declares
``global helper`` past an enclosing local import still reaches the module-level ``helper``,
and an import under ``global`` / ``nonlocal`` is refused.

Moves (``moves.json``, any venv, synthetic tree and ledgers): (m) a pure move — one test in
a renamed module, one renamed into it — changes no ledger, must-keep answer or negative-log
owner with the map, and without it the ledger and must-keep reject; (o) every refusal of
``moves.problems()``, one entry each, and a valid map that passes; (p) must-keep membership
survives a move either way: out of a ``STEP2_GLOBS`` directory stays in, into one stays out,
a name-pinned id counts at its baseline id, and a test that lands on the NEVER-field tests'
old lines is not one of them; (q) a test that runs a security negative check through a
helper stays in ``security_node_ids`` when it moves by ``ids``, with its helper or without
it; (r) a security helper moved into a module none of its tests lives in makes
``negative_logs --check`` / ``--write`` refuse while the test is must-keep, and only then;
(s) a test parametrised over cited test ids and module paths keeps its baseline ids when the
cited tests move (ledger, ``must_keep.expand``), and a parameter the map does not name stays
as it is. A move map that sends two current tests to one baseline id stops every gate (o).

Outcome ledger (run with the minimal venv, scoped to the mutated must-keep files): a module
that stops being collected, a lost parametrize case, a setup-time skip, a changed
collection-skip reason.

``python -m tests._gates.selftest static|ledger`` (``static`` runs g-s)
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests._gates import inventory, ledger, moves, must_keep, negative_logs

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    # Every one must appear in the gate's output.
    expect: tuple[str, ...]


REPARSED_BODY = (
    "import pytest\n"
    "def first():\n    pass\n"
    "def second():\n    pass\n"
    "@pytest.fixture\n"
    "def value():\n    return {helper}()\n"
    "def test_x(value):\n    pass\n"
)
REPARSED_HELPERS = ("first", "second")


def cache_reparse(rounds: int = 200) -> tuple[bool, str]:
    """Re-parse one synthetic module with alternating bodies, whose fixture calls a different
    helper; each must always reach its own. A fixture or conftest cache keyed by a reused
    ``id()`` hands back the other body's fixture."""
    path = ROOT / "tests" / "_gates" / "selftest_reparsed.py"
    reached: dict[int, set[tuple[str, ...]]] = {0: set(), 1: set()}
    for index in range(rounds):
        which = index % 2
        text = REPARSED_BODY.format(helper=REPARSED_HELPERS[which])
        module = inventory._index("tests._gates.selftest_reparsed", path, text)
        checks = inventory.collect(
            (module, qual, node, cls) for qual, node, cls in inventory._tests_in(module)
        )
        reached[which] |= {tuple(entry["helpers"]) for entry in checks.values()}
        del module, checks
    # The real conftest chain's autouse fixtures add their helpers to both bodies.
    ok = all(
        len(found) == 1
        and REPARSED_HELPERS[which] in next(iter(found))
        and REPARSED_HELPERS[1 - which] not in next(iter(found))
        for which, found in reached.items()
    )
    return ok, f"{rounds} re-parses, helpers reached per body: {reached}"[:240]


LOCAL_HELPER = "def helper(value):\n    assert value\n    return value\n"
LOCAL_SUITE = (
    "def test_imports_it_inside():\n"
    "    if True:\n"
    "        from tests._gates.selftest_local_helper import helper\n"
    "    helper(1)\n"
    "def test_sibling_without_the_import():\n"
    "    helper(1)\n"
)


@contextlib.contextmanager
def _only_modules(*extra: inventory.Module) -> Iterator[None]:
    real = inventory.modules
    mods = {module.name: module for module in extra}
    inventory.modules = lambda: mods
    try:
        yield
    finally:
        inventory.modules = real


def _synthetic_checks(suite: inventory.Module, *others: inventory.Module) -> dict[str, Any]:
    with _only_modules(suite, *others):
        return inventory.collect(
            (suite, qual, node, cls) for qual, node, cls in inventory._tests_in(suite)
        )


def _reached(suite: inventory.Module, *others: inventory.Module) -> dict[str, set[str]]:
    """Test id -> every module-level function or class its closure reaches, as
    ``module.name``, so two helpers that share a name stay apart."""
    found: dict[str, set[str]] = {}
    with _only_modules(suite, *others):
        for qual, node, cls in inventory._tests_in(suite):
            _, closure = inventory.inventory_entry(suite, node, cls)
            found[f"{suite.rel}::{qual}"] = {
                f"{owner.name}.{item.name}"
                for owner, item, kind in closure.items.values()
                if kind == "def" and isinstance(item, inventory.DEFS)
            }
    return found


def _synthetic(name: str, text: str) -> inventory.Module:
    return inventory._index(f"tests._gates.{name}", ROOT / f"tests/_gates/{name}.py", text)


def local_import() -> tuple[bool, str]:
    """A helper reached only through a function-local import is in the importing test's
    closure and ``helpers``; its sibling, which names it without the import, reaches none."""
    suite = _synthetic("selftest_local_suite", LOCAL_SUITE)
    helper = _synthetic("selftest_local_helper", LOCAL_HELPER)
    checks, reached = _synthetic_checks(suite, helper), _reached(suite, helper)
    test = f"{suite.rel}::test_imports_it_inside"
    sibling = f"{suite.rel}::test_sibling_without_the_import"
    ok = (
        reached[test] == {"tests._gates.selftest_local_helper.helper"}
        and checks[test]["helpers"] == ["helper"]
        and reached[sibling] == set()
        and checks[sibling]["helpers"] == []
    )
    return ok, f"reached: {reached}"[:240]


SCOPE_SUITE = (
    "def helper(value):\n    assert value\n    return value\n"
    "def test_a_nested_import_leaves_the_module_helper():\n"
    "    def inner():\n"
    "        from tests._gates.selftest_scope_other import helper\n"
    "        return helper(2)\n"
    "    helper(1)\n"
    "    inner()\n"
    "class Thing:\n"
    "    def a(self):\n"
    "        from tests._gates.selftest_scope_other import helper\n"
    "        return helper(2)\n"
    "    def b(self):\n"
    "        return helper(1)\n"
    "def test_a_method_import_leaves_the_module_helper():\n"
    "    Thing().b()\n"
)
SCOPE_OTHER = "def helper(value):\n    assert value > 1\n    return value\n"


BOTH_HELPERS = "tests._gates.selftest_{}_suite.helper", "tests._gates.selftest_{}_other.helper"


def _missing_helpers(reached: dict[str, set[str]], prefix: str) -> list[str]:
    """Each test that does not reach both same-named helpers, with the one it misses."""
    want = {name.format(prefix) for name in BOTH_HELPERS}
    return [
        f"{node_id.split('::')[1]}: {sorted(want - found)}"
        for node_id, found in reached.items()
        if not want <= found
    ]


def nested_scope_import() -> tuple[bool, str]:
    """An import in a nested def, or in another method of a class, binds there only: each
    test reaches the module-level ``helper`` it calls, and the imported one too."""
    reached = _reached(
        _synthetic("selftest_scope_suite", SCOPE_SUITE),
        _synthetic("selftest_scope_other", SCOPE_OTHER),
    )
    unmet = _missing_helpers(reached, "scope")
    if unmet or len(reached) != 2:
        return False, f"not reached: {unmet}; tests {len(reached)}"[:240]
    return True, f"{len(reached)} tests, each reaches both helpers"


REBOUND_SUITE = (
    "def test_imports_helper_twice():\n"
    "    from tests._gates.selftest_rebound_a import helper\n"
    "    from tests._gates.selftest_rebound_b import helper\n"
    "    helper(1)\n"
)
REBOUND_HELPER = "def helper(value):\n    assert value\n    return value\n"


def rebound_import() -> tuple[bool, str]:
    """One scope imports ``helper`` from two modules: which binding is live depends on
    control flow, so the test reaches both."""
    suite = _synthetic("selftest_rebound_suite", REBOUND_SUITE)
    reached = _reached(
        suite,
        _synthetic("selftest_rebound_a", REBOUND_HELPER),
        _synthetic("selftest_rebound_b", REBOUND_HELPER),
    )
    want = {"tests._gates.selftest_rebound_a.helper", "tests._gates.selftest_rebound_b.helper"}
    found = reached[f"{suite.rel}::test_imports_helper_twice"]
    if found != want:
        return False, f"reached {sorted(found)}, want {sorted(want)}"[:240]
    return True, f"reached both: {sorted(found)}"[:240]


UNALIASED_SUITES = {
    "tests.pkg.other": (
        "def test_x():\n    import tests.pkg.other\n\n    tests.pkg.other.helper()\n"
    ),
    "tests": "def test_x():\n    import tests\n\n    tests.pkg.other.helper()\n",
}


def unaliased_tests_import() -> tuple[bool, str]:
    """An unaliased ``import tests[.x]`` binds only ``tests``, so the inventory refuses it
    outright, dotted or bare."""
    texts = []
    for imported, source in UNALIASED_SUITES.items():
        try:
            _synthetic_checks(_synthetic("selftest_unaliased_suite", source))
        except inventory.UnaliasedTestsImportError as exc:
            texts.append(str(exc))
            expect = (
                "tests/_gates/selftest_unaliased_suite.py:2:",
                f"an unaliased `import {imported}`",
                "`from tests.x import name`",
                "`import tests.x as alias`",
            )
            unmet = [e for e in expect if e not in texts[-1]]
            if unmet:
                return False, f"not in the error: {unmet}"[:240]
        else:
            return False, f"accepted `import {imported}`: no error raised"
    return True, f"{len(texts)} refused, e.g. {texts[-1]}"[:240]


GLOBAL_SUITE = (
    "def helper(value):\n    assert value\n    return value\n"
    "def test_a_global_in_a_nested_def_reaches_the_module_helper():\n"
    "    from tests._gates.selftest_global_other import helper\n"
    "    def inner():\n"
    "        global helper\n"
    "        return helper(1)\n"
    "    helper(2)\n"
    "    inner()\n"
    "def test_a_global_in_a_class_body_reaches_the_module_helper():\n"
    "    from tests._gates.selftest_global_other import helper\n"
    "    class C:\n"
    "        global helper\n"
    "        made = helper(1)\n"
    "    helper(2)\n"
)
GLOBAL_OTHER = "def helper(value):\n    assert value > 1\n    return value\n"
REBINDING_SUITES = {
    "global": (
        "def helper():\n    pass\n"
        "def test_x():\n"
        "    def inner():\n"
        "        global helper\n"
        "        from tests._gates.selftest_global_other import helper\n"
        "    inner()\n"
        "    helper()\n"
    ),
    "nonlocal": (
        "def test_x():\n"
        "    from tests._gates.selftest_global_other import helper\n"
        "    def inner():\n"
        "        nonlocal helper\n"
        "        from tests._gates.selftest_rebound_a import helper\n"
        "    inner()\n"
        "    helper(1)\n"
    ),
}


def global_in_nested_scope() -> tuple[bool, str]:
    """A nested def or class body declares ``global helper`` inside a test that imports
    another ``helper``: it calls the module-level one, so each test reaches both. A scope
    that imports a ``tests`` name it declares ``global`` / ``nonlocal`` is refused."""
    reached = _reached(
        _synthetic("selftest_global_suite", GLOBAL_SUITE),
        _synthetic("selftest_global_other", GLOBAL_OTHER),
    )
    unmet = _missing_helpers(reached, "global")
    if unmet or len(reached) != 2:
        return False, f"not reached: {unmet}; tests {len(reached)}"[:240]
    for kind, source in REBINDING_SUITES.items():
        try:
            _synthetic("selftest_rebinding_suite", source)
        except inventory.RebindingImportError as exc:
            if f"`{kind} helper`" not in str(exc):
                return False, f"{kind}: not named in the error: {exc}"[:240]
        else:
            return False, f"accepted an import under `{kind} helper`"
    return True, (
        f"{len(reached)} tests, each reaches both helpers; an import under global / nonlocal "
        "refused"
    )


@contextlib.contextmanager
def _world(
    mods: Iterable[inventory.Module],
    *,
    files: dict[str, str] | None = None,
    ids: dict[str, str] | None = None,
    ledgers: Path | None = None,
    committed: list[str] | None = None,
    pinned: list[str] | None = None,
    negative: Path | None = None,
) -> Iterator[None]:
    """Every gate sees only ``mods`` and this move map; when given, the expected outcomes
    in ``ledgers`` (``<env>.json``), this must-keep list and these name-pinned ids. The real
    ones are back on exit."""
    tree = {module.name: module for module in mods}
    the_map = moves.Moves(dict(files or {}), dict(ids or {}))
    patches: list[tuple[Any, str, Any]] = [
        (inventory, "modules", lambda: tree),
        (must_keep, "modules", lambda: tree),
        (negative_logs, "modules", lambda: tree),
        (
            negative_logs,
            "build_inventory",
            lambda: inventory.collect(
                (m, q, n, c) for m in tree.values() for q, n, c in inventory._tests_in(m)
            ),
        ),
        (moves, "load", lambda: the_map),
        (ledger, "not_applicable", dict),
    ]
    if ledgers is not None:
        patches.append((ledger, "expected_path", lambda env: ledgers / f"{env}.json"))
    if committed is not None:
        patches.append((must_keep, "read", lambda: list(committed)))
    if negative is not None:
        patches.append((negative_logs, "PATH", negative))
    with tempfile.TemporaryDirectory() as tmp:
        if pinned is not None:
            manifests = Path(tmp)
            (manifests / "negative_log_checks.json").write_bytes(
                (must_keep.MANIFESTS / "negative_log_checks.json").read_bytes()
            )
            (manifests / "name_pinned.json").write_text(json.dumps({"ids": dict.fromkeys(pinned)}))
            patches.append((must_keep, "MANIFESTS", manifests))
        saved = [(owner, name, getattr(owner, name)) for owner, name, _ in patches]
        try:
            for owner, name, value in patches:
                setattr(owner, name, value)
            yield
        finally:
            for owner, name, value in saved:
                setattr(owner, name, value)


def _write_ledgers(where: Path, outcomes: dict[str, str]) -> None:
    for env in ledger.ENVS:
        (where / f"{env}.json").write_text(ledger._dump(env, ledger.nest(outcomes)))


MOVE_OLD = "tests/_gates/selftest_move_old.py"
MOVE_NEW = "tests/_gates/selftest_move_new.py"
MOVE_SUITE = (
    "import pytest\n"
    "def helper(value):\n    assert value\n    return value\n"
    "def test_kept_name(caplog):\n"
    "    helper(1)\n"
    "    assert 'secret' not in caplog.text\n"
    "@pytest.mark.parametrize('n', [1, 2])\n"
    "def test_old_name(n):\n"
    "    assert helper(n) == n\n"
)
MOVED_SUITE = MOVE_SUITE.replace("def test_old_name(", "def test_new_name(")
# A module collection-skipped in full and recorded there as one file-level entry.
SKIPPED_OLD = "tests/_gates/selftest_move_skipped_old.py"
SKIPPED_NEW = "tests/_gates/selftest_move_skipped_new.py"
MOVE_MAP = {
    "files": {MOVE_NEW: MOVE_OLD, SKIPPED_NEW: SKIPPED_OLD},
    "ids": {f"{MOVE_NEW}::test_new_name": f"{MOVE_OLD}::test_old_name"},
}


def _module_at(rel: str, text: str) -> inventory.Module:
    return inventory._index(inventory._module_name(ROOT / rel), ROOT / rel, text)


def _runs(path: str, renamed: str, skipped: str) -> dict[str, dict[str, Any]]:
    """Both environments' runs of the move suite at ``path``: passed in minimal, the module
    collection-skipped in full, with the ``skipped`` module."""
    ids = [f"{path}::test_kept_name", *(f"{path}::{renamed}[{n}]" for n in (1, 2))]
    empty: dict[str, str] = {}
    return {
        "minimal": {
            "outcomes": dict.fromkeys(ids, "passed"),
            "collection_skipped": empty,
            "collection_errors": empty,
        },
        "full": {
            "outcomes": empty,
            "collection_skipped": {path: "selftest", skipped: "selftest"},
            "collection_errors": empty,
        },
    }


def _move_world(suite: str, mapped: bool) -> dict[str, list[str]]:
    """What each gate says about ``suite`` at ``MOVE_NEW`` against the baseline recorded
    from ``MOVE_SUITE`` at ``MOVE_OLD``."""
    old, new = _module_at(MOVE_OLD, MOVE_SUITE), _module_at(MOVE_NEW, suite)
    with tempfile.TemporaryDirectory() as tmp:
        recorded_dir, rewritten_dir = Path(tmp) / "recorded", Path(tmp) / "rewritten"
        recorded_dir.mkdir()
        rewritten_dir.mkdir()
        with _world([old], ledgers=recorded_dir):
            ledger.write(_runs(MOVE_OLD, "test_old_name", SKIPPED_OLD))
            checks = inventory.collect((old, q, n, c) for q, n, c in inventory._tests_in(old))
            reviewed = {negative_logs._key(site) for site in negative_logs.sites()}
            committed = must_keep.expand(dict.fromkeys(checks))
        runs = _runs(MOVE_NEW, "test_new_name", SKIPPED_NEW)
        the_map = MOVE_MAP if mapped else {}
        with _world([new], **the_map, ledgers=recorded_dir, committed=committed):
            out = {
                "ledger": [p for env in ledger.ENVS for p in ledger.compare(env, runs[env])],
                "ledger in scope": ledger.compare(
                    "minimal", {**runs["minimal"], "outcomes": {}}, [MOVE_NEW]
                ),
                "ledger, skipped module's reason": ledger.compare(
                    "full",
                    {
                        "outcomes": {},
                        "collection_skipped": {SKIPPED_NEW: "selftest: changed"},
                        "collection_errors": {},
                    },
                    [SKIPPED_NEW],
                ),
                "must-keep": [p for p in must_keep.problems() if "selftest_move" in p],
                "negative logs": sorted(
                    {negative_logs._key(site) for site in negative_logs.sites()} ^ reviewed
                ),
            }
        with _world([new], **the_map, ledgers=rewritten_dir):
            ledger.write(runs)
        out["ledger rewrite"] = [
            env
            for env in ledger.ENVS
            if (recorded_dir / f"{env}.json").read_bytes()
            != (rewritten_dir / f"{env}.json").read_bytes()
        ]
    return out


def pure_move() -> tuple[bool, str]:
    """A test in a renamed module and a test renamed into it: with the map, no gate reports
    anything and a ledger rewrite is byte-identical; without it, the ledger (missing id, in
    scope too) and must-keep (test is gone) reject. A moved module's changed collection-skip
    reason is reported under its baseline path."""
    with_map, without = _move_world(MOVED_SUITE, True), _move_world(MOVED_SUITE, False)
    reason = "ledger, skipped module's reason"
    checked_apart = {"ledger in scope", reason}
    noisy = {gate: lines for gate, lines in with_map.items() if gate not in checked_apart and lines}
    want_skipped = f"{SKIPPED_OLD} (now {SKIPPED_NEW}): expected 'collection-skipped:selftest'"
    if not any(want_skipped in line for line in with_map[reason]):
        noisy[reason] = with_map[reason]
    want_in_scope = f"missing id (deselected, lost or not collected): {MOVE_OLD}::test_kept_name"
    if not any(want_in_scope in line for line in with_map["ledger in scope"]):
        noisy["ledger in scope"] = with_map["ledger in scope"]
    if noisy:
        return False, f"the mapped move is not a no-op: {noisy}"[:240]
    expect = {
        "ledger": f"missing id (deselected, lost or not collected): {MOVE_OLD}::test_kept_name",
        "must-keep": f"must-keep test is gone: {MOVE_OLD}::test_kept_name",
        "negative logs": f"{MOVE_NEW}::test_kept_name|",
        "ledger rewrite": "minimal",
    }
    unmet = [g for g, e in expect.items() if not any(e in line for line in without[g])]
    if unmet:
        return False, f"unmapped move not rejected by: {unmet}"[:240]
    return (
        True,
        f"mapped: no gate line, ledgers byte-identical; unmapped: {without['must-keep'][0]}"[:240],
    )


_OLD = "tests/_gates/selftest_moves_old.py"
_OLD2 = "tests/_gates/selftest_moves_old2.py"
_NEW = "tests/_gates/test_selftest_moves_new.py"
_NEW2 = "tests/_gates/test_selftest_moves_new2.py"
_STAY = "tests/_gates/test_selftest_moves_stay.py"
# Not a test_*.py module: pytest does not collect it, so the inventory does not walk it.
_NOT_COLLECTED = "tests/_gates/selftest_moves_helper.py"
REFUSAL_TREE = {
    _NEW: "def test_a():\n    pass\ndef test_b():\n    pass\n",
    # A different body from _NEW's test_a: a collision must not hide it.
    _NEW2: "def test_a():\n    assert 1\ndef test_x():\n    pass\n",
    _STAY: "def test_s():\n    pass\n",
    _NOT_COLLECTED: "def test_h():\n    pass\n",
}
REFUSAL_BASELINE = (
    f"{_OLD}::test_a",
    f"{_OLD}::test_b",
    f"{_OLD}::test_c[p1]",
    f"{_OLD2}::test_x",
    f"{_STAY}::test_s",
)


@dataclass(frozen=True)
class Refusal:
    case: str
    files: dict[str, str]
    ids: dict[str, str]
    # The refusal line must hold both: why, and the entry it refuses.
    says: str
    names: str


REFUSALS = (
    Refusal(
        "ids key not in the tree",
        {},
        {f"{_NEW}::test_zz": f"{_OLD}::test_a"},
        "key is not in",
        f"{_NEW}::test_zz",
    ),
    Refusal(
        "files key not in the tree",
        {"tests/_gates/selftest_moves_gone.py": _OLD},
        {},
        "key is not in",
        "selftest_moves_gone.py",
    ),
    Refusal(
        "ids value still in the tree",
        {},
        {f"{_NEW}::test_a": f"{_STAY}::test_s"},
        "still in the current tree",
        f"{_STAY}::test_s",
    ),
    Refusal("files value still in the tree", {_NEW: _STAY}, {}, "still in the current tree", _STAY),
    Refusal(
        "ids value outside the baseline",
        {},
        {f"{_NEW}::test_a": f"{_OLD}::test_q"},
        "not in the baseline",
        f"{_OLD}::test_q",
    ),
    Refusal(
        "files value outside the baseline",
        {_NEW: "tests/_gates/selftest_moves_never.py"},
        {},
        "not in the baseline",
        "selftest_moves_never.py",
    ),
    Refusal(
        "ids value used twice",
        {},
        {f"{_NEW}::test_a": f"{_OLD}::test_a", f"{_NEW}::test_b": f"{_OLD}::test_a"},
        "not 1:1",
        f"{_NEW}::test_b",
    ),
    Refusal("files value used twice", {_NEW: _OLD, _NEW2: _OLD}, {}, "not 1:1", _NEW2),
    Refusal(
        "ids value is an ids key",
        {},
        {f"{_NEW2}::test_x": f"{_NEW}::test_a", f"{_NEW}::test_a": f"{_OLD}::test_a"},
        "itself moved",
        f"{_NEW2}::test_x",
    ),
    Refusal("files value is a files key", {_NEW2: _NEW, _NEW: _OLD}, {}, "itself moved", _NEW2),
    Refusal(
        "ids value in a moved file",
        {_NEW: _OLD},
        {f"{_NEW2}::test_x": f"{_NEW}::test_b"},
        "itself moved",
        f"{_NEW2}::test_x",
    ),
    Refusal(
        "ids key in a module pytest does not collect",
        {},
        {f"{_NOT_COLLECTED}::test_h": f"{_OLD}::test_b"},
        "key is not in",
        f"{_NOT_COLLECTED}::test_h",
    ),
    Refusal(
        "parametrised key",
        {},
        {f"{_NEW}::test_a[p1]": f"{_OLD}::test_c"},
        "parametrize suffix",
        f"{_NEW}::test_a[p1]",
    ),
    Refusal(
        "parametrised value",
        {},
        {f"{_NEW}::test_a": f"{_OLD}::test_c[p1]"},
        "parametrize suffix",
        f"{_OLD}::test_c[p1]",
    ),
    Refusal(
        "two current ids, one baseline id",
        {_NEW: _OLD},
        {f"{_NEW2}::test_a": f"{_OLD}::test_a"},
        "both translate to",
        f"{_NEW2}::test_a",
    ),
)
VALID_MOVES = ({_NEW: _OLD}, {f"{_NEW2}::test_x": f"{_OLD2}::test_x"})
COLLISION = REFUSALS[-1]


def _collision_refused_by_every_gate(tree: list[inventory.Module], ledgers: Path) -> list[str]:
    """The gates that let two current ids land on one baseline id without an error."""
    suite = [m for m in tree if m.path.name.startswith("test_")]
    current = [f"{m.rel}::{q}" for m in suite for q, _, _ in inventory._tests_in(m)]
    calls = {
        "index": lambda: inventory.collect(
            (m, q, n, c) for m in suite for q, n, c in inventory._tests_in(m)
        ),
        "must-keep": must_keep._functions,
        "ledger": lambda: ledger._translated(dict.fromkeys(current, "passed")),
    }
    silent = []
    with _world(tree, files=COLLISION.files, ids=COLLISION.ids, ledgers=ledgers):
        for gate, call in calls.items():
            try:
                call()
            except moves.MoveCollisionError as exc:
                if COLLISION.names not in str(exc):
                    silent.append(f"{gate} (does not name the entry)")
            else:
                silent.append(gate)
    return silent


def move_refusals() -> tuple[bool, str]:
    """Each malformed ``moves.json`` entry is refused by name; a valid map is not."""
    tree = [_module_at(rel, text) for rel, text in REFUSAL_TREE.items()]
    with tempfile.TemporaryDirectory() as tmp:
        _write_ledgers(Path(tmp), dict.fromkeys(REFUSAL_BASELINE, "passed"))

        def found(files: dict[str, str], ids: dict[str, str]) -> list[str]:
            with _world(tree, files=files, ids=ids, ledgers=Path(tmp)):
                return moves.problems()

        unmet = [
            r.case
            for r in REFUSALS
            if not any(r.says in line and r.names in line for line in found(r.files, r.ids))
        ]
        control = found(*VALID_MOVES)
        silent = _collision_refused_by_every_gate(tree, Path(tmp))
    if unmet or control or silent:
        return False, (
            f"not refused: {unmet}; valid map refused: {control}; collision passes: {silent}"
        )[:240]
    return True, (
        f"{len(REFUSALS)} refused by name, the valid map passes; the collision also stops "
        "the index, must-keep and the ledger"
    )


PLAIN_AT = ("tests/_gates/selftest_plain.py", "tests/route_gate/selftest_plain.py")
KEPT_AT = ("tests/route_gate/selftest_kept.py", "tests/_gates/selftest_kept.py")
# A test moved into audit/test_logger.py's place, on the lines the NEVER-field tests held.
LOGGER_AT = ("tests/audit/test_logger.py", "tests/_gates/selftest_logger.py")
PINNED_AT = ("tests/_gates/selftest_pinned_old.py", "tests/_gates/selftest_pinned.py")


def membership_move() -> tuple[bool, str]:
    """``STEP2_GLOBS`` and a name-pinned id decide on the baseline identity: a plain test
    moved into ``tests/route_gate/`` stays out, a must-keep one moved out of it (one renamed
    on the way) stays in, a pinned id cited at its new place counts at its old one, and a
    test that lands on ``audit/test_logger.py:135-163`` is not a NEVER-field test."""
    tree = [
        _module_at(PLAIN_AT[1], "def test_plain():\n    pass\n"),
        _module_at(KEPT_AT[1], "def test_kept():\n    pass\ndef test_renamed():\n    pass\n"),
        _module_at(PINNED_AT[1], "def test_pinned():\n    pass\n"),
        _module_at(LOGGER_AT[1], "\n" * 140 + "def test_selftest_on_the_old_lines():\n    pass\n"),
    ]
    files = {
        PLAIN_AT[1]: PLAIN_AT[0],
        KEPT_AT[1]: KEPT_AT[0],
        PINNED_AT[1]: PINNED_AT[0],
        LOGGER_AT[1]: LOGGER_AT[0],
    }
    ids = {f"{KEPT_AT[1]}::test_renamed": f"{KEPT_AT[0]}::test_original"}
    pinned = [f"{PINNED_AT[1]}::test_pinned"]
    selected = {}
    for label, the_map in (("mapped", {"files": files, "ids": ids}), ("unmapped", {})):
        with _world(tree, **the_map, pinned=pinned):
            selected[label] = {i for i in must_keep._rule_ids() if "selftest_" in i}
    want = {
        f"{KEPT_AT[0]}::test_kept",
        f"{KEPT_AT[0]}::test_original",
        f"{PINNED_AT[0]}::test_pinned",
    }
    control = {f"{PLAIN_AT[1]}::test_plain", f"{PINNED_AT[1]}::test_pinned"}
    if selected["mapped"] != want or selected["unmapped"] != control:
        return False, f"selected {selected}"[:240]
    return (
        True,
        f"mapped: {len(want)} kept at their baseline ids, the plain test out; "
        "unmapped: the current path decides",
    )


Q_OLD = "tests/_gates/test_selftest_q_old.py"
Q_NEW = "tests/_gates/test_selftest_q_new.py"
Q_HELPER = "def _assert_clean(log_text):\n    assert 'secret' not in log_text\n"
Q_TEST = "def {name}(caplog):\n    _assert_clean(caplog.text)\n"
Q_IMPORT = "from tests._gates.test_selftest_q_old import _assert_clean\n"
# (case, baseline module, current modules, ids): the tests run the helper's security check.
Q_WORLDS = (
    (
        "helper and test moved together by ids",
        Q_HELPER + Q_TEST.format(name="test_a"),
        {Q_NEW: Q_HELPER + Q_TEST.format(name="test_a")},
        {f"{Q_NEW}::test_a": f"{Q_OLD}::test_a"},
    ),
    (
        "test moved by ids, helper left behind and imported",
        Q_HELPER + Q_TEST.format(name="test_b"),
        {Q_OLD: Q_HELPER, Q_NEW: Q_IMPORT + Q_TEST.format(name="test_b")},
        {f"{Q_NEW}::test_b": f"{Q_OLD}::test_b"},
    ),
)


def _security_node_ids() -> list[str]:
    sites = [{**site, "class": "security"} for site in negative_logs.sites()]
    return negative_logs.node_ids({"sites": sites})


def security_helper_move() -> tuple[bool, str]:
    """A test that runs a security negative check through a helper stays in
    ``security_node_ids`` (so in must-keep) when it moves by ``ids``, with the helper or
    without it."""
    unmet = []
    for case, baseline, current, ids in Q_WORLDS:
        with _world([_module_at(Q_OLD, baseline)]):
            before = _security_node_ids()
        with _world([_module_at(rel, text) for rel, text in current.items()], ids=ids):
            after = _security_node_ids()
        if not before or after != before:
            unmet.append(f"{case}: {before} -> {after}")
    if unmet:
        return False, f"security_node_ids changed: {unmet}"[:240]
    return True, f"{len(Q_WORLDS)} moves, security_node_ids unchanged, e.g. {before}"[:240]


R_SUITE = "tests/_gates/test_selftest_r.py"
# Not a test_*.py module: a shared helper module none of the tests lives in.
R_FIXTURES = "tests/_gates/selftest_r_fixtures.py"
R_TEST = f"{R_SUITE}::test_a"
R_LOST = f"security negative-log id lost while its test is still must-keep: {R_TEST}"


def _negative_logs_cli(argv: list[str]) -> tuple[int, str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = negative_logs.main(argv)
    return code, err.getvalue()


def security_helper_moved_away() -> tuple[bool, str]:
    """A security helper moved into a module none of its tests lives in: its test drops out of
    ``security_node_ids``. While the test is must-keep, ``--check`` and ``--write`` refuse by
    id and ``--write`` leaves the manifest as it was; a test that is not must-keep drops out
    with no refusal (review only)."""
    with _world([_module_at(R_SUITE, Q_HELPER + Q_TEST.format(name="test_a"))]):
        sites = [
            {**site, "class": "security", "note": "selftest"} for site in negative_logs.sites()
        ]
        before = negative_logs.node_ids({"sites": sites})
    # The reviewer carried the helper's row to its new owner, class and note kept.
    moved = [{**site, "owner": f"{R_FIXTURES}::_assert_clean"} for site in sites]
    current = [
        _module_at(R_FIXTURES, Q_HELPER),
        _module_at(
            R_SUITE,
            "from tests._gates.selftest_r_fixtures import _assert_clean\n"
            + Q_TEST.format(name="test_a"),
        ),
    ]
    results: dict[str, tuple[int, str, bool]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "negative_log_checks.json"
        text = json.dumps({"sites": moved, "security_node_ids": before}, indent=1) + "\n"
        for label, committed in (("must-keep", [R_TEST]), ("not must-keep", [])):
            for command in ("--check", "--write"):
                path.write_text(text)
                with _world(current, committed=committed, negative=path):
                    code, err = _negative_logs_cli([command])
                results[f"{label} {command}"] = (code, err, path.read_text() == text)
    unmet = [
        f"{key}: exit {code}"
        for key, (code, err, unchanged) in results.items()
        if key.startswith("must-keep") and (code == 0 or R_LOST not in err or not unchanged)
    ]
    unmet += [
        f"{key}: exit {code} {err.strip()[:80]}"
        for key, (code, err, _) in results.items()
        if key.startswith("not") and (code != 0 or "lost" in err)
    ]
    if before != [R_TEST] or unmet:
        return False, f"baseline ids {before}; not as expected: {unmet}"[:240]
    return True, f"must-keep: --check and --write refuse ({R_LOST}); not must-keep: no refusal"[
        :240
    ]


S_MATRIX = "tests/_gates/test_selftest_s_matrix.py::test_cites"
S_FILE = ("tests/_gates/test_selftest_s_old.py", "tests/_gates/test_selftest_s_new.py")
S_TEST = (
    "tests/_gates/test_selftest_s_old2.py::test_old_name",
    "tests/_gates/test_selftest_s_other.py::test_new_name",
)
S_UNMAPPED = "tests/_gates/test_selftest_s_unmapped.py::test_y"
# (baseline parameter, current parameter): what a matrix-style test is parametrised over.
S_PARAMS = (
    (f"[{S_FILE[0]}::test_kept]", f"[{S_FILE[1]}::test_kept]"),
    (f"[{S_TEST[0]}]", f"[{S_TEST[1]}]"),
    (f"[{S_FILE[0]}]", f"[{S_FILE[1]}]"),
    (f"[{S_TEST[0]}-{S_FILE[0]}::test_kept]", f"[{S_TEST[1]}-{S_FILE[1]}::test_kept]"),
    # Not in the map, or only a longer name / path that starts with a key: as it is.
    (f"[{S_UNMAPPED}]", f"[{S_UNMAPPED}]"),
    (f"[{S_TEST[1]}_extra]", f"[{S_TEST[1]}_extra]"),
    (f"[{S_FILE[1]}x::test_kept]", f"[{S_FILE[1]}x::test_kept]"),
)


def _matrix_runs(which: int) -> dict[str, dict[str, Any]]:
    ids = [f"{S_MATRIX}{pair[which]}" for pair in S_PARAMS]
    run = {
        "outcomes": dict.fromkeys(ids, "passed"),
        "collection_skipped": {},
        "collection_errors": {},
    }
    return dict.fromkeys(ledger.ENVS, run)


def moved_parameters() -> tuple[bool, str]:
    """A test parametrised over cited test ids and module paths: with the map, ids and paths
    inside the suffix translate to their baseline (the ledger compares clean, a rewrite is
    byte-identical, ``must_keep.expand`` yields the baseline ids, ``from_baseline`` comes
    back); without it, ``missing id`` / ``new id``. A parameter the map does not name, or
    one that only starts with a key, stays as it is."""
    the_map = {"files": {S_FILE[1]: S_FILE[0]}, "ids": {S_TEST[1]: S_TEST[0]}}
    baseline_ids = sorted(f"{S_MATRIX}{old}" for old, _ in S_PARAMS)
    found: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        recorded, rewritten = Path(tmp) / "recorded", Path(tmp) / "rewritten"
        recorded.mkdir()
        rewritten.mkdir()
        with _world([], ledgers=recorded):
            ledger.write(_matrix_runs(0))
        for label, mapping in (("mapped", the_map), ("unmapped", {})):
            with _world([], **mapping, ledgers=recorded):
                found[f"{label} compare"] = [
                    p for env in ledger.ENVS for p in ledger.compare(env, _matrix_runs(1)[env])
                ]
                found[f"{label} back"] = [
                    moves.from_baseline(f"{S_MATRIX}{old}") for old, _ in S_PARAMS
                ]
            with _world([], **mapping, ledgers=rewritten):
                ledger.write(_matrix_runs(1))
                found[f"{label} expand"] = must_keep.expand({S_MATRIX: "selftest"})
            found[f"{label} rewrite identical"] = all(
                (recorded / f"{env}.json").read_bytes() == (rewritten / f"{env}.json").read_bytes()
                for env in ledger.ENVS
            )
    unmet = []
    if found["mapped compare"]:
        unmet.append(f"mapped compare: {found['mapped compare'][:2]}")
    if not found["mapped rewrite identical"]:
        unmet.append("mapped rewrite differs")
    if found["mapped expand"] != sorted([*baseline_ids, S_MATRIX]):
        unmet.append(f"mapped expand: {found['mapped expand'][:3]}")
    if found["mapped back"] != [f"{S_MATRIX}{new}" for _, new in S_PARAMS]:
        unmet.append(f"mapped from_baseline: {found['mapped back'][:3]}")
    moved = [i for i, (old, new) in enumerate(S_PARAMS) if old != new]
    lines = found["unmapped compare"]
    for index in moved:
        old, new = S_PARAMS[index]
        if not any(
            f"missing id (deselected, lost or not collected): {S_MATRIX}{old}" in line
            for line in lines
        ):
            unmet.append(f"unmapped: no missing id for {old}")
        if not any(
            f"new id not in the expected outcomes: {S_MATRIX}{new}" in line for line in lines
        ):
            unmet.append(f"unmapped: no new id for {new}")
    if unmet:
        return False, f"not as expected: {unmet}"[:240]
    return True, (
        f"mapped: {len(moved)} suffixes translated, compare clean, rewrite identical, expand "
        f"baseline; {len(S_PARAMS) - len(moved)} unmapped suffixes kept; unmapped: "
        f"{len(lines)} missing/new lines"
    )[:240]


def _apply(mutation: Mutation, run: Callable[[], tuple[int, str]]) -> tuple[bool, str]:
    path = ROOT / mutation.path
    original = path.read_text()
    if original.count(mutation.old) != 1:
        return False, f"mutation site not found exactly once in {mutation.path}"
    if not mutation.expect:
        return False, "no expectation: the baseline column it reads is empty"
    try:
        path.write_text(original.replace(mutation.old, mutation.new))
        code, output = run()
    finally:
        path.write_text(original)
    lines = output.splitlines()
    unmet = [e for e in mutation.expect if not any(e in line for line in lines)]
    if code == 0 or unmet:
        return False, f"exit {code}; not reported: {unmet[:3]}"[:240]
    first = next(line for line in lines if mutation.expect[0] in line)
    return True, f"{len(mutation.expect)} expected line(s), e.g. {first}"[:240]


def _ledger_check(scope: str, env_name: str = "minimal") -> Callable[[], tuple[int, str]]:
    def run() -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ledger.json"
            env = {k: v for k, v in os.environ.items() if k not in {"CI", "CORP_TEST_PG_DSN"}}
            env.update(
                {"PYTHONPATH": "src", "CORP_REQUIRE_PROXY_CAPTURE": "1", "CORP_TEST_ENV": env_name}
            )
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    scope,
                    "-q",
                    "-p",
                    "tests._gates.outcome_ledger",
                    f"--outcome-ledger={out}",
                    "-p",
                    "no:cacheprovider",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            if not out.exists():
                return 1, "no ledger written"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "tests._gates.ledger",
                    "check",
                    env_name,
                    str(out),
                    "--scope",
                    scope,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            return result.returncode, result.stdout + result.stderr

    return run


LEDGER_SETUP_SKIP = (
    "\n\n@pytest.fixture(autouse=True)\ndef _selftest_setup_skip() -> None:\n"
    '    pytest.skip("selftest: setup-time skip")\n'
)


def ledger_mutations() -> list[tuple[Mutation, Callable[[], tuple[int, str]]]]:
    store = "tests/storage/test_mapping_store.py"
    team = "tests/team_config/test_store.py"
    ner = "tests/detectors/test_ner_en.py"
    team_text = (ROOT / team).read_text()
    return [
        (
            Mutation(
                "lost parametrize case",
                store,
                'params=[_make_in_memory, _make_redis], ids=["in_memory", "redis"]',
                'params=[_make_in_memory], ids=["in_memory"]',
                ("missing id",),
            ),
            _ledger_check("tests/storage/"),
        ),
        (
            Mutation(
                "setup-time skip",
                team,
                team_text,
                team_text + LEDGER_SETUP_SKIP,
                ("selftest: setup-time skip",),
            ),
            _ledger_check(team),
        ),
        (
            Mutation(
                "changed collection-skip reason",
                ner,
                'pytest.importorskip("spacy")',
                'pytest.importorskip("spacy", reason="selftest: another reason")',
                ("selftest: another reason",),
            ),
            _ledger_check(ner),
        ),
    ]


def missing_module() -> tuple[bool, str]:
    """The module is renamed so pytest no longer collects it, then put back."""
    source = ROOT / "tests/team_config/test_store.py"
    hidden = source.with_name("selftest_hidden_store.py")
    os.rename(source, hidden)
    try:
        code, output = _ledger_check("tests/team_config/")()
    finally:
        os.rename(hidden, source)
    expect = "missing id (deselected, lost or not collected): tests/team_config/test_store.py::"
    first = next((line for line in output.splitlines() if expect in line), "")
    return code != 0 and bool(first), first[:240]


def main(argv: list[str] | None = None) -> int:
    which = (argv or sys.argv[1:] or ["static"])[0]
    results: list[tuple[str, bool, str]] = []
    if which == "static":
        rejected, evidence = cache_reparse()
        results.append(("g: synthetic module re-parsed with another body", rejected, evidence))
        rejected, evidence = local_import()
        results.append(("h: helper reached only by a function-local import", rejected, evidence))
        rejected, evidence = nested_scope_import()
        results.append(
            ("i: a nested-scope import never hides a module-level helper", rejected, evidence)
        )
        rejected, evidence = rebound_import()
        results.append(
            ("j: a name imported twice in one scope reaches both helpers", rejected, evidence)
        )
        rejected, evidence = unaliased_tests_import()
        results.append(("k: an unaliased import tests[.x], dotted or bare", rejected, evidence))
        rejected, evidence = global_in_nested_scope()
        results.append(("l: global / nonlocal past or under a local import", rejected, evidence))
        rejected, evidence = pure_move()
        results.append(("m: a pure move, with and without moves.json", rejected, evidence))
        rejected, evidence = move_refusals()
        results.append(("o: each malformed moves.json entry", rejected, evidence))
        rejected, evidence = membership_move()
        results.append(("p: must-keep membership across a move", rejected, evidence))
        rejected, evidence = security_helper_move()
        results.append(("q: a security helper's test moved by ids", rejected, evidence))
        rejected, evidence = security_helper_moved_away()
        results.append(
            ("r: a security helper moved away from its must-keep test", rejected, evidence)
        )
        rejected, evidence = moved_parameters()
        results.append(("s: moved ids and paths inside a parametrize suffix", rejected, evidence))
    else:
        rejected, evidence = missing_module()
        results.append(("newly missing module", rejected, evidence))
        for mutation, run in ledger_mutations():
            rejected, evidence = _apply(mutation, run)
            results.append((mutation.name, rejected, evidence))
    for name, rejected, evidence in results:
        print(json.dumps({"self-test": name, "rejected": rejected, "evidence": evidence}))
    return 0 if all(rejected for _, rejected, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
