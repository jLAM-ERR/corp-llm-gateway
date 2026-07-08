"""PIIDetector contract over the whole DETECTOR_REGISTRY (D8).

Mirrors the storage contract-suite pattern (tests/storage/test_mapping_store.py):
one fixture parametrized over every registered detector name, each built through
``build_detectors`` (the production path), asserting the ``PIIDetector`` ABC
contract — ``detect`` is a coroutine, returns the declared ``list[Finding]``
(an iterable of findings, empty allowed), and handles empty input.

NER-backed detectors (dual_ner / ner_ru / ner_en) skip when natasha/spaCy are
absent so the suite is green on the 3.14 gate venv.
"""

from __future__ import annotations

import inspect

import pytest

from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.profiles import DETECTOR_REGISTRY, build_detectors

_ner_available = False
try:
    import natasha as _nat  # noqa: F401
    import spacy as _spa  # noqa: F401

    _ner_available = True
except ImportError:
    pass

_NER_DETECTORS = {"dual_ner", "ner_ru", "ner_en"}

_TEXT = "write to ivan@example.com about ИНН 7707083893 and John Smith"


def _param(name: str):
    marks = (
        [pytest.mark.skipif(not _ner_available, reason="natasha/spaCy not installed")]
        if name in _NER_DETECTORS
        else []
    )
    return pytest.param(name, id=name, marks=marks)


@pytest.fixture(params=[_param(name) for name in sorted(DETECTOR_REGISTRY)])
def detector(request: pytest.FixtureRequest) -> PIIDetector:
    (built,) = build_detectors([request.param])
    return built


def test_registry_covers_every_known_detector() -> None:
    assert set(DETECTOR_REGISTRY) == {"regex_checksum", "dual_ner", "ner_ru", "ner_en"}


def test_built_detector_is_pii_detector(detector: PIIDetector) -> None:
    assert isinstance(detector, PIIDetector)


def test_detect_is_a_coroutine_function(detector: PIIDetector) -> None:
    assert inspect.iscoroutinefunction(detector.detect)


@pytest.mark.asyncio
async def test_detect_returns_declared_list_type(detector: PIIDetector) -> None:
    result = await detector.detect(_TEXT)
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_detect_returns_iterable_of_findings(detector: PIIDetector) -> None:
    for item in await detector.detect(_TEXT):
        assert isinstance(item, Finding)


@pytest.mark.asyncio
async def test_detect_empty_input_returns_empty_list(detector: PIIDetector) -> None:
    assert await detector.detect("") == []
