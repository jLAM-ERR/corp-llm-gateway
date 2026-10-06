"""The two collection-time hooks of tests/conftest.py: the ``slow`` marker from
``tests/slow_tests.txt`` and the opt-in ``--shuffle-seed`` order."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

SLOW_TESTS_FILE = Path(__file__).parent / "slow_tests.txt"


def slow_entries(path: Path = SLOW_TESTS_FILE) -> set[str]:
    """Every line but blank ones and whole-line ``#`` comments; a ``#`` inside an entry
    (a parametrize id) is part of it."""
    entries = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.add(line)
    return entries


def function_id(nodeid: str) -> str:
    """The node id without its parametrize suffix."""
    return nodeid.split("[", 1)[0]


def mark_slow(items: list[pytest.Item], entries: set[str]) -> None:
    for item in items:
        if item.nodeid in entries or function_id(item.nodeid) in entries:
            item.add_marker(pytest.mark.slow)


def shuffled(items: list[pytest.Item], seed: int) -> list[pytest.Item]:
    """Modules in a seeded random order, then the classes in each, then the tests in each;
    pytest's own reorder then groups the cases of a module- or class-scoped parameter, so
    no wider-scoped fixture is set up more often than in the ordered run."""
    # Private pytest API, imported here so an upgrade that moves it breaks only shuffled runs.
    from _pytest.fixtures import reorder_items

    rng = random.Random(seed)
    modules: dict[str, dict[str, list[pytest.Item]]] = {}
    for item in items:
        group = item.parent.nodeid if isinstance(item.parent, pytest.Class) else ""
        modules.setdefault(item.nodeid.split("::", 1)[0], {}).setdefault(group, []).append(item)
    order: list[pytest.Item] = []
    for groups in rng.sample(list(modules.values()), len(modules)):
        for group in rng.sample(list(groups.values()), len(groups)):
            order.extend(rng.sample(group, len(group)))
    return reorder_items(order)
