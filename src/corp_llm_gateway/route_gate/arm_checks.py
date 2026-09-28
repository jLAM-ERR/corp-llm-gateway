"""What ``asgi.py``'s lifespan refuses to arm with (exit 70), checked after litellm's
startup: our guardrail absent or set up so litellm bypasses it, or litellm's DEBUG output
on (it logs the original request before any pre-call hook). ``REASONS`` says why for each."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

GUARDRAIL_ABSENT = "guardrail_absent"
APPLY_GUARDRAIL = "apply_guardrail"
SCAN_RAW_REQUEST = "scan_raw_request"
RUN_IN_PARALLEL = "run_in_parallel"
LITELLM_DEBUG_LOGGING = "litellm_debug_logging"
LITELLM_SET_VERBOSE = "litellm_set_verbose"

DEBUG_PROBLEMS = frozenset({LITELLM_DEBUG_LOGGING, LITELLM_SET_VERBOSE})

_ALLOW_DEBUG_HINT = "CORP_LLM_ALLOW_LITELLM_DEBUG=1 allows it outside prod, for tests only"

REASONS: dict[str, str] = {
    GUARDRAIL_ABSENT: (
        "no CorpLlmGuardrail in litellm.callbacks after startup; the litellm config did "
        "not register corp_llm_gateway.bootstrap.guardrail"
    ),
    APPLY_GUARDRAIL: (
        "CorpLlmGuardrail defines apply_guardrail; litellm would dispatch it through "
        "unified_guardrail and never run its pre-call hook"
    ),
    SCAN_RAW_REQUEST: (
        "CorpLlmGuardrail has scan_raw_request set; litellm would scan a snapshot and "
        "send the unsanitized request"
    ),
    RUN_IN_PARALLEL: (
        "CorpLlmGuardrail has run_in_parallel set; litellm would discard its rewrite and "
        "run it after every other pre-call hook"
    ),
    LITELLM_DEBUG_LOGGING: (
        "litellm's loggers are at DEBUG (LITELLM_LOG=DEBUG, DETAILED_DEBUG or --detailed_debug); "
        "litellm logs the original request before any pre-call hook runs. " + _ALLOW_DEBUG_HINT
    ),
    LITELLM_SET_VERBOSE: (
        "litellm.set_verbose is on (litellm_settings.set_verbose); litellm prints requests "
        "before any pre-call hook runs. " + _ALLOW_DEBUG_HINT
    ),
}


def _is_corp_guardrail(callback: Any) -> bool:
    from corp_llm_gateway.litellm_hook import CorpLlmGuardrail

    return isinstance(callback, CorpLlmGuardrail)


def _base_apply_guardrail() -> Any:
    try:
        from litellm.integrations.custom_guardrail import CustomGuardrail
    except ImportError:  # pragma: no cover - arm only runs with litellm installed
        return None
    return CustomGuardrail.apply_guardrail


def guardrail_problems(
    callbacks: Iterable[Any], *, is_ours: Callable[[Any], bool] | None = None
) -> list[str]:
    """Problems with our guardrail as registered: absent, or set up to be bypassed."""
    ours_test = is_ours if is_ours is not None else _is_corp_guardrail
    ours = [cb for cb in callbacks if ours_test(cb)]
    if not ours:
        return [GUARDRAIL_ABSENT]
    problems: list[str] = []
    base = _base_apply_guardrail()
    for cb in ours:
        if getattr(cb, "scan_raw_request", False):
            problems.append(SCAN_RAW_REQUEST)
        if getattr(cb, "run_in_parallel", False):
            problems.append(RUN_IN_PARALLEL)
        if getattr(type(cb), "apply_guardrail", None) not in (None, base):
            problems.append(APPLY_GUARDRAIL)
    return problems


def debug_problems(
    *, loggers: Iterable[logging.Logger] = (), set_verbose: bool = False
) -> list[str]:
    """litellm DEBUG output that would print a request before it is sanitized."""
    problems: list[str] = []
    if any(logger.isEnabledFor(logging.DEBUG) for logger in loggers):
        problems.append(LITELLM_DEBUG_LOGGING)
    if set_verbose:
        problems.append(LITELLM_SET_VERBOSE)
    return problems


def live_problems() -> list[str]:
    """Every problem with the running process: ``litellm.callbacks``, litellm's
    three verbose loggers and both ``set_verbose`` flags."""
    import litellm
    from litellm import _logging as litellm_logging

    loggers = (
        litellm_logging.verbose_logger,
        litellm_logging.verbose_proxy_logger,
        litellm_logging.verbose_router_logger,
    )
    verbose = bool(getattr(litellm, "set_verbose", False)) or bool(
        getattr(litellm_logging, "set_verbose", False)
    )
    return guardrail_problems(litellm.callbacks or ()) + debug_problems(
        loggers=loggers, set_verbose=verbose
    )
