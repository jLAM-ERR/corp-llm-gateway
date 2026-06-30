"""Tests for DualNerDetector (RU + EN, run-both-union).

Skipped automatically on Python 3.14 (no natasha/spaCy wheels); authoritative
run is on Python 3.12 via .venv-bench.
"""

from __future__ import annotations

import pytest

pytest.importorskip("natasha")
pytest.importorskip("spacy")

from corp_llm_gateway.detectors.base import Finding
from corp_llm_gateway.detectors.dual_ner import DualNerDetector

pytestmark = pytest.mark.asyncio

# Mixed RU/EN input — from ADR-003 / plan example
_MIXED_TEXT = "// owner: Анна Кузнецова — see AcmeService for John Smith"


@pytest.fixture
def det() -> DualNerDetector:
    return DualNerDetector()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def by_label(findings: list[Finding], label: str) -> list[Finding]:
    return [f for f in findings if f.label == label]


def assert_offsets(text: str, findings: list[Finding]) -> None:
    for f in findings:
        assert text[f.start : f.end] == f.text, (
            f"offset mismatch for {f.label}: text[{f.start}:{f.end}]="
            f"{text[f.start : f.end]!r} != {f.text!r}"
        )


def _has_overlap(findings: list[Finding]) -> bool:
    for i, a in enumerate(findings):
        for b in findings[i + 1 :]:
            if a.start < b.end and b.start < a.end:
                return True
    return False


# ---------------------------------------------------------------------------
# Mixed RU+EN single input — the core union test
# ---------------------------------------------------------------------------


async def test_dual_finds_ru_person(det: DualNerDetector) -> None:
    findings = await det.detect(_MIXED_TEXT)
    persons = by_label(findings, "PERSON")
    # Natasha finds Анна Кузнецова (and spaCy also picks it up)
    assert any("Кузнецова" in f.text or "Анна" in f.text for f in persons), (
        f"RU person not found in {findings}"
    )


async def test_dual_finds_en_person(det: DualNerDetector) -> None:
    findings = await det.detect(_MIXED_TEXT)
    persons = by_label(findings, "PERSON")
    assert any("John" in f.text or "Smith" in f.text for f in persons), (
        f"EN person not found in {findings}"
    )


async def test_dual_no_overlapping_spans(det: DualNerDetector) -> None:
    findings = await det.detect(_MIXED_TEXT)
    assert not _has_overlap(findings), f"overlapping spans in {findings}"


async def test_dual_offsets_valid(det: DualNerDetector) -> None:
    findings = await det.detect(_MIXED_TEXT)
    assert_offsets(_MIXED_TEXT, findings)


async def test_dual_ru_and_en_union(det: DualNerDetector) -> None:
    """Union must cover at least RU + EN persons; no span overlap."""
    findings = await det.detect(_MIXED_TEXT)
    persons = by_label(findings, "PERSON")
    # Expect at least 2 persons total from the union (Анна Кузнецова + John Smith)
    assert len(persons) >= 2, f"expected >=2 persons in union, got {persons}"
    assert not _has_overlap(findings)
    assert_offsets(_MIXED_TEXT, findings)


# ---------------------------------------------------------------------------
# Pure RU text — only Natasha contributes
# ---------------------------------------------------------------------------


async def test_dual_pure_ru_text(det: DualNerDetector) -> None:
    text = "Директор Сбербанка Герман Греф выступил в Москве"
    findings = await det.detect(text)
    labels = {f.label for f in findings}
    assert "PERSON" in labels
    assert not _has_overlap(findings)
    assert_offsets(text, findings)


# ---------------------------------------------------------------------------
# Pure EN text — only spaCy contributes (or both if spaCy picks up EN)
# ---------------------------------------------------------------------------


async def test_dual_pure_en_text(det: DualNerDetector) -> None:
    text = "John Smith is the CEO of Acme Corporation in New York"
    findings = await det.detect(text)
    persons = by_label(findings, "PERSON")
    assert len(persons) >= 1
    assert not _has_overlap(findings)
    assert_offsets(text, findings)


# ---------------------------------------------------------------------------
# Dedup: same span from both engines → only one in output
# ---------------------------------------------------------------------------


async def test_dual_no_duplicate_spans(det: DualNerDetector) -> None:
    # Both Natasha and spaCy detect "Анна Кузнецова" in this text
    text = "Анна Кузнецова руководит проектом"
    findings = await det.detect(text)
    # After dedup, the same span must appear at most once
    seen: set[tuple[int, int, str]] = set()
    for f in findings:
        key = (f.start, f.end, f.label)
        assert key not in seen, f"duplicate span {key} in {findings}"
        seen.add(key)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_dual_empty_string(det: DualNerDetector) -> None:
    assert await det.detect("") == []


async def test_dual_whitespace_only(det: DualNerDetector) -> None:
    assert await det.detect("   ") == []


async def test_dual_no_entities(det: DualNerDetector) -> None:
    text = "the function returns an integer sorted in ascending order"
    findings = await det.detect(text)
    assert isinstance(findings, list)
    assert not _has_overlap(findings)
    assert_offsets(text, findings)
