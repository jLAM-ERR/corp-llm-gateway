"""EN NER detector backed by spaCy en_core_web_md.

Model is lazy-loaded at first detect() call. spaCy is optional (install via the
'ner' extra). en_core_web_md is not on PyPI by name — install via its wheel URL
(see pyproject.toml [project.optional-dependencies] ner comment).
This module is safe to import when spaCy or the model are absent.
"""

from __future__ import annotations

from corp_llm_gateway.detectors.base import Finding, PIIDetector

_EN_LABEL_MAP: dict[str, str] = {
    "PERSON": "PERSON",
    "ORG": "ORG",
    "GPE": "LOCATION",  # geo-political entity
    "LOC": "LOCATION",  # non-GPE location
}

# Module-level model cache. Set once on first successful load.
_spacy_nlp: object | None = None
_spacy_tried: bool = False


def _load_spacy() -> object:
    """Return cached spaCy Language object; raise RuntimeError if dep/model absent."""
    global _spacy_nlp, _spacy_tried
    if _spacy_tried:
        if _spacy_nlp is None:
            raise RuntimeError("ner_en requires the 'ner' extra: pip install -e '.[ner]'")
        return _spacy_nlp
    _spacy_tried = True
    try:
        import spacy
    except ImportError as exc:
        raise RuntimeError("ner_en requires the 'ner' extra: pip install -e '.[ner]'") from exc
    try:
        _spacy_nlp = spacy.load("en_core_web_md")
    except OSError as exc:
        raise RuntimeError(
            "spaCy model 'en_core_web_md' not installed; "
            "install via its wheel URL (see pyproject.toml ner extra)"
        ) from exc
    return _spacy_nlp


class EnNerDetector(PIIDetector):
    """English NER via spaCy en_core_web_md.

    Maps PERSON→PERSON, ORG→ORG, GPE/LOC→LOCATION.
    Span offsets (start_char, end_char) are character positions;
    text[start_char:end_char] == ent.text.
    Score is fixed at 0.8 (probabilistic model — not a hard rule).
    """

    async def detect(self, text: str) -> list[Finding]:
        if not text.strip():
            return []
        nlp = _load_spacy()
        doc = nlp(text)  # type: ignore[call-arg]
        findings: list[Finding] = []
        for ent in doc.ents:  # type: ignore[attr-defined]
            label = _EN_LABEL_MAP.get(ent.label_)
            if label is None:
                continue
            findings.append(
                Finding(
                    text=ent.text,
                    label=label,
                    start=ent.start_char,
                    end=ent.end_char,
                    score=0.8,
                )
            )
        return findings
