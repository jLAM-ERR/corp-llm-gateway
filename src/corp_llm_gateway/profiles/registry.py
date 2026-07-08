"""In-tree detector registry: select detector algorithms BY NAME.

Generalizes ``auth/factory.py`` keyed dispatch to a list of names. Factories are
lazy (no detector is constructed at import) and take a config mapping so a
future config-driven detector needs no signature change. An unknown name raises
``ValueError`` listing the known set (safe-extension-registry rule 2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from corp_llm_gateway.detectors.dual_ner import DualNerDetector
from corp_llm_gateway.detectors.ner_en import EnNerDetector
from corp_llm_gateway.detectors.ner_ru import RuNerDetector
from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from typing import Any

    from corp_llm_gateway.detectors.base import PIIDetector

    DetectorFactory = Callable[[Mapping[str, Any]], PIIDetector]


# Eager dict literal is safe: values are factory callables, not detectors —
# nothing is instantiated and no config is read at import.
DETECTOR_REGISTRY: dict[str, DetectorFactory] = {
    "regex_checksum": lambda cfg: RegexChecksumDetector(),
    "dual_ner": lambda cfg: DualNerDetector(),
    "ner_ru": lambda cfg: RuNerDetector(),
    "ner_en": lambda cfg: EnNerDetector(),
}


def build_detectors(
    names: Sequence[str], cfg: Mapping[str, Any] | None = None
) -> tuple[PIIDetector, ...]:
    """Map detector names to instances (order-preserving, deduplicated)."""
    cfg_map: Mapping[str, Any] = cfg if cfg is not None else {}
    known = tuple(sorted(DETECTOR_REGISTRY))
    out: list[PIIDetector] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        factory = DETECTOR_REGISTRY.get(name)
        if factory is None:
            raise ValueError(f"unknown detector {name!r}; expected one of {known}")
        seen.add(name)
        out.append(factory(cfg_map))
    return tuple(out)
