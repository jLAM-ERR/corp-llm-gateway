"""The hooks tests/conftest.py adds around every test: the stale package attribute guard,
the ``slow`` marker list and the ``--shuffle-seed`` order."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

import corp_llm_gateway
import corp_llm_gateway.config
import corp_llm_gateway.metrics
from tests import collection_hooks, package_state

ROOT = Path(__file__).resolve().parents[1]

pytest_plugins = ["pytester"]

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


def test_a_package_attribute_whose_sys_modules_entry_is_not_a_module_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = sys.modules["corp_llm_gateway.metrics"]
    monkeypatch.setitem(sys.modules, "corp_llm_gateway.metrics", None)

    assert package_state.stale_package_attributes() == [
        f"corp_llm_gateway.metrics is module corp_llm_gateway.metrics at {id(live):#x}, "
        "but sys.modules['corp_llm_gateway.metrics'] holds NoneType, not a module"
    ]

    # Recorded so teardown puts the attribute back after the restore drops it.
    monkeypatch.setattr(corp_llm_gateway, "metrics", live)
    package_state.restore_package_attributes()

    assert "metrics" not in vars(corp_llm_gateway)
    assert package_state.stale_package_attributes() == []


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


def test_the_conftest_registers_the_guard_as_a_teardown_wrapper(
    request: pytest.FixtureRequest,
) -> None:
    conftest = ROOT / "tests" / "conftest.py"
    impls = [
        impl
        for impl in request.config.pluginmanager.hook.pytest_runtest_teardown.get_hookimpls()
        if Path(getattr(impl.plugin, "__file__", "") or "").resolve() == conftest.resolve()
    ]

    assert [(impl.function, impl.wrapper) for impl in impls] == [
        (package_state.pytest_runtest_teardown, True)
    ]


def test_the_guard_fails_the_leaking_test_at_teardown_and_restores_for_the_next(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
    pytester.makeconftest("from tests.package_state import pytest_runtest_teardown  # noqa: F401\n")
    pytester.makepyfile(
        test_inner="""
        import sys
        import types

        import corp_llm_gateway
        import corp_llm_gateway.metrics


        def test_leaks():
            corp_llm_gateway.metrics = types.ModuleType("corp_llm_gateway.metrics")


        def test_clean():
            assert corp_llm_gateway.metrics is sys.modules["corp_llm_gateway.metrics"]
        """
    )

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:asyncio", "-rA")

    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*ERROR at teardown of test_leaks*"])
    result.stdout.re_match_lines(
        [
            r"test left package attributes on stale "
            r"modules: \[\"corp_llm_gateway\.metrics is module corp_llm_gateway\.metrics at 0x\w+, "
            r"but sys\.modules\['corp_llm_gateway\.metrics'\] is corp_llm_gateway\.metrics at "
            r"0x\w+\"\]$"
        ]
    )
    result.stdout.fnmatch_lines(
        ["PASSED test_inner.py::test_clean", "ERROR test_inner.py::test_leaks*"]
    )
    assert "ERROR test_inner.py::test_clean" not in result.stdout.str()


def test_a_teardown_error_keeps_its_traceback_names_the_leak_and_restores_for_the_next(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
    pytester.makeconftest("from tests.package_state import pytest_runtest_teardown  # noqa: F401\n")
    pytester.makepyfile(
        test_inner="""
        import sys
        import types

        import pytest

        import corp_llm_gateway
        import corp_llm_gateway.metrics


        @pytest.fixture
        def breaks_at_teardown():
            yield
            raise RuntimeError("the fixture broke at teardown")


        def test_leaks(breaks_at_teardown):
            corp_llm_gateway.metrics = types.ModuleType("corp_llm_gateway.metrics")


        def test_clean():
            assert corp_llm_gateway.metrics is sys.modules["corp_llm_gateway.metrics"]
        """
    )

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider", "-p", "no:asyncio", "-rA")

    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(
        [
            "*RuntimeError: the fixture broke at teardown",
            "E * test also left stale package attributes: *corp_llm_gateway.metrics is module*",
        ]
    )
    assert "test left package attributes on stale modules" not in result.stdout.str()
    result.stdout.fnmatch_lines(
        ["PASSED test_inner.py::test_clean", "ERROR test_inner.py::test_leaks*"]
    )


def test_shuffled_is_a_seeded_permutation_that_keeps_modules_classes_and_scoped_params_together(
    pytester: pytest.Pytester,
) -> None:
    pytester.makepyfile(
        test_a="""
        import pytest

        @pytest.fixture(scope="module", params=["p1", "p2"])
        def wide(request):
            return request.param

        @pytest.mark.parametrize("n", range(4))
        def test_one(wide, n):
            pass

        def test_two():
            pass
        """,
        test_b="""
        class TestC:
            def test_1(self): pass
            def test_2(self): pass
            def test_3(self): pass

        def test_4(): pass
        def test_5(): pass
        """,
        test_c="\n".join(f"def test_{i}(): pass" for i in range(6)),
    )
    items, _ = pytester.inline_genitems("-p", "no:cacheprovider", "-p", "no:asyncio")
    ids = [item.nodeid for item in items]

    orders = {
        seed: [item.nodeid for item in collection_hooks.shuffled(items, seed)] for seed in range(8)
    }

    assert [item.nodeid for item in collection_hooks.shuffled(items, 3)] == orders[3]
    assert any(order != ids for order in orders.values())
    for order in orders.values():
        assert sorted(order) == sorted(ids)
        for prefix in ("test_a.py::", "test_b.py::", "test_b.py::TestC::", "test_c.py::"):
            at = [i for i, nodeid in enumerate(order) if nodeid.startswith(prefix)]
            assert at == list(range(at[0], at[-1] + 1)), (prefix, order)
        params = [nodeid.split("[")[1][:2] for nodeid in order if "[" in nodeid]
        assert params == sorted(params, key=params.index), order


def _collected(*args: str) -> list[str]:
    # A --shuffle-seed in the caller's PYTEST_ADDOPTS would reorder every collection here.
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode in (0, 5), run.stdout + run.stderr
    return [line for line in run.stdout.splitlines() if "::" in line and not line.startswith(" ")]


def test_without_a_seed_the_collection_order_is_unchanged_and_a_seed_only_reorders() -> None:
    files = ["tests/test_ci_workflow.py", "tests/test_conftest_hooks.py"]
    # pytest's own order, every collected test: the same collection without tests/conftest.py.
    native = _collected("--noconftest", *files)

    shuffled = _collected("--shuffle-seed=20261006", *files)

    assert {nodeid.split("::", 1)[0] for nodeid in native} == set(files)
    assert _collected(*files) == native
    assert sorted(shuffled) == sorted(native)
    assert shuffled != native


def test_every_case_level_slow_entry_is_still_collected() -> None:
    cases = sorted(e for e in collection_hooks.slow_entries() if "[" in e)
    files = sorted({e.split("::", 1)[0] for e in cases})
    collected = set(_collected(*files))
    collected_files = {nodeid.split("::", 1)[0] for nodeid in collected}

    # A module collection-skipped in this environment (an absent extra) collects nothing.
    stale = [e for e in cases if e not in collected and e.split("::", 1)[0] in collected_files]

    assert stale == [], (
        "slow entries whose case is no longer collected "
        f"(a module collection-skipped in this environment is allowed): {stale}"
    )


def test_the_slow_list_skips_only_blank_lines_and_whole_line_comments(tmp_path: Path) -> None:
    listing = tmp_path / "slow_tests.txt"
    listing.write_text(
        "# a comment\n\n   # an indented comment\n"
        "tests/a.py::test_one\n  tests/a.py::test_two[x#1]  \n"
        "tests/a.py::test_three # not a comment\n"
    )

    assert collection_hooks.slow_entries(listing) == {
        "tests/a.py::test_one",
        "tests/a.py::test_two[x#1]",
        "tests/a.py::test_three # not a comment",
    }
