"""The conftest guard that fails a test leaving state on a ``corp_llm_gateway`` logger."""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from tests import logger_state

PROBE = "corp_llm_gateway._logger_state_probe"


def _add_handler(logger: logging.Logger) -> None:
    logger.addHandler(logging.NullHandler())


def _set_level(logger: logging.Logger) -> None:
    logger.setLevel(logging.INFO)


def _stop_propagating(logger: logging.Logger) -> None:
    logger.propagate = False


def _disable(logger: logging.Logger) -> None:
    logger.disabled = True


@pytest.mark.parametrize("name", ["corp_llm_gateway", PROBE])
@pytest.mark.parametrize("change", [_add_handler, _set_level, _stop_propagating, _disable])
def test_a_change_left_on_a_gateway_logger_is_found_and_put_back(
    name: str, change: Callable[[logging.Logger], None]
) -> None:
    before = logger_state.snapshot()
    change(logging.getLogger(name))

    assert logger_state.changes(before, logger_state.snapshot()) == [name]
    logger_state.restore(before)
    assert logger_state.changes(before, logger_state.snapshot()) == []


def test_a_logger_created_in_its_default_state_is_not_a_change() -> None:
    before = logger_state.snapshot()
    logging.getLogger(f"{PROBE}.created_here")

    assert logger_state.changes(before, logger_state.snapshot()) == []


def test_another_packages_logger_is_not_watched() -> None:
    before = logger_state.snapshot()
    other = logging.getLogger("not_corp_llm_gateway")
    other.setLevel(logging.INFO)
    try:
        assert logger_state.changes(before, logger_state.snapshot()) == []
    finally:
        other.setLevel(logging.NOTSET)
