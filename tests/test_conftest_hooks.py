"""The stale package attribute guard tests/conftest.py runs after every test."""

from __future__ import annotations

import sys
import types

import pytest

import corp_llm_gateway
import corp_llm_gateway.config
import corp_llm_gateway.metrics
from tests import package_state

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
