"""Parametrized contract test for the PIIDetector ABC.

Every concrete PIIDetector implementation must satisfy:
  1. detect() is awaitable and returns list[Finding].
  2. Every Finding's offsets slice back to its .text in the source string.
  3. No two Findings in the returned list have overlapping spans.

DualNerDetector param is skipped when natasha/spacy are not installed (Python 3.14).
"""

from __future__ import annotations

import pytest

from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.detectors.dual_ner import DualNerDetector
from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector

# ---------------------------------------------------------------------------
# NER availability guard (module-level, evaluated at collection time)
# ---------------------------------------------------------------------------

_ner_available = False
try:
    import natasha as _nat  # noqa: F401
    import spacy as _spa  # noqa: F401

    _ner_available = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Fixture parametrized over all detectors
# ---------------------------------------------------------------------------

_REGEX_PARAM = pytest.param(RegexChecksumDetector, id="regex")
_DUAL_NER_PARAM = pytest.param(
    DualNerDetector,
    id="dual_ner",
    marks=pytest.mark.skipif(not _ner_available, reason="natasha/spacy not available"),
)


@pytest.fixture(params=[_REGEX_PARAM, _DUAL_NER_PARAM])
def detector(request: pytest.FixtureRequest) -> PIIDetector:
    cls = request.param
    return cls()


# ---------------------------------------------------------------------------
# Contract text (rich enough to generate findings from both detectors)
# ---------------------------------------------------------------------------

_TEXT_WITH_ENTITIES = (
    "Contact John Smith (john.smith@corp.internal) "
    "about the server at 192.168.1.42 — "
    "Анна Кузнецова manages the project"
)

_TEXT_CLEAN = "the function returns a sorted list of integers"

# ---------------------------------------------------------------------------
# Contract assertions
# ---------------------------------------------------------------------------


def _no_overlap(findings: list[Finding]) -> bool:
    for i, a in enumerate(findings):
        for b in findings[i + 1 :]:
            if a.start < b.end and b.start < a.end:
                return False
    return True


@pytest.mark.asyncio
async def test_contract_returns_list(detector: PIIDetector) -> None:
    result = await detector.detect(_TEXT_WITH_ENTITIES)
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_contract_findings_are_finding_instances(detector: PIIDetector) -> None:
    result = await detector.detect(_TEXT_WITH_ENTITIES)
    for item in result:
        assert isinstance(item, Finding)


@pytest.mark.asyncio
async def test_contract_offsets_slice_to_text(detector: PIIDetector) -> None:
    text = _TEXT_WITH_ENTITIES
    findings = await detector.detect(text)
    for f in findings:
        sliced = text[f.start : f.end]
        assert sliced == f.text, (
            f"{type(detector).__name__}: offset mismatch for {f.label}: "
            f"text[{f.start}:{f.end}]={sliced!r} != {f.text!r}"
        )


@pytest.mark.asyncio
async def test_contract_no_overlapping_spans(detector: PIIDetector) -> None:
    findings = await detector.detect(_TEXT_WITH_ENTITIES)
    assert _no_overlap(findings), f"{type(detector).__name__}: overlapping spans in {findings}"


@pytest.mark.asyncio
async def test_contract_empty_input_returns_empty(detector: PIIDetector) -> None:
    assert await detector.detect("") == []


@pytest.mark.asyncio
async def test_contract_clean_text_no_overlap(detector: PIIDetector) -> None:
    findings = await detector.detect(_TEXT_CLEAN)
    assert _no_overlap(findings)
    for f in findings:
        assert _TEXT_CLEAN[f.start : f.end] == f.text
