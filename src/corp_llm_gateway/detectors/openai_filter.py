from corp_llm_gateway.detectors.base import Finding, PIIDetector


class OpenaiPrivacyFilterDetector(PIIDetector):
    async def detect(self, text: str) -> list[Finding]:
        raise NotImplementedError(
            "OpenaiPrivacyFilterDetector stub — implement after M0-10 GPU pod is provisioned"
        )
