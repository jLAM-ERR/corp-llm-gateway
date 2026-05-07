from corp_llm_gateway.detectors.base import Finding, PIIDetector


class PresidioDetector(PIIDetector):
    async def detect(self, text: str) -> list[Finding]:
        raise NotImplementedError(
            "PresidioDetector stub — implement after Presidio engine deps are added"
        )
