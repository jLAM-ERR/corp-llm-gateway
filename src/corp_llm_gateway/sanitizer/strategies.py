from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyResult:
    pairs: tuple[tuple[str, str], ...]


class StrategyError(Exception):
    pass


class SanitizerStrategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def extract(self, raw_llm_output: str) -> StrategyResult: ...


class FunctionCallStrategy(SanitizerStrategy):
    @property
    def name(self) -> str:
        return "function_call"

    async def extract(self, raw_llm_output: str) -> StrategyResult:
        raise NotImplementedError(
            "FunctionCallStrategy stub — implement after corp LLM endpoint is wired"
        )


class JsonStrategy(SanitizerStrategy):
    @property
    def name(self) -> str:
        return "json"

    async def extract(self, raw_llm_output: str) -> StrategyResult:
        raise NotImplementedError(
            "JsonStrategy stub — implement after corp LLM endpoint is wired"
        )


class RegexStrategy(SanitizerStrategy):
    @property
    def name(self) -> str:
        return "regex"

    async def extract(self, raw_llm_output: str) -> StrategyResult:
        raise NotImplementedError(
            "RegexStrategy stub — implement after corp LLM endpoint is wired"
        )
