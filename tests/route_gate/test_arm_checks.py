"""The arm predicates ``asgi.py`` exits 70 on (``route_gate/arm_checks.py``).

End to end through the real lifespan: ``tests/test_asgi_entrypoint.py``. Through
litellm's dispatch: ``tests/litellm_hook`` (``_dispatch_fixtures.arm_problems``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from corp_llm_gateway.route_gate import arm_checks


class _Ours:
    scan_raw_request = False
    run_in_parallel = False


def _is_ours(cb: Any) -> bool:
    return isinstance(cb, _Ours)


def test_every_problem_has_a_log_reason() -> None:
    problems = {
        arm_checks.GUARDRAIL_ABSENT,
        arm_checks.APPLY_GUARDRAIL,
        arm_checks.SCAN_RAW_REQUEST,
        arm_checks.RUN_IN_PARALLEL,
        *arm_checks.DEBUG_PROBLEMS,
    }
    assert set(arm_checks.REASONS) == problems


@pytest.mark.parametrize("callbacks", [[], [object()], ["corp_llm_gateway.bootstrap.guardrail"]])
def test_no_guardrail_of_ours_is_absent(callbacks: list[Any]) -> None:
    assert arm_checks.guardrail_problems(callbacks, is_ours=_is_ours) == ["guardrail_absent"]


def test_a_plain_guardrail_among_others_arms() -> None:
    assert arm_checks.guardrail_problems([object(), _Ours()], is_ours=_is_ours) == []


@pytest.mark.parametrize("flag", ["scan_raw_request", "run_in_parallel"])
def test_an_unsafe_flag_is_refused(flag: str) -> None:
    ours = _Ours()
    setattr(ours, flag, True)

    assert arm_checks.guardrail_problems([ours], is_ours=_is_ours) == [flag]


def test_apply_guardrail_anywhere_in_the_mro_is_refused() -> None:
    class _WithApply(_Ours):
        async def apply_guardrail(self, *args: Any) -> Any:
            return None

    class _Inherits(_WithApply):
        pass

    assert arm_checks.guardrail_problems([_Inherits()], is_ours=_is_ours) == ["apply_guardrail"]


def test_the_real_guardrail_class_is_recognised_by_type() -> None:
    from tests.test_litellm_hook import _build_guardrail

    guardrail, _ = _build_guardrail([])

    assert arm_checks.guardrail_problems([guardrail]) == []
    assert arm_checks.guardrail_problems([_Ours()]) == ["guardrail_absent"]


@pytest.fixture
def logger() -> Iterator[logging.Logger]:
    probe = logging.getLogger("corp_llm_gateway.tests.arm_checks_probe")
    yield probe
    probe.setLevel(logging.NOTSET)


def test_a_logger_below_debug_arms(logger: logging.Logger) -> None:
    logger.setLevel(logging.INFO)

    assert arm_checks.debug_problems(loggers=[logger]) == []


def test_a_logger_at_debug_is_refused(logger: logging.Logger) -> None:
    logger.setLevel(logging.DEBUG)

    assert arm_checks.debug_problems(loggers=[logger]) == ["litellm_debug_logging"]


def test_set_verbose_is_refused() -> None:
    assert arm_checks.debug_problems(set_verbose=True) == ["litellm_set_verbose"]


def test_no_loggers_and_no_verbose_arms() -> None:
    assert arm_checks.debug_problems() == []
