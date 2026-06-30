"""Local detection pass: concurrent detector union with de-overlap."""

from __future__ import annotations

import asyncio

from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.detectors.regex_checksum import _deduplicate


class LocalDetectionPass:
    def __init__(self, detectors: list[PIIDetector]) -> None:
        self._detectors = detectors

    async def findings(self, text: str) -> list[Finding]:
        if not self._detectors:
            return []
        results = await asyncio.gather(*(d.detect(text) for d in self._detectors))
        raw: list[Finding] = []
        for r in results:
            raw.extend(r)
        return _deduplicate(raw)
