"""What a test must leave on the ``corp_llm_gateway`` loggers as it found it: a level,
handler, ``propagate`` or ``disabled`` left behind changes what a later test's ``caplog``
captures, so that test passes or fails by run order."""

from __future__ import annotations

import logging

PACKAGE = "corp_llm_gateway"

State = dict[str, tuple[int, bool, bool, tuple[logging.Handler, ...]]]

_DEFAULT = (logging.NOTSET, True, False, ())


def _gateway_loggers() -> dict[str, logging.Logger]:
    return {
        name: logger
        for name, logger in list(logging.Logger.manager.loggerDict.items())
        if isinstance(logger, logging.Logger)
        and (name == PACKAGE or name.startswith(f"{PACKAGE}."))
    }


def snapshot() -> State:
    return {
        name: (logger.level, logger.propagate, logger.disabled, tuple(logger.handlers))
        for name, logger in _gateway_loggers().items()
    }


def changes(before: State, after: State) -> list[str]:
    """Loggers whose state differs; one created since ``before`` counts only if it is no
    longer in its default state."""
    return sorted(
        name
        for name in before.keys() | after.keys()
        if before.get(name, _DEFAULT) != after.get(name, _DEFAULT)
    )


def restore(before: State) -> None:
    for name, logger in _gateway_loggers().items():
        level, propagate, disabled, handlers = before.get(name, _DEFAULT)
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled
        logger.handlers = list(handlers)
