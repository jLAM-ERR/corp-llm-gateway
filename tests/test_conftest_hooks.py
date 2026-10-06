"""The hooks tests/conftest.py adds around every test: the stale package attribute guard,
the ``slow`` marker list and the ``--shuffle-seed`` order."""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

import corp_llm_gateway
import corp_llm_gateway.config
import corp_llm_gateway.metrics
from tests import collection_hooks, package_state

ROOT = Path(__file__).resolve().parents[1]

# The guard runs in pytest_runtest_teardown after every fixture's teardown, so the
# monkeypatch below is undone before it looks.


def test_a_clean_process_has_no_stale_package_attribute() -> None:
    assert package_state.stale_package_attributes() == []


def test_a_package_attribute_on_another_module_than_sys_modules_holds_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = sys.modules["corp_llm_gateway.metrics"]
    fresh = types.ModuleType("corp_llm_gateway.metrics")
    monkeypatch.setattr(corp_llm_gateway, "metrics", fresh)

    assert package_state.stale_package_attributes() == [
        f"corp_llm_gateway.metrics is module corp_llm_gateway.metrics at {id(fresh):#x}, "
        f"but sys.modules['corp_llm_gateway.metrics'] is corp_llm_gateway.metrics at {id(live):#x}"
    ]


def test_a_package_attribute_whose_module_left_sys_modules_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = sys.modules["corp_llm_gateway.config"]
    monkeypatch.delitem(sys.modules, "corp_llm_gateway.config")

    assert package_state.stale_package_attributes() == [
        f"corp_llm_gateway.config is module corp_llm_gateway.config at {id(live):#x}, "
        "but sys.modules has no 'corp_llm_gateway.config'"
    ]


def test_restore_rebinds_the_live_module_and_drops_an_unloaded_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = sys.modules["corp_llm_gateway.metrics"]
    monkeypatch.setattr(corp_llm_gateway, "metrics", types.ModuleType("corp_llm_gateway.metrics"))
    unloaded = types.ModuleType("corp_llm_gateway._never_imported")
    try:
        corp_llm_gateway._never_imported = unloaded  # type: ignore[attr-defined]
        assert len(package_state.stale_package_attributes()) == 2

        package_state.restore_package_attributes()

        assert corp_llm_gateway.metrics is live
        assert "_never_imported" not in vars(corp_llm_gateway)
        assert package_state.stale_package_attributes() == []
    finally:
        vars(corp_llm_gateway).pop("_never_imported", None)


def _defines(tree: ast.Module, names: list[str]) -> bool:
    body: list[ast.stmt] = tree.body
    for name in names[:-1]:
        cls = next((s for s in body if isinstance(s, ast.ClassDef) and s.name == name), None)
        if cls is None:
            return False
        body = cls.body
    return any(
        isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == names[-1] for s in body
    )


def test_every_slow_entry_names_a_test_defined_in_its_file() -> None:
    entries = collection_hooks.slow_entries()
    stale = []
    for entry in sorted(entries):
        path, *names = collection_hooks.function_id(entry).split("::")
        source = ROOT / path
        if not path.startswith("tests/") or not source.is_file():
            stale.append(f"{entry}: no file {path}")
        elif not names or not _defines(ast.parse(source.read_text()), names):
            stale.append(f"{entry}: {path} defines no {'::'.join(names)}")

    assert entries
    assert stale == []


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.marks: list[str] = []

    def add_marker(self, mark: pytest.MarkDecorator) -> None:
        self.marks.append(mark.name)


def test_a_slow_entry_marks_its_case_or_every_case_of_its_function() -> None:
    items = [
        _Item("tests/a.py::test_one[x]"),
        _Item("tests/a.py::test_one[y]"),
        _Item("tests/a.py::test_two[x]"),
        _Item("tests/a.py::test_two[y]"),
        _Item("tests/a.py::TestC::test_three"),
        _Item("tests/a.py::test_four[tests/b.py::test_one[x]]"),
    ]
    entries = {"tests/a.py::test_one", "tests/a.py::test_two[y]", "tests/a.py::TestC::test_three"}

    collection_hooks.mark_slow(items, entries)  # type: ignore[arg-type]

    assert [item.marks for item in items] == [["slow"], ["slow"], [], ["slow"], ["slow"], []]
