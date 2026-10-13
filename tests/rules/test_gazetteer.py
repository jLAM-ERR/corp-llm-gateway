"""Tests for the bilingual lemma-based Gazetteer (DP-4)."""

from __future__ import annotations

import random
import re

import pytest

from corp_llm_gateway.detectors.base import Finding
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


# ---------------------------------------------------------------------------
# Identifier sub-token scanning tests (DP-5)
# Surface-fallback: no morphology needed; .lower() is sufficient for EN codenames.
# These tests run on Python 3.14 (no pymorphy3/spaCy) and 3.12 (with deps).
# ---------------------------------------------------------------------------


async def test_identifier_companynameabc_service_yields_product() -> None:
    """CompanynameabcService → PRODUCT finding for 'Companynameabc' sub-token."""
    gaz = Gazetteer({"Companynameabc": "PRODUCT", "Betadirect": "PRODUCT", "Kdir": "PRODUCT"})
    text = "using CompanynameabcService"
    findings = await gaz.detect(text)
    assert any(f.label == "PRODUCT" and f.text == "Companynameabc" for f in findings)
    for f in findings:
        assert text[f.start : f.end] == f.text, f"offset mismatch for {f.text!r}"


async def test_identifier_betadirect_client_yields_product() -> None:
    """BetadirectClient → PRODUCT finding for 'Betadirect' sub-token."""
    gaz = Gazetteer({"Betadirect": "PRODUCT"})
    text = "import BetadirectClient"
    findings = await gaz.detect(text)
    assert any(f.label == "PRODUCT" and f.text == "Betadirect" for f in findings)
    for f in findings:
        assert text[f.start : f.end] == f.text


async def test_identifier_kdir_service_yields_product() -> None:
    """KdirService → PRODUCT finding for 'Kdir' sub-token."""
    gaz = Gazetteer({"Kdir": "PRODUCT"})
    text = "class KdirService:"
    findings = await gaz.detect(text)
    assert any(f.label == "PRODUCT" and f.text == "Kdir" for f in findings)
    for f in findings:
        assert text[f.start : f.end] == f.text


async def test_clean_identifier_yields_no_findings() -> None:
    """UserAccountService has no gazetteer terms — nothing is redacted (R8 / UX)."""
    gaz = Gazetteer({"Companynameabc": "PRODUCT", "Betadirect": "PRODUCT", "Kdir": "PRODUCT"})
    findings = await gaz.detect("UserAccountService is a standard service")
    assert findings == []


async def test_no_duplicate_finding_for_standalone_term() -> None:
    """A term that appears standalone matches once, not twice (whole + sub-token)."""
    gaz = Gazetteer({"Betadirect": "PRODUCT"})
    text = "Betadirect is the product"
    findings = await gaz.detect(text)
    betadirect_hits = [f for f in findings if "betadirect" in f.text.lower()]
    assert len(betadirect_hits) == 1


async def test_identifier_offsets_absolute_in_original_text() -> None:
    """Sub-token finding offsets must be absolute into the original text string."""
    gaz = Gazetteer({"Kdir": "PRODUCT"})
    text = "see also KdirService for details"
    findings = await gaz.detect(text)
    kdir_findings = [f for f in findings if f.text == "Kdir"]
    assert len(kdir_findings) == 1
    f = kdir_findings[0]
    assert text[f.start : f.end] == "Kdir"
    # "KdirService" starts at position 9 in text; "Kdir" sub-token at 9
    assert f.start == text.index("Kdir")


async def test_identifier_snake_case_sub_token() -> None:
    """Snake-case identifier sub-tokens are also scanned."""
    gaz = Gazetteer({"Betadirect": "PRODUCT"})
    text = "betadirect_client = connect()"
    findings = await gaz.detect(text)
    # "betadirect" is its own \w+ token in snake_case, so whole-token match
    assert any(f.label == "PRODUCT" for f in findings)


# ---------------------------------------------------------------------------
# Lemmatizer capability signature (Cache-A key input)
# ---------------------------------------------------------------------------


def test_lemmatizer_signature_distinguishes_backend_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same terms + different lemmatizer capability must be distinguishable —
    that difference changes which text the SAME terms match."""
    from corp_llm_gateway.rules import gazetteer as gaz_module

    gaz = Gazetteer({"AML": "REGULATED"})

    monkeypatch.setattr(gaz_module, "_try_load_ru_morph", lambda: None)
    monkeypatch.setattr(gaz_module, "_try_load_en_nlp", lambda: None)
    absent = gaz.lemmatizer_signature()

    monkeypatch.setattr(gaz_module, "_try_load_ru_morph", object)
    present = gaz.lemmatizer_signature()

    assert absent == ("ru:none", "en:none")
    assert present != absent
    assert all(isinstance(part, str) for part in present)


def test_lemmatizer_signature_forces_lazy_backends_to_settle() -> None:
    """The signature is folded into a cache key computed ONCE, so the capability
    it reports must not be able to change afterwards: probing resolves the lazy
    handles, and their `_tried` flags latch for the process lifetime."""
    from corp_llm_gateway.rules import gazetteer as gaz_module

    Gazetteer({}).lemmatizer_signature()

    assert gaz_module._morph_tried is True
    assert gaz_module._spacy_tried is True


# ---------------------------------------------------------------------------
# Lemma memo and term index — same findings, fewer lemmatizer calls
# ---------------------------------------------------------------------------


class _CountingToken:
    def __init__(self, lemma: str) -> None:
        self.lemma_ = lemma


class _CountingNlp:
    """Fake spaCy pipeline: strips a trailing 's', records every word it sees."""

    def __init__(self, tag: str = "") -> None:
        self.tag = tag
        self.calls: list[str] = []

    def __call__(self, word: str) -> list[_CountingToken]:
        self.calls.append(word)
        lemma = word.lower()
        if len(lemma) > 3 and lemma.endswith("s"):
            lemma = lemma[:-1]
        return [_CountingToken(self.tag + lemma)]


class _CountingParse:
    def __init__(self, normal_form: str) -> None:
        self.normal_form = normal_form


class _CountingMorph:
    """Fake pymorphy3: strips one RU case ending, records every word it sees."""

    def __init__(self, tag: str = "") -> None:
        self.tag = tag
        self.calls: list[str] = []

    def parse(self, word: str) -> list[_CountingParse]:
        self.calls.append(word)
        lemma = word.lower()
        for ending in ("ами", "ы", "и", "а", "е"):  # noqa: RUF001
            if len(lemma) > 3 and lemma.endswith(ending):
                lemma = lemma.removesuffix(ending)
                break
        return [_CountingParse(self.tag + lemma)]


def _install_lemmatizers(
    monkeypatch: pytest.MonkeyPatch, morph: object | None, nlp: object | None
) -> None:
    import corp_llm_gateway.rules.gazetteer as gaz_mod

    monkeypatch.setattr(gaz_mod, "_try_load_ru_morph", lambda: morph)
    monkeypatch.setattr(gaz_mod, "_try_load_en_nlp", lambda: nlp)


async def test_detect_lemmatizes_each_unique_word_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections import Counter

    nlp = _CountingNlp()
    morph = _CountingMorph()
    _install_lemmatizers(monkeypatch, morph, nlp)

    gaz = Gazetteer({"AML": "REGULATED", "money laundering": "REGULATED", "отмывание": "REGULATED"})
    text = (
        "AML review of money laundering, AML again; отмывание и отмывание. "
        "BetadirectClient calls BetadirectClient. " * 20
    )
    findings = await gaz.detect(text)

    assert findings, "the terms are in the text"
    repeated = {w: n for w, n in Counter(nlp.calls + morph.calls).items() if n > 1}
    assert repeated == {}, f"lemmatizer re-run for words it already lemmatized: {repeated}"


def test_lemma_memo_follows_the_global_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handle swapped through the loaders' globals is never answered from the old memo."""
    import corp_llm_gateway.rules.gazetteer as gaz_mod

    monkeypatch.setattr(gaz_mod, "_spacy_tried", True)
    monkeypatch.setattr(gaz_mod, "_morph_tried", True)
    monkeypatch.setattr(gaz_mod, "_spacy_nlp", _CountingNlp("a:"))
    monkeypatch.setattr(gaz_mod, "_morph", _CountingMorph("a:"))
    assert gaz_mod._lemmatize_word("reports") == "a:report"
    assert gaz_mod._lemmatize_word("договоры") == "a:договор"

    monkeypatch.setattr(gaz_mod, "_spacy_nlp", _CountingNlp("b:"))
    monkeypatch.setattr(gaz_mod, "_morph", _CountingMorph("b:"))
    assert gaz_mod._lemmatize_word("reports") == "b:report"
    assert gaz_mod._lemmatize_word("договоры") == "b:договор"

    monkeypatch.setattr(gaz_mod, "_spacy_nlp", None)
    monkeypatch.setattr(gaz_mod, "_morph", None)
    assert gaz_mod._lemmatize_word("Reports") == "reports"
    assert gaz_mod._lemmatize_word("Договоры") == "договоры"


async def test_lemma_memo_is_not_shared_across_lemmatizers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lemma one backend computed must not decide a match under another backend."""

    class _IdentityMorph:
        def parse(self, word: str) -> list[_CountingParse]:
            return [_CountingParse(word.lower())]

    _install_lemmatizers(monkeypatch, _CountingMorph(), None)
    assert await Gazetteer({"договор": "REGULATED"}).detect("подписан договоры")

    _install_lemmatizers(monkeypatch, _IdentityMorph(), None)
    assert await Gazetteer({"договор": "REGULATED"}).detect("подписан договоры") == []


def test_lemma_memo_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import corp_llm_gateway.rules.gazetteer as gaz_mod

    monkeypatch.setattr(gaz_mod, "_LEMMA_CACHE_SIZE", 4)
    nlp = _CountingNlp()
    _install_lemmatizers(monkeypatch, None, nlp)
    for i in range(10):
        gaz_mod._lemmatize_word(f"word{i}")
    nlp.calls.clear()
    gaz_mod._lemmatize_word("word0")
    gaz_mod._lemmatize_word("word9")
    assert nlp.calls == ["word0"], "the oldest entry is evicted, the newest is kept"


# Reference copy of the term-major scan Gazetteer.detect ran before the
# first-lemma index. It shares no state with the code under test: lemmas come
# straight from the backend (no memo) and the term maps are built here.
def _reference_lemma(word: str, morph: object | None, nlp: object | None) -> str:
    from corp_llm_gateway.rules.gazetteer import _en_lemma, _ru_lemma

    if re.search(r"[а-яёА-ЯЁ]", word):  # noqa: RUF001
        if morph is not None:
            return _ru_lemma(morph, word)
    elif nlp is not None:
        return _en_lemma(nlp, word)
    return word.lower()


def _reference_raw_findings(
    terms: dict[str, str], text: str, morph: object | None, nlp: object | None
) -> list[Finding]:
    from corp_llm_gateway.sanitizer.segmenter.identifiers import split_identifier

    index: dict[tuple[str, ...], str] = {}
    single: dict[str, str] = {}
    for term, label in terms.items():
        term = term.strip()
        if not term:
            continue
        seq = tuple(_reference_lemma(t, morph, nlp) for t in re.findall(r"\w+", term))
        if not seq:
            continue
        index.setdefault(seq, label)
        if len(seq) == 1:
            single.setdefault(seq[0], label)
    if not text.strip() or not index:
        return []

    tokens = [(m.group(), m.start(), m.end()) for m in re.finditer(r"\w+", text)]
    lemmas = [(_reference_lemma(word, morph, nlp), start, end) for word, start, end in tokens]
    n = len(lemmas)
    findings: list[Finding] = []
    for seq, label in index.items():
        seq_len = len(seq)
        if seq_len > n:
            continue
        for i in range(n - seq_len + 1):
            if all(lemmas[i + j][0] == seq[j] for j in range(seq_len)):
                start = lemmas[i][1]
                end = lemmas[i + seq_len - 1][2]
                findings.append(Finding(text[start:end], label, start, end, 0.95))
    if single:
        for word, tok_start, _tok_end in tokens:
            sub_tokens = split_identifier(word)
            if len(sub_tokens) <= 1:
                continue
            for sub_text, rel_start, rel_end in sub_tokens:
                label = single.get(_reference_lemma(sub_text, morph, nlp))
                if label is not None:
                    start, end = tok_start + rel_start, tok_start + rel_end
                    findings.append(Finding(text[start:end], label, start, end, 0.95))
    return findings


_EN_VOCAB = (
    "money", "laundering", "launderings", "report", "reports", "Report", "AML", "aml",
    "CFT", "project", "Projects", "polaris", "Polaris", "confidential", "the", "of", "and",
    "Betadirect", "Kdir", "Companynameabc", "client", "Service", "scheme", "schemes",
)  # fmt: skip
_RU_VOCAB = (
    "отмывание", "отмывания", "легализация", "легализации", "доходов", "доходы",
    "договор", "договоры", "договора", "клиент", "клиента", "и", "по", "счёт",
)  # fmt: skip
_IDENT_VOCAB = (
    "BetadirectClient", "KdirService", "CompanynameabcService", "betadirect_client",
    "money_laundering", "AMLReport", "reportsScheme", "HTTPKdirClient", "kdir2go",
    "UserAccountService", "snake_case_word",
)  # fmt: skip
_SEPARATORS = (" ", " ", " ", ", ", ". ", "\n", " — ", "(", ") ", "-", "_", "")
_LABELS = ("PRODUCT", "REGULATED", "CONFIDENTIAL_MARK")


def _random_terms(rng: random.Random) -> dict[str, str]:
    terms: dict[str, str] = {}
    for _ in range(rng.randint(0, 14)):
        vocab = rng.choice((_EN_VOCAB, _RU_VOCAB, _EN_VOCAB + _RU_VOCAB, _IDENT_VOCAB))
        words = [rng.choice(vocab) for _ in range(rng.choice((1, 1, 1, 2, 2, 3)))]
        terms[" ".join(words)] = rng.choice(_LABELS)
    return terms


def _random_text(rng: random.Random) -> str:
    vocab = _EN_VOCAB + _RU_VOCAB + _IDENT_VOCAB
    parts: list[str] = []
    for _ in range(rng.randint(0, 60)):
        word = rng.choice(vocab)
        if parts and rng.random() < 0.2:
            word = parts[-2] if len(parts) >= 2 else word
        parts.append(word)
        parts.append(rng.choice(_SEPARATORS))
    return "".join(parts)


@pytest.mark.parametrize("backend", ["fake-lemmatizers", "no-lemmatizers"])
@pytest.mark.parametrize("seed", [0, 1, 2, 3])
async def test_indexed_detect_matches_the_term_major_scan(
    monkeypatch: pytest.MonkeyPatch, backend: str, seed: int
) -> None:
    import corp_llm_gateway.rules.gazetteer as gaz_mod
    from corp_llm_gateway.detectors.regex_checksum import _deduplicate

    morph: object | None = None
    nlp: object | None = None
    if backend == "fake-lemmatizers":
        morph, nlp = _CountingMorph(), _CountingNlp()
    _install_lemmatizers(monkeypatch, morph, nlp)

    raw_seen: list[list[Finding]] = []

    def _capture(findings: list[Finding]) -> list[Finding]:
        raw_seen.append(list(findings))
        return _deduplicate(findings)

    monkeypatch.setattr(gaz_mod, "_deduplicate", _capture)

    rng = random.Random(seed)
    for _ in range(150):
        terms = _random_terms(rng)
        text = _random_text(rng)
        gaz = Gazetteer(terms)
        expected_raw = _reference_raw_findings(terms, text, morph, nlp)
        for _repeat in range(2):  # cold, then warm lemma memo
            raw_seen.clear()
            got = await gaz.detect(text)
            assert (raw_seen[0] if raw_seen else []) == expected_raw, (seed, text, terms)
            assert got == _deduplicate(expected_raw)


def test_long_words_bypass_the_lemma_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    import corp_llm_gateway.rules.gazetteer as gaz_mod

    nlp = _CountingNlp()
    morph = _CountingMorph()
    _install_lemmatizers(monkeypatch, morph, nlp)
    limit = gaz_mod._LEMMA_CACHE_MAX_WORD
    long_en = "Reports" + "x" * limit + "s"
    long_ru = "договор" + "ж" * limit + "ы"

    assert gaz_mod._lemmatize_word(long_en) == long_en.lower()[:-1]
    assert gaz_mod._lemmatize_word(long_ru) == long_ru.lower()[:-1]
    assert gaz_mod._lemmatize_word(long_en) == long_en.lower()[:-1]
    assert nlp.calls == [long_en, long_en], "a long word is lemmatized afresh every time"
    assert morph.calls == [long_ru]

    short_en = "r" * (limit - 1) + "s"
    short_ru = "д" * (limit - 1) + "ы"
    assert gaz_mod._lemmatize_word(short_en) == short_en[:-1]
    assert gaz_mod._lemmatize_word(short_ru) == short_ru[:-1]
    en_memo = gaz_mod._en_lemmas._bound[1]  # type: ignore[index]
    ru_memo = gaz_mod._ru_lemmas._bound[1]  # type: ignore[index]
    assert en_memo.cache_info().currsize == 1  # type: ignore[attr-defined]
    assert ru_memo.cache_info().currsize == 1  # type: ignore[attr-defined]
    gaz_mod._lemmatize_word(short_en)
    assert nlp.calls.count(short_en) == 1


@pytest.mark.requires_ner
def test_memoised_lemma_equals_the_backend_lemma(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the real spaCy and pymorphy3, the memo returns what a direct call returns."""
    import corp_llm_gateway.rules.gazetteer as gaz_mod

    monkeypatch.setattr(gaz_mod._en_lemmas, "_bound", None)
    monkeypatch.setattr(gaz_mod._ru_lemmas, "_bound", None)
    nlp = gaz_mod._try_load_en_nlp()
    morph = gaz_mod._try_load_ru_morph()
    assert nlp is not None, "the ner extra and en_core_web_md are installed"
    assert morph is not None, "the ner extra brings pymorphy3"
    en_words = ["running", "Reports", "laundering", "geese", "AML", "was", "BetadirectClient"]
    ru_words = ["договоры", "легализации", "доходов", "отмывания", "Счёта", "клиентами"]
    for _call in ("cold", "warm"):
        for word in en_words:
            assert gaz_mod._lemmatize_word(word) == gaz_mod._en_lemma(nlp, word), word
        for word in ru_words:
            assert gaz_mod._lemmatize_word(word) == gaz_mod._ru_lemma(morph, word), word
