import logging

import pytest

from corp_llm_gateway.detectors import (
    Finding,
    OpenaiPrivacyFilterDetector,
    PIIDetector,
    PresidioDetector,
    ShadowDetector,
)


class _StaticDetector(PIIDetector):
    def __init__(self, findings: list[Finding]) -> None:
        self._findings = findings

    async def detect(self, text: str) -> list[Finding]:
        return list(self._findings)


class _RaisingDetector(PIIDetector):
    async def detect(self, text: str) -> list[Finding]:
        raise RuntimeError("boom secret-content-here")


@pytest.mark.asyncio
async def test_shadow_returns_canonical_results() -> None:
    canonical = _StaticDetector([Finding("alice", "PERSON", 0, 5, 0.9)])
    shadow = _StaticDetector([Finding("alice", "PERSON", 0, 5, 0.5)])
    detector = ShadowDetector(canonical, shadow)
    result = await detector.detect("alice")
    assert result == [Finding("alice", "PERSON", 0, 5, 0.9)]


@pytest.mark.asyncio
async def test_shadow_exception_does_not_break_canonical(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canonical = _StaticDetector([Finding("alice", "PERSON", 0, 5, 0.9)])
    shadow = _RaisingDetector()
    detector = ShadowDetector(canonical, shadow)
    with caplog.at_level(logging.WARNING):
        result = await detector.detect("alice my secret is XYZ")
    assert result == [Finding("alice", "PERSON", 0, 5, 0.9)]
    assert "shadow_detector_failed" in caplog.text
    assert "secret-content-here" not in caplog.text


@pytest.mark.asyncio
async def test_shadow_agreement_does_not_log(caplog: pytest.LogCaptureFixture) -> None:
    canonical = _StaticDetector([Finding("alice", "PERSON", 0, 5, 0.9)])
    shadow = _StaticDetector([Finding("alice", "PERSON", 0, 5, 0.5)])
    detector = ShadowDetector(canonical, shadow)
    with caplog.at_level(logging.INFO):
        await detector.detect("alice")
    assert "detector_disagreement" not in caplog.text


@pytest.mark.asyncio
async def test_shadow_disagreement_is_logged_but_originals_are_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canonical = _StaticDetector([Finding("alice@corp.lan", "EMAIL", 0, 16, 0.9)])
    shadow = _StaticDetector(
        [Finding("alice@corp.lan", "EMAIL", 0, 16, 0.9), Finding("bob", "PERSON", 20, 23, 0.5)]
    )
    detector = ShadowDetector(canonical, shadow)
    with caplog.at_level(logging.INFO):
        await detector.detect("alice@corp.lan and bob")
    assert "detector_disagreement" in caplog.text
    assert "EMAIL" in caplog.text
    assert "alice@corp.lan" not in caplog.text
    assert "bob" not in caplog.text


@pytest.mark.asyncio
async def test_openai_filter_stub_raises() -> None:
    with pytest.raises(NotImplementedError):
        await OpenaiPrivacyFilterDetector().detect("anything")


@pytest.mark.asyncio
async def test_presidio_stub_raises() -> None:
    with pytest.raises(NotImplementedError):
        await PresidioDetector().detect("anything")
