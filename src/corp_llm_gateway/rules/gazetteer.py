"""Lemma-based bilingual gazetteer for semantic PII categories.

Covers: products (R8), ПОД/ФТ↔AML/CFT regulated terms (R9), and
confidentiality markings. Implements PIIDetector so findings flow through
_merge_local with [LABEL_NNN] placeholders, preserving the M1-9 bijection.

Lemmatization uses pymorphy3 (RU) + spaCy (EN) when installed (the 'ner'
extra). Falls back to case-insensitive surface/token matching when absent
so the module stays importable on Python 3.14 without those deps.
"""

from __future__ import annotations

import re
from pathlib import Path

from corp_llm_gateway.detectors.base import Finding, PIIDetector

# ---------------------------------------------------------------------------
# Lazy morphology handles (module-level singletons, loaded once)
# ---------------------------------------------------------------------------

_morph: object | None = None
_morph_tried: bool = False
_spacy_nlp: object | None = None
_spacy_tried: bool = False

_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")  # noqa: RUF001


def _is_cyrillic(word: str) -> bool:
    return bool(_CYRILLIC_RE.search(word))


def _try_load_ru_morph() -> object | None:
    """Try to load pymorphy3 MorphAnalyzer; return None if unavailable."""
    global _morph, _morph_tried
    if _morph_tried:
        return _morph
    _morph_tried = True
    try:
        import pymorphy3  # type: ignore[import-untyped]

        _morph = pymorphy3.MorphAnalyzer()
    except Exception:
        _morph = None
    return _morph


def _try_load_en_nlp() -> object | None:
    """Try to load spaCy en_core_web_md (lemmatize only); None if absent."""
    global _spacy_nlp, _spacy_tried
    if _spacy_tried:
        return _spacy_nlp
    _spacy_tried = True
    try:
        import spacy  # type: ignore[import-untyped]

        _spacy_nlp = spacy.load("en_core_web_md", disable=["ner", "parser"])
    except Exception:
        _spacy_nlp = None
    return _spacy_nlp


def _lemmatize_word(word: str) -> str:
    """Return the lowercase lemma for a single word token; falls back to lower()."""
    if _is_cyrillic(word):
        morph = _try_load_ru_morph()
        if morph is not None:
            return morph.parse(word)[0].normal_form  # type: ignore[union-attr]
    else:
        nlp = _try_load_en_nlp()
        if nlp is not None:
            doc = nlp(word)  # type: ignore[operator]
            if doc:
                return doc[0].lemma_.lower()
    return word.lower()


def _term_to_lemma_seq(term: str) -> tuple[str, ...]:
    """Split a (possibly multi-word) term into a tuple of lowercase lemmas."""
    tokens = re.findall(r"\w+", term)
    return tuple(_lemmatize_word(t) for t in tokens)


def _tokenize_text(text: str) -> list[tuple[str, int, int]]:
    """Return [(word, char_start, char_end), ...] for every word token in text."""
    return [(m.group(), m.start(), m.end()) for m in re.finditer(r"\w+", text)]


# ---------------------------------------------------------------------------
# Default data directory
# ---------------------------------------------------------------------------

_DEFAULTS_DIR = Path(__file__).parent / "defaults"

_CATEGORY_FILES: dict[str, str] = {
    "products.txt": "PRODUCT",
    "regulated.txt": "REGULATED",
    "markings.txt": "CONFIDENTIAL_MARK",
}


# ---------------------------------------------------------------------------
# Gazetteer detector
# ---------------------------------------------------------------------------


class Gazetteer(PIIDetector):
    """Lemma-based bilingual term gazetteer; emits [LABEL_NNN] findings.

    Each matched surface span yields a Finding with score 0.95. The label
    is one of PRODUCT / REGULATED / CONFIDENTIAL_MARK. Offsets satisfy
    ``text[finding.start : finding.end] == finding.text``.
    """

    def __init__(self, term_categories: dict[str, str]) -> None:
        """Build from {term_text: label}.

        The constructor lemmatizes all terms eagerly so detect() stays fast.
        """
        # Maps lemma-token-tuple → label (first registration wins on collision)
        self._index: dict[tuple[str, ...], str] = {}
        for term, label in term_categories.items():
            term = term.strip()
            if not term:
                continue
            seq = _term_to_lemma_seq(term)
            if seq and seq not in self._index:
                self._index[seq] = label

    async def detect(self, text: str) -> list[Finding]:
        if not text.strip() or not self._index:
            return []

        tokens = _tokenize_text(text)
        # Lemmatize once; reuse across all term lookups
        lemmas = [(_lemmatize_word(word), start, end) for word, start, end in tokens]
        n = len(lemmas)

        findings: list[Finding] = []
        for seq, label in self._index.items():
            seq_len = len(seq)
            if seq_len > n:
                continue
            for i in range(n - seq_len + 1):
                if all(lemmas[i + j][0] == seq[j] for j in range(seq_len)):
                    start = lemmas[i][1]
                    end = lemmas[i + seq_len - 1][2]
                    findings.append(
                        Finding(
                            text=text[start:end],
                            label=label,
                            start=start,
                            end=end,
                            score=0.95,
                        )
                    )
        return findings

    @classmethod
    def from_defaults(cls) -> Gazetteer:
        """Build from the bundled rules/defaults/ term files."""
        return cls(_load_dir(_DEFAULTS_DIR))

    @classmethod
    def from_dir(cls, directory: Path) -> Gazetteer:
        """Build from a custom directory using the standard filename conventions."""
        return cls(_load_dir(directory))


def _load_dir(directory: Path) -> dict[str, str]:
    """Read the three category files from *directory*; return {term: label}."""
    result: dict[str, str] = {}
    for filename, label in _CATEGORY_FILES.items():
        path = directory / filename
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                result[line] = label
    return result


def load_defaults_terms() -> dict[str, str]:
    """Public helper: return {term: label} from the bundled defaults dir."""
    return _load_dir(_DEFAULTS_DIR)
