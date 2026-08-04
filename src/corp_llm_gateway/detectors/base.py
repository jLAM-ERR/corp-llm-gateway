from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Finding:
    text: str
    label: str
    start: int
    end: int
    score: float


class PIIDetector(ABC):
    @abstractmethod
    async def detect(self, text: str) -> list[Finding]: ...


@runtime_checkable
class BatchPIIDetector(Protocol):
    """Optional extension for detectors that score many texts in one round-trip.

    Contract: return exactly one list per input text, in input order, with
    ``0 <= start <= end <= len(texts[i])`` offsets relative to *that* text.
    """

    async def detect_batch(self, texts: list[str]) -> list[list[Finding]]: ...
