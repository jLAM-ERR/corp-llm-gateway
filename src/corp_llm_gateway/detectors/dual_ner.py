"""Dual-NER detector: RU (Natasha/Slovnet) + EN (spaCy), run-both-union.

Graceful degradation (dev / Python-3.14): if one engine's deps are absent at
runtime (RuntimeError from detect()), that engine is permanently disabled for
the lifetime of the instance and the other still runs; both absent → empty list.

Fail-closed (prod, F2): when ``CORP_LLM_REQUIRE_NER`` is set, a self-disabled
engine instead raises ``NerUnavailableError`` — never a silent ``[]`` that would
let a PERSON/ORG only NER catches egress unredacted (invariant 6, no fail-open).

De-overlap uses the same priority order as RegexChecksumDetector: highest score,
then longest span, then earliest start.
"""

from __future__ import annotations

import logging

from corp_llm_gateway import config
from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.detectors.regex_checksum import _deduplicate

logger = logging.getLogger(__name__)


class NerUnavailableError(Exception):
    """A configured NER engine self-disabled while ``CORP_LLM_REQUIRE_NER`` is set.

    Deliberately NOT a ``RuntimeError``: DualNerDetector's per-engine handler
    catches ``RuntimeError``, and the orchestrator path must let this propagate
    UNCAUGHT up to the litellm hook (→ 503 ``E_NER_UNAVAILABLE``). Nothing on the
    egress path may swallow it into a fail-open.
    """


class DualNerDetector(PIIDetector):
    """Run RU NER + EN NER in parallel, union spans, de-overlap by highest-score-then-longest.

    Sub-detector objects are constructed eagerly (cheap); NER models load lazily on
    first detect() call via module-level caches in ner_ru / ner_en.

    ``require_ner`` (resolved from ``CORP_LLM_REQUIRE_NER`` when None) flips a
    self-disabled engine from fail-open to fail-closed. ``engines`` is a test seam.
    """

    def __init__(
        self,
        *,
        require_ner: bool | None = None,
        engines: list[PIIDetector] | None = None,
    ) -> None:
        if engines is None:
            from corp_llm_gateway.detectors.ner_en import EnNerDetector
            from corp_llm_gateway.detectors.ner_ru import RuNerDetector

            engines = [RuNerDetector(), EnNerDetector()]
        self._engines: list[PIIDetector] = engines
        # Indices of engines that raised RuntimeError on first use (missing deps).
        self._disabled: set[int] = set()
        self._require_ner = config.require_ner() if require_ner is None else require_ner

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
        if self._require_ner and self._disabled:
            # Fail-closed (M4/F2): a required NER engine is missing. Never return a
            # partial/empty result — a PERSON/ORG only NER catches would egress.
            raise NerUnavailableError(
                "NER required but unavailable: "
                + ", ".join(type(self._engines[i]).__name__ for i in sorted(self._disabled))
            )
        return _deduplicate(raw)
