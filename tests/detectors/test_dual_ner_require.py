"""F2/A2: NER unavailability must fail CLOSED in production, not silently open.

These tests inject fake engines, so they run on every venv (no natasha/spaCy
needed) — the real 3.14 no-NER path is exactly "both engines raise RuntimeError".
"""

from __future__ import annotations

import pytest

from corp_llm_gateway.detectors import DualNerDetector, NerUnavailableError
from corp_llm_gateway.detectors.base import Finding, PIIDetector

# asyncio_mode = "auto" (pyproject) collects the async tests; the one sync test
# below must stay unmarked, so no module-level asyncio pytestmark here.

_PERSON = "John Smith met Анна Кузнецова in Moscow"


class _RaisingEngine(PIIDetector):
    """Models an engine whose deps/model are absent at runtime."""

    async def detect(self, text: str) -> list[Finding]:
        raise RuntimeError("ner deps absent")


class _WorkingEngine(PIIDetector):
    def __init__(self, findings: list[Finding]) -> None:
        self._findings = findings

    async def detect(self, text: str) -> list[Finding]:
        return list(self._findings)


def _finding() -> Finding:
    return Finding(text="John Smith", label="PERSON", start=0, end=10, score=0.8)


# --- Repro: the silent fail-OPEN when require-ner is off -------------------


async def test_require_ner_off_both_absent_returns_empty_fail_open() -> None:
    """CONFIRMED F2: both NER engines absent + require-ner OFF → [] with NO
    exception. A PERSON only NER would catch is not redacted (the leak)."""
    det = DualNerDetector(require_ner=False, engines=[_RaisingEngine(), _RaisingEngine()])
    assert await det.detect(_PERSON) == []


# --- The fix: require-ner ON fails closed ----------------------------------


async def test_require_ner_on_both_absent_raises() -> None:
    det = DualNerDetector(require_ner=True, engines=[_RaisingEngine(), _RaisingEngine()])
    with pytest.raises(NerUnavailableError):
        await det.detect(_PERSON)


async def test_require_ner_on_one_absent_still_raises() -> None:
    """Strict fail-closed: any configured engine missing → raise (a partial
    result would let entities the missing engine covers egress)."""
    det = DualNerDetector(
        require_ner=True,
        engines=[_WorkingEngine([_finding()]), _RaisingEngine()],
    )
    with pytest.raises(NerUnavailableError):
        await det.detect(_PERSON)


async def test_require_ner_on_all_present_returns_findings() -> None:
    det = DualNerDetector(
        require_ner=True,
        engines=[_WorkingEngine([_finding()]), _WorkingEngine([])],
    )
    findings = await det.detect(_PERSON)
    assert [f.text for f in findings] == ["John Smith"]


async def test_require_ner_on_stays_closed_across_calls() -> None:
    det = DualNerDetector(require_ner=True, engines=[_RaisingEngine(), _RaisingEngine()])
    with pytest.raises(NerUnavailableError):
        await det.detect(_PERSON)
    # Engine stays disabled; the second call must also fail closed, not return [].
    with pytest.raises(NerUnavailableError):
        await det.detect(_PERSON)


def test_ner_unavailable_is_not_a_runtimeerror() -> None:
    """It must NOT subclass RuntimeError: the per-engine handler catches
    RuntimeError, and the orchestrator path must let this propagate uncaught
    up to the litellm hook (never re-caught, never fail-open)."""
    assert not issubclass(NerUnavailableError, RuntimeError)
    assert issubclass(NerUnavailableError, Exception)


async def test_require_ner_defaults_to_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the param is omitted the flag resolves via the config loader."""
    from corp_llm_gateway import config

    monkeypatch.setenv("CORP_LLM_REQUIRE_NER", "1")
    config.reset_cache()
    det = DualNerDetector(engines=[_RaisingEngine(), _RaisingEngine()])
    with pytest.raises(NerUnavailableError):
        await det.detect(_PERSON)
