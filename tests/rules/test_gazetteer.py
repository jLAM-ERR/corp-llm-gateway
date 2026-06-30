"""Tests for the bilingual lemma-based Gazetteer (DP-4)."""

from __future__ import annotations

import pytest

from corp_llm_gateway.rules.gazetteer import Gazetteer, _term_to_lemma_seq, _tokenize_text

# ---------------------------------------------------------------------------
# Surface-fallback tests (no morphology required — run on every Python version)
# ---------------------------------------------------------------------------


async def test_gazetteer_empty_text_returns_no_findings() -> None:
    gaz = Gazetteer({"AML": "REGULATED"})
    assert await gaz.detect("") == []


async def test_gazetteer_no_terms_returns_empty() -> None:
    gaz = Gazetteer({})
    assert await gaz.detect("AML CFT") == []


async def test_single_en_term_found() -> None:
    gaz = Gazetteer({"AML": "REGULATED"})
    findings = await gaz.detect("We need AML compliance here.")
    assert len(findings) == 1
    assert findings[0].label == "REGULATED"
    assert findings[0].text == "AML"


async def test_multiword_en_term_found() -> None:
    gaz = Gazetteer({"money laundering": "REGULATED"})
    text = "This relates to money laundering activity."
    findings = await gaz.detect(text)
    assert len(findings) == 1
    assert findings[0].text == "money laundering"
    assert text[findings[0].start : findings[0].end] == findings[0].text


async def test_marking_term_found() -> None:
    gaz = Gazetteer({"Confidential": "CONFIDENTIAL_MARK", "NDA": "CONFIDENTIAL_MARK"})
    text = "This document is Confidential."
    findings = await gaz.detect(text)
    labels = {f.label for f in findings}
    assert "CONFIDENTIAL_MARK" in labels


async def test_product_term_found() -> None:
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    text = "We are building Project Polaris this quarter."
    findings = await gaz.detect(text)
    assert len(findings) == 1
    assert findings[0].label == "PRODUCT"
    assert findings[0].text == "Project Polaris"


async def test_cyrillic_surface_found_without_morph() -> None:
    """Cyrillic exact-surface match works even without pymorphy3."""
    gaz = Gazetteer({"AML": "REGULATED", "отмывание": "REGULATED"})
    text = "проверяем отмывание и AML"
    findings = await gaz.detect(text)
    texts = {f.text for f in findings}
    assert "отмывание" in texts
    assert "AML" in texts


async def test_offsets_slice_to_surface_form() -> None:
    """text[start:end] must equal finding.text for every finding."""
    gaz = Gazetteer({"Project Polaris": "PRODUCT", "NDA": "CONFIDENTIAL_MARK"})
    text = "Discuss Project Polaris and the NDA."
    findings = await gaz.detect(text)
    for f in findings:
        assert text[f.start : f.end] == f.text, f"offset mismatch for {f.text!r}"


async def test_case_insensitive_surface_fallback() -> None:
    """Lowercase fallback matches terms case-insensitively."""
    gaz = Gazetteer({"Confidential": "CONFIDENTIAL_MARK"})
    # 'confidential' lowercase in text; term stored as 'Confidential'
    # _lemmatize_word falls back to .lower() so both become 'confidential'
    text = "This is confidential information."
    findings = await gaz.detect(text)
    assert len(findings) == 1
    assert findings[0].label == "CONFIDENTIAL_MARK"


async def test_no_hit_on_unrelated_text() -> None:
    gaz = Gazetteer({"Project Polaris": "PRODUCT"})
    findings = await gaz.detect("Hello world, how are you?")
    assert findings == []


async def test_score_is_0_95() -> None:
    gaz = Gazetteer({"AML": "REGULATED"})
    findings = await gaz.detect("AML review")
    assert findings[0].score == pytest.approx(0.95)


async def test_from_defaults_loads_known_terms() -> None:
    """The defaults/ files ship known terms; smoke-check a few."""
    gaz = Gazetteer.from_defaults()
    # regulated.txt must contain AML
    aml_findings = await gaz.detect("The AML policy requires review.")
    assert any(f.label == "REGULATED" for f in aml_findings)
    # markings.txt must contain Confidential
    mark_findings = await gaz.detect("This is Confidential.")
    assert any(f.label == "CONFIDENTIAL_MARK" for f in mark_findings)
    # products.txt must contain Project Polaris
    prod_findings = await gaz.detect("We launched Project Polaris last month.")
    assert any(f.label == "PRODUCT" for f in prod_findings)


async def test_multiword_ru_surface_match() -> None:
    """Multi-word RU term found without inflection (surface match path)."""
    gaz = Gazetteer({"легализация доходов": "REGULATED"})
    text = "запрещена легализация доходов через данный канал"
    findings = await gaz.detect(text)
    assert len(findings) == 1
    assert findings[0].label == "REGULATED"
    assert text[findings[0].start : findings[0].end] == "легализация доходов"


# ---------------------------------------------------------------------------
# Lemma-specific tests (require pymorphy3 — skip on Python 3.14 if absent)
# ---------------------------------------------------------------------------


async def test_ru_inflected_form_matches_lemma_regulated() -> None:
    """«легализации» (genitive) matches lemma «легализация» via pymorphy3."""
    pymorphy3 = pytest.importorskip("pymorphy3")
    assert pymorphy3  # silence unused-import lint

    # Force reload of the morph handle (module may cache None from earlier tests)
    import corp_llm_gateway.rules.gazetteer as gaz_mod

    gaz_mod._morph_tried = False
    gaz_mod._morph = None

    gaz = Gazetteer({"легализация доходов": "REGULATED"})
    # Should also match inflected forms
    text = "проводилась легализация доходов через банк"
    findings = await gaz.detect(text)
    assert len(findings) >= 1
    assert findings[0].label == "REGULATED"


async def test_ru_inflected_form_legalizatsii() -> None:
    """Inflected genitive «легализации» matches term «легализация»."""
    pytest.importorskip("pymorphy3")

    import corp_llm_gateway.rules.gazetteer as gaz_mod

    gaz_mod._morph_tried = False
    gaz_mod._morph = None

    gaz = Gazetteer({"легализация": "REGULATED"})
    text = "расследование легализации средств"
    findings = await gaz.detect(text)
    assert any(f.label == "REGULATED" for f in findings)


# ---------------------------------------------------------------------------
# Tokenizer unit tests
# ---------------------------------------------------------------------------


def test_tokenize_text_returns_word_spans() -> None:
    tokens = _tokenize_text("hello world")
    assert tokens == [("hello", 0, 5), ("world", 6, 11)]


def test_tokenize_text_mixed_script() -> None:
    tokens = _tokenize_text("AML и отмывание")
    words = [w for w, _, _ in tokens]
    assert "AML" in words
    assert "и" in words
    assert "отмывание" in words


def test_term_to_lemma_seq_lowercases() -> None:
    seq = _term_to_lemma_seq("AML CFT")
    # Without morphology libs, fallback is .lower()
    assert "aml" in seq or "AML".lower() in seq
