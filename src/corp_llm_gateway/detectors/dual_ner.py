"""Dual-NER detector: RU (Natasha/Slovnet) + EN (spaCy), run-both-union.

Graceful degradation: if one engine's deps are absent at runtime (RuntimeError from
detect()), that engine is permanently disabled for the lifetime of the instance and
the other still runs. Both absent → empty list, no exception raised.
De-overlap uses the same priority order as RegexChecksumDetector: highest score,
then longest span, then earliest start.
"""

from __future__ import annotations

import logging

from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.detectors.regex_checksum import _deduplicate

logger = logging.getLogger(__name__)


class DualNerDetector(PIIDetector):
    """Run RU NER + EN NER in parallel, union spans, de-overlap by highest-score-then-longest.

    Sub-detector objects are constructed eagerly (cheap); NER models load lazily on
    first detect() call via module-level caches in ner_ru / ner_en.
    """

    def __init__(self) -> None:
        from corp_llm_gateway.detectors.ner_en import EnNerDetector
        from corp_llm_gateway.detectors.ner_ru import RuNerDetector

        self._engines: list[PIIDetector] = [RuNerDetector(), EnNerDetector()]
        # Indices of engines that raised RuntimeError on first use (missing deps).
        self._disabled: set[int] = set()

    async def detect(self, text: str) -> list[Finding]:
        raw: list[Finding] = []
        for idx, engine in enumerate(self._engines):
            if idx in self._disabled:
                continue
            try:
                raw.extend(await engine.detect(text))
            except RuntimeError as exc:
                self._disabled.add(idx)
                logger.warning(
                    "dual_ner_engine_disabled engine=%s reason=%s",
                    type(engine).__name__,
                    exc,
                )
        return _deduplicate(raw)
