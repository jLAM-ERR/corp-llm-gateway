"""Local detection pass: segment-aware concurrent detector union with de-overlap.

asyncio.gather fans out detectors per segment. NER detectors (ner_ru / ner_en)
offload their CPU-bound inference via asyncio.to_thread so the event loop stays
free during model calls; a per-model threading.Lock serialises concurrent callers
(spaCy Language and Natasha tagger are not thread-safe for concurrent calls).
Regex stays inline — it is microseconds and purely GIL-free stdlib.

Detectors implementing BatchPIIDetector (network-backed ones, where one
round-trip per segment would serialise against the service timeout) are lifted
out of that per-segment loop: they get a single detect_batch call carrying every
segment they are eligible for, and their findings are offset-corrected into the
same _deduplicate as the rest.
"""

from __future__ import annotations

import asyncio

from corp_llm_gateway.detectors.base import BatchPIIDetector, Finding, PIIDetector
from corp_llm_gateway.detectors.dual_ner import NerUnavailableError
from corp_llm_gateway.detectors.regex_checksum import _deduplicate
from corp_llm_gateway.sanitizer.segmenter import Segment, SegmentKind, split_segments


class DetectorContractError(NerUnavailableError, ValueError):
    """A batch detector broke the ``BatchPIIDetector`` contract — fail closed.

    Subclasses ``NerUnavailableError`` so the fail-closed handlers already on the
    egress path (``litellm_hook._pre_call_impl`` / ``_sanitize_prompt_field``)
    classify it as a detection failure (503) instead of letting it reach the F8
    catch-all as an opaque 500; the log line carries the exception type, so a
    contract bug stays distinguishable from a missing NER model. Also subclasses
    ``ValueError`` (as ``StaleSpanError`` does) to keep existing
    ``except ValueError`` call sites working.

    Messages carry labels, offsets and lengths only — never text (M1-14).
    """


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

        # (detector, runs on PROSE/COMMENT, runs on CODE) for batch-capable detectors.
        # Membership in the two lists is what restricts e.g. corp NER to non-CODE.
        self._batch_specs: list[tuple[BatchPIIDetector, bool, bool]] = []
        seen: set[int] = set()
        for d in (*self._detectors, *self._code_detectors):
            if id(d) in seen:
                continue
            seen.add(id(d))
            if isinstance(d, BatchPIIDetector):
                self._batch_specs.append(
                    (
                        d,
                        any(d is x for x in self._detectors),
                        any(d is x for x in self._code_detectors),
                    )
                )
        self._batch_ids = {id(d) for d, _, _ in self._batch_specs}

    @property
    def detectors(self) -> tuple[PIIDetector, ...]:
        """Detectors run on PROSE/COMMENT segments (the full configured set)."""
        return tuple(self._detectors)

    @property
    def code_detectors(self) -> tuple[PIIDetector, ...]:
        """Subset run on raw CODE segments."""
        return tuple(self._code_detectors)

    async def findings(self, text: str) -> list[Finding]:
        if not self._detectors:
            return []

        segments = split_segments(text)
        if not segments:
            return []

        all_raw: list[Finding] = []

        if self._batch_specs:
            all_raw.extend(await self._batched_findings(segments))

        for seg in segments:
            chosen = self._code_detectors if seg.kind == SegmentKind.CODE else self._detectors
            chosen = [d for d in chosen if id(d) not in self._batch_ids]
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

    async def _batched_findings(self, segments: list[Segment]) -> list[Finding]:
        plans: list[tuple[BatchPIIDetector, list[Segment]]] = []
        for detector, on_text, on_code in self._batch_specs:
            eligible = [s for s in segments if (on_code if s.kind == SegmentKind.CODE else on_text)]
            if eligible:
                plans.append((detector, eligible))
        if not plans:
            return []

        results = await asyncio.gather(
            *(detector.detect_batch([s.text for s in eligible]) for detector, eligible in plans)
        )

        out: list[Finding] = []
        for (_, eligible), per_text in zip(plans, results, strict=True):
            if len(per_text) != len(eligible):
                # Counts only — never echo user text (M1-14).
                raise DetectorContractError(
                    f"detect_batch returned {len(per_text)} result lists for {len(eligible)} texts"
                )
            for seg, found in zip(eligible, per_text, strict=True):
                for f in found:
                    if not 0 <= f.start <= f.end <= len(seg.text):
                        raise DetectorContractError(
                            "detect_batch returned a finding with an out-of-range offset"
                        )
                    if f.text != seg.text[f.start : f.end]:
                        # Rebasing an inconsistent finding would let the real span
                        # egress unredacted and break the M1-9 bijection.
                        raise DetectorContractError(
                            "detect_batch returned a finding whose text does not match its "
                            f"own span: label={f.label} start={f.start} end={f.end} "
                            f"text_len={len(f.text)} span_len={f.end - f.start}"
                        )
                    out.append(
                        Finding(
                            text=f.text,
                            label=f.label,
                            start=seg.start + f.start,
                            end=seg.start + f.end,
                            score=f.score,
                        )
                    )
        return out
