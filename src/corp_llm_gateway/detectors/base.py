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
    ``0 <= start <= end <= len(texts[i])`` offsets relative to *that* text and
    ``texts[i][start:end] == finding.text``. Callers reject the whole batch on a
    violation (fail closed) — an offset/text disagreement would rebase to a span
    covering a different original and corrupt the placeholder bijection (M1-9).
    """

    async def detect_batch(self, texts: list[str]) -> list[list[Finding]]: ...
