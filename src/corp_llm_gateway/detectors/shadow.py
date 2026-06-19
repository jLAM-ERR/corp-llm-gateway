import logging

from corp_llm_gateway.detectors.base import Finding, PIIDetector

logger = logging.getLogger(__name__)


class ShadowDetector(PIIDetector):
    """Runs canonical + shadow detectors. Returns canonical's results.

    Logs structural disagreements (counts, label sets) but NEVER the
    matched text — the M1-14 invariant forbids emitting originals to
    any logging callback.
    """

    def __init__(self, canonical: PIIDetector, shadow: PIIDetector) -> None:
        self._canonical = canonical
        self._shadow = shadow

    async def detect(self, text: str) -> list[Finding]:
        canonical_findings = await self._canonical.detect(text)
        try:
            shadow_findings = await self._shadow.detect(text)
        except Exception as exc:
            logger.warning("shadow_detector_failed exception=%s", type(exc).__name__)
            return canonical_findings

        if not _equivalent(canonical_findings, shadow_findings):
            logger.info(
                "detector_disagreement canonical_count=%d shadow_count=%d "
                "canonical_labels=%s shadow_labels=%s",
                len(canonical_findings),
                len(shadow_findings),
                sorted({f.label for f in canonical_findings}),
                sorted({f.label for f in shadow_findings}),
            )
        return canonical_findings


def _equivalent(a: list[Finding], b: list[Finding]) -> bool:
    return {(f.label, f.start, f.end) for f in a} == {(f.label, f.start, f.end) for f in b}
