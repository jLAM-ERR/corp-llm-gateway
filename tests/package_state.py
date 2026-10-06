"""What a test must leave on the ``corp_llm_gateway`` packages as it found it: a package
attribute naming a submodule must be the module ``sys.modules`` holds. A test that drops
a submodule from ``sys.modules`` and re-imports it rebinds the attribute; restoring only
``sys.modules`` leaves the attribute on a stale module, so a later
``from corp_llm_gateway import x`` or a monkeypatch through ``corp_llm_gateway.x`` reaches
a module the code under test never reads, and that test passes or fails by run order."""

from __future__ import annotations

import sys
from collections.abc import Generator
from types import ModuleType

import pytest

PACKAGE = "corp_llm_gateway"

Stale = tuple[ModuleType, str, str]


def _gateway_modules() -> dict[str, ModuleType]:
    return {
        name: module
        for name, module in list(sys.modules.items())
        if isinstance(module, ModuleType) and (name == PACKAGE or name.startswith(f"{PACKAGE}."))
    }


def _stale() -> list[Stale]:
    modules = _gateway_modules()
    found: list[Stale] = []
    for name, module in modules.items():
        parent_name, _, child = name.rpartition(".")
        parent = modules.get(parent_name)
        if parent is None:
            continue
        # vars(), not getattr(): a PEP 562 __getattr__ (bootstrap.guardrail) must not run.
        bound = vars(parent).get(child)
        if isinstance(bound, ModuleType) and bound is not module:
            found.append(
                (
                    parent,
                    child,
                    f"{parent_name}.{child} is module {bound.__name__} at {id(bound):#x}, "
                    f"but sys.modules[{name!r}] is {module.__name__} at {id(module):#x}",
                )
            )
    for parent_name, parent in modules.items():
        if not hasattr(parent, "__path__"):
            continue
        for child, bound in list(vars(parent).items()):
            name = f"{parent_name}.{child}"
            if isinstance(bound, ModuleType) and bound.__name__ == name and name not in sys.modules:
                found.append(
                    (
                        parent,
                        child,
                        f"{name} is module {bound.__name__} at {id(bound):#x}, "
                        f"but sys.modules has no {name!r}",
                    )
                )
    return found


def stale_package_attributes() -> list[str]:
    """One line per ``corp_llm_gateway`` package attribute that is a submodule other than
    the one ``sys.modules`` holds under its name, or one ``sys.modules`` no longer holds."""
    return sorted(message for _, _, message in _stale())


def restore_package_attributes() -> None:
    """Rebind each stale attribute to the module ``sys.modules`` holds, or drop it."""
    for parent, child, _ in _stale():
        name = f"{parent.__name__}.{child}"
        module = sys.modules.get(name)
        if isinstance(module, ModuleType):
            setattr(parent, child, module)
        else:
            delattr(parent, child)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, None, None]:
    """Fail a test that leaves a stale package attribute (and put it back). Registered by
    ``tests/conftest.py``; it runs after every fixture's teardown, ``monkeypatch``'s undo
    included, so it sees what the next test will."""
    try:
        result = yield
    except BaseException as exc:
        if stale := stale_package_attributes():
            restore_package_attributes()
            exc.add_note(f"test also left stale package attributes: {stale}")
        raise
    if stale := stale_package_attributes():
        restore_package_attributes()
        pytest.fail(f"test left package attributes on stale modules: {stale}", pytrace=False)
    return result
