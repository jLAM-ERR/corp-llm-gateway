"""The two collection-time hooks of tests/conftest.py: the ``slow`` marker from
``tests/slow_tests.txt`` and the opt-in ``--shuffle-seed`` order."""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from _pytest.fixtures import reorder_items

SLOW_TESTS_FILE = Path(__file__).parent / "slow_tests.txt"


def slow_entries(path: Path = SLOW_TESTS_FILE) -> set[str]:
    entries = set()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
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
