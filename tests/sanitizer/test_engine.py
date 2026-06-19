import pytest

from corp_llm_gateway.sanitizer import (
    CorpLlmSanitizer,
    SanitizerStrategy,
    StrategyResult,
)
from corp_llm_gateway.sanitizer.engine import AllStrategiesFailedError
from corp_llm_gateway.sanitizer.placeholder import sort_placeholders_by_descending_length
from corp_llm_gateway.sanitizer.strategies import StrategyError


class _StaticStrategy(SanitizerStrategy):
    def __init__(self, n: str, result: StrategyResult) -> None:
        self._n = n
        self._result = result

    @property
    def name(self) -> str:
        return self._n

    async def extract(self, raw_llm_output: str) -> StrategyResult:
        return self._result


class _FailingStrategy(SanitizerStrategy):
    def __init__(self, n: str) -> None:
        self._n = n

    @property
    def name(self) -> str:
        return self._n

    async def extract(self, raw_llm_output: str) -> StrategyResult:
        raise StrategyError(f"{self._n} cannot parse")


class _NotImplementedStrategy(SanitizerStrategy):
    @property
    def name(self) -> str:
        return "not_impl"

    async def extract(self, raw_llm_output: str) -> StrategyResult:
        raise NotImplementedError("stub")


# placeholder ordering ------------------------------------------------------


def test_placeholder_sort_descending_by_length() -> None:
    placeholders = ["[A]", "[ABC]", "[AB]"]
    assert sort_placeholders_by_descending_length(placeholders) == ["[ABC]", "[AB]", "[A]"]


def test_placeholder_sort_stable_on_ties() -> None:
    assert sort_placeholders_by_descending_length(["b", "a"]) == ["a", "b"]
    assert sort_placeholders_by_descending_length(["aa", "bb"]) == ["aa", "bb"]


def test_placeholder_sort_empty() -> None:
    assert sort_placeholders_by_descending_length([]) == []


# engine strategy ordering ---------------------------------------------------


def test_engine_requires_strategy() -> None:
    with pytest.raises(ValueError, match="at least one"):
        CorpLlmSanitizer(strategies=[])


async def test_engine_returns_first_success() -> None:
    expected = StrategyResult(pairs=(("alice", "[NAME_001]"),))
    sanitizer = CorpLlmSanitizer(
        strategies=[
            _StaticStrategy("a", expected),
            _StaticStrategy("b", StrategyResult(pairs=())),
        ]
    )
    assert await sanitizer.extract("raw") == expected


async def test_engine_skips_not_implemented_strategies() -> None:
    expected = StrategyResult(pairs=(("alice", "[NAME_001]"),))
    sanitizer = CorpLlmSanitizer(
        strategies=[
            _NotImplementedStrategy(),
            _StaticStrategy("b", expected),
        ]
    )
    assert await sanitizer.extract("raw") == expected


async def test_engine_falls_through_on_strategy_error() -> None:
    expected = StrategyResult(pairs=(("alice", "[NAME_001]"),))
    sanitizer = CorpLlmSanitizer(
        strategies=[
            _FailingStrategy("a"),
            _StaticStrategy("b", expected),
        ]
    )
    assert await sanitizer.extract("raw") == expected


async def test_engine_raises_when_all_strategies_fail() -> None:
    sanitizer = CorpLlmSanitizer(strategies=[_FailingStrategy("a"), _FailingStrategy("b")])
    with pytest.raises(AllStrategiesFailedError):
        await sanitizer.extract("raw")


async def test_engine_raises_when_only_stubs() -> None:
    sanitizer = CorpLlmSanitizer(strategies=[_NotImplementedStrategy()])
    with pytest.raises(AllStrategiesFailedError):
        await sanitizer.extract("raw")


# apply / reverse with length ordering --------------------------------------


def test_apply_uses_length_descending_order() -> None:
    sanitizer = CorpLlmSanitizer(strategies=[_NotImplementedStrategy()])
    mapping = StrategyResult(pairs=(("alice cooper", "[NAME_002]"), ("alice", "[NAME_001]")))
    assert sanitizer.apply("hello alice cooper", mapping) == "hello [NAME_002]"


def test_reverse_uses_length_descending_order() -> None:
    sanitizer = CorpLlmSanitizer(strategies=[_NotImplementedStrategy()])
    mapping = StrategyResult(pairs=(("alice", "[NAME_1]"), ("bob", "[NAME_12]")))
    assert sanitizer.reverse("[NAME_12] and [NAME_1]", mapping) == "bob and alice"
