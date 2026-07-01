"""Local detection pass: segment-aware concurrent detector union with de-overlap.

asyncio.gather fans out detectors per segment. NER detectors (ner_ru / ner_en)
offload their CPU-bound inference via asyncio.to_thread so the event loop stays
free during model calls; a per-model threading.Lock serialises concurrent callers
(spaCy Language and Natasha tagger are not thread-safe for concurrent calls).
Regex stays inline — it is microseconds and purely GIL-free stdlib.
"""

from __future__ import annotations

import asyncio

from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.detectors.regex_checksum import _deduplicate
from corp_llm_gateway.sanitizer.segmenter import SegmentKind, split_segments


class LocalDetectionPass:
    def __init__(
        self,
        detectors: list[PIIDetector],
        *,
        code_safe_detectors: list[PIIDetector] | None = None,
    ) -> None:
        self._detectors = detectors
        # Detectors to run on raw CODE segments (e.g. regex, gazetteer — not NER).
        # Defaults to all detectors when not specified (backward-compatible).
        self._code_detectors = code_safe_detectors if code_safe_detectors is not None else detectors

    async def findings(self, text: str) -> list[Finding]:
        if not self._detectors:
            return []

        segments = split_segments(text)
        if not segments:
            return []

        all_raw: list[Finding] = []
        for seg in segments:
            chosen = self._code_detectors if seg.kind == SegmentKind.CODE else self._detectors
            if not chosen:
                continue
            results = await asyncio.gather(*(d.detect(seg.text) for d in chosen))
            for r in results:
                for f in r:
                    # Offset sub-segment findings back to absolute positions in text.
                    all_raw.append(
                        Finding(
                            text=f.text,
                            label=f.label,
                            start=seg.start + f.start,
                            end=seg.start + f.end,
                            score=f.score,
                        )
                    )

        return _deduplicate(all_raw)
