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


def package_version(name: str) -> str:
    """Installed version of ``name``, or ``"unknown"``. Never raises."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except (PackageNotFoundError, ValueError):
        return "unknown"


@runtime_checkable
class PolicySignatureDetector(Protocol):
    """Optional extension for detectors whose redaction coverage is not implied
    by their class.

    Cache A applies a stored mapping WITHOUT running detectors, so its key folds
    a fingerprint of the effective policy — and that fingerprint identifies a
    detector by import path + qualname alone. Two instances of the SAME class can
    still redact differently: NER engines whose models are absent (no ``ner``
    extra) find nothing, and construction args can narrow coverage. A pod with
    the weaker instance would seed an entry that the stronger pod replays
    unredacted for the whole TTL.

    Contract:

    * Return plain strings, sorted-stable across processes — no builtin
      ``hash()`` (PYTHONHASHSEED-salted), ``id()``, object ``repr()``, or
      set/dict iteration order. Include engine and model versions where they
      are available.
    * The value must be IMMUTABLE for the process lifetime: it is read once at
      orchestrator construction and the key it produces outlives the request.
      Resolve (and latch) anything lazy here instead of reporting a state that
      later changes.
    * Corp/environment identity only, never user content (M1-14).

    A detector that raises here disables Cache A for the whole orchestrator
    (fail closed) rather than falling back to class identity.
    """

    def policy_signature(self) -> tuple[str, ...]: ...


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
