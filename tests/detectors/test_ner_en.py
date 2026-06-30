"""Tests for EnNerDetector (spaCy en_core_web_md).

Skipped automatically on Python 3.14 (no spaCy wheels); authoritative run is
on Python 3.12 via .venv-bench (model must be installed alongside spaCy).
"""

from __future__ import annotations

import pytest

pytest.importorskip("spacy")

from corp_llm_gateway.detectors.ner_en import EnNerDetector

pytestmark = pytest.mark.asyncio


@pytest.fixture
def det() -> EnNerDetector:
    return EnNerDetector()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def by_label(findings: list, label: str) -> list:
    return [f for f in findings if f.label == label]


def assert_offsets(text: str, findings: list) -> None:
    for f in findings:
        assert text[f.start : f.end] == f.text, (
            f"offset mismatch for {f.label}: text[{f.start}:{f.end}]="
            f"{text[f.start : f.end]!r} != {f.text!r}"
        )


# ---------------------------------------------------------------------------
# PERSON
# ---------------------------------------------------------------------------


async def test_en_person_detected(det: EnNerDetector) -> None:
    text = "John Smith is the administrator of the system"
    findings = await det.detect(text)
    persons = by_label(findings, "PERSON")
    assert len(persons) >= 1
    assert_offsets(text, findings)


async def test_en_person_offsets(det: EnNerDetector) -> None:
    text = "Please contact Jane Doe for further information"
    findings = await det.detect(text)
    persons = by_label(findings, "PERSON")
    assert len(persons) >= 1
    assert_offsets(text, findings)
    assert any("Jane" in f.text or "Doe" in f.text for f in persons)


async def test_en_person_score(det: EnNerDetector) -> None:
    text = "John Smith works at Acme Corporation"
    findings = await det.detect(text)
    persons = by_label(findings, "PERSON")
    assert len(persons) >= 1
    assert all(f.score == 0.8 for f in persons)


# ---------------------------------------------------------------------------
# ORG
# ---------------------------------------------------------------------------


async def test_en_org_detected(det: EnNerDetector) -> None:
    text = "The CEO of Acme Corporation announced the results"
    findings = await det.detect(text)
    orgs = by_label(findings, "ORG")
    assert len(orgs) >= 1
    assert_offsets(text, findings)


async def test_en_org_label(det: EnNerDetector) -> None:
    text = "Google reported record earnings this quarter"
    findings = await det.detect(text)
    orgs = by_label(findings, "ORG")
    assert len(orgs) >= 1
    assert any("Google" in f.text for f in orgs)


# ---------------------------------------------------------------------------
# LOCATION (GPE → LOCATION, LOC → LOCATION)
# ---------------------------------------------------------------------------


async def test_en_gpe_mapped_to_location(det: EnNerDetector) -> None:
    text = "John Smith is based in New York"
    findings = await det.detect(text)
    locs = by_label(findings, "LOCATION")
    assert len(locs) >= 1
    assert_offsets(text, findings)


async def test_en_location_offsets(det: EnNerDetector) -> None:
    text = "The London office handles European operations"
    findings = await det.detect(text)
    assert_offsets(text, findings)


# ---------------------------------------------------------------------------
# Multiple entities in one sentence
# ---------------------------------------------------------------------------


async def test_en_person_org_location(det: EnNerDetector) -> None:
    text = "John Smith is the CEO of Acme Corporation in New York"
    findings = await det.detect(text)
    labels = {f.label for f in findings}
    assert "PERSON" in labels
    assert "ORG" in labels
    assert "LOCATION" in labels
    assert_offsets(text, findings)


async def test_en_all_offsets_valid(det: EnNerDetector) -> None:
    text = "Jane Doe from Microsoft attended the conference in San Francisco"
    findings = await det.detect(text)
    assert_offsets(text, findings)
    assert len(findings) >= 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_en_empty_string(det: EnNerDetector) -> None:
    assert await det.detect("") == []


async def test_en_whitespace_only(det: EnNerDetector) -> None:
    assert await det.detect("   \t\n  ") == []


async def test_en_no_entities(det: EnNerDetector) -> None:
    # Generic code-like prose
    text = "the function returns a list of integers sorted in ascending order"
    findings = await det.detect(text)
    assert isinstance(findings, list)
    assert_offsets(text, findings)


async def test_en_single_word(det: EnNerDetector) -> None:
    findings = await det.detect("Hello")
    assert isinstance(findings, list)
