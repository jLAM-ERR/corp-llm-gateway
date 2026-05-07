from abc import ABC, abstractmethod
from dataclasses import dataclass


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
