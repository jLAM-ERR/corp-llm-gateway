"""Tests for RuNerDetector (Natasha/Slovnet).

Skipped automatically on Python 3.14 (no natasha wheels); authoritative run is
on Python 3.12 via .venv-bench.
"""

from __future__ import annotations

import pytest

pytest.importorskip("natasha")

from corp_llm_gateway.detectors.ner_ru import RuNerDetector

pytestmark = pytest.mark.asyncio


@pytest.fixture
def det() -> RuNerDetector:
    return RuNerDetector()


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
# PERSON (PER)
# ---------------------------------------------------------------------------


async def test_ru_person_detected(det: RuNerDetector) -> None:
    text = "Директор Иван Иванов подписал приказ"
    findings = await det.detect(text)
    persons = by_label(findings, "PERSON")
    assert len(persons) >= 1
    assert_offsets(text, findings)


async def test_ru_person_fullname_offsets(det: RuNerDetector) -> None:
    text = "Анна Кузнецова встретилась с коллегами"  # noqa: RUF001
    findings = await det.detect(text)
    persons = by_label(findings, "PERSON")
    assert len(persons) >= 1
    # Each finding's span must slice back to its surface form
    assert_offsets(text, findings)
    person = persons[0]
    assert "Кузнецова" in person.text or "Анна" in person.text


async def test_ru_person_score(det: RuNerDetector) -> None:
    text = "Директор Герман Греф выступил с речью"  # noqa: RUF001
    findings = await det.detect(text)
    persons = by_label(findings, "PERSON")
    assert len(persons) >= 1
    assert all(f.score == 0.8 for f in persons)


# ---------------------------------------------------------------------------
# ORG
# ---------------------------------------------------------------------------


async def test_ru_org_detected(det: RuNerDetector) -> None:
    text = "Сотрудники Газпрома провели совещание"
    findings = await det.detect(text)
    orgs = by_label(findings, "ORG")
    assert len(orgs) >= 1
    assert_offsets(text, findings)


async def test_ru_org_sberbank(det: RuNerDetector) -> None:
    text = "Директор Сбербанка объявил о новой стратегии"  # noqa: RUF001
    findings = await det.detect(text)
    orgs = by_label(findings, "ORG")
    assert len(orgs) >= 1
    assert any("Сбербанк" in f.text for f in orgs)


# ---------------------------------------------------------------------------
# LOCATION (LOC)
# ---------------------------------------------------------------------------


async def test_ru_location_detected(det: RuNerDetector) -> None:
    text = "Конференция прошла в Москве"
    findings = await det.detect(text)
    locs = by_label(findings, "LOCATION")
    assert len(locs) >= 1
    assert_offsets(text, findings)


async def test_ru_location_city(det: RuNerDetector) -> None:
    text = "Иван Иванов работает в компании Google в Санкт-Петербурге"
    findings = await det.detect(text)
    locs = by_label(findings, "LOCATION")
    assert len(locs) >= 1
    assert any("Петербург" in f.text for f in locs)


# ---------------------------------------------------------------------------
# Multiple entity types in one text
# ---------------------------------------------------------------------------


async def test_ru_mixed_entities(det: RuNerDetector) -> None:
    text = "Директор Сбербанка Герман Греф встретился с президентом в Москве"  # noqa: RUF001
    findings = await det.detect(text)
    labels = {f.label for f in findings}
    assert "PERSON" in labels
    assert "ORG" in labels
    assert "LOCATION" in labels
    assert_offsets(text, findings)


async def test_ru_all_offsets_valid(det: RuNerDetector) -> None:
    text = "Иван Иванов работает в компании Google в Санкт-Петербурге"
    findings = await det.detect(text)
    assert_offsets(text, findings)
    assert len(findings) >= 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_ru_empty_string(det: RuNerDetector) -> None:
    assert await det.detect("") == []


async def test_ru_whitespace_only(det: RuNerDetector) -> None:
    assert await det.detect("   \t\n  ") == []


async def test_ru_no_entities(det: RuNerDetector) -> None:
    # Generic prose with no named entities
    text = "Все данные обрабатываются в соответствии с законодательством"  # noqa: RUF001
    findings = await det.detect(text)
    # No strong assertion — model is probabilistic — but offsets must be valid
    assert_offsets(text, findings)


async def test_ru_single_word(det: RuNerDetector) -> None:
    # Single word — should not crash
    findings = await det.detect("Привет")
    assert isinstance(findings, list)
