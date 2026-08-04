"""In-tree detector registry: select detector algorithms BY NAME.

Generalizes ``auth/factory.py`` keyed dispatch to a list of names. Factories are
lazy (no detector is constructed at import) and take a config mapping so a
future config-driven detector needs no signature change. An unknown name raises
``ValueError`` listing the known set (safe-extension-registry rule 2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from corp_llm_gateway import config
from corp_llm_gateway.corp_ner import CorpNerClient
from corp_llm_gateway.detectors.corp_ner import CorpNerDetector
from corp_llm_gateway.detectors.dual_ner import DualNerDetector
from corp_llm_gateway.detectors.ner_en import EnNerDetector
from corp_llm_gateway.detectors.ner_ru import RuNerDetector
from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from typing import Any

    from corp_llm_gateway.detectors.base import PIIDetector

    DetectorFactory = Callable[[Mapping[str, Any]], PIIDetector]


def _make_corp_ner(cfg: Mapping[str, Any]) -> PIIDetector:
    """Corp NER for a profile bundle that names it.

    Endpoint from the bundle cfg, else the config chain. A missing endpoint is a
    refusal, not a detector pointed nowhere: that would fail every request as an
    outage instead of naming the misconfiguration.

    The composition root (``bootstrap.build_corp_ner``) is the path that also
    wires the live metrics exporter; a profile-declared instance keeps the
    detector's own Noop default.
    """
    endpoint = str(cfg.get("corp_ner_endpoint") or config.get("CORP_NER_ENDPOINT") or "")
    if not endpoint:
        raise ValueError(
            "detector 'corp_ner' needs an endpoint: set CORP_NER_ENDPOINT or "
            "corp_ner_endpoint in the profile bundle config"
        )
    return CorpNerDetector(CorpNerClient(endpoint))


# Eager dict literal is safe: values are factory callables, not detectors —
# nothing is instantiated and no config is read at import.
DETECTOR_REGISTRY: dict[str, DetectorFactory] = {
    "regex_checksum": lambda cfg: RegexChecksumDetector(),
    "dual_ner": lambda cfg: DualNerDetector(),
    "ner_ru": lambda cfg: RuNerDetector(),
    "ner_en": lambda cfg: EnNerDetector(),
    "corp_ner": _make_corp_ner,
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
