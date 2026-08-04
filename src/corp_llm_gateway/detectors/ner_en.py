"""EN NER detector backed by spaCy en_core_web_md.

Model is lazy-loaded at first detect() call. spaCy is optional (install via the
'ner' extra). en_core_web_md is not on PyPI by name — install via its wheel URL
(see pyproject.toml [project.optional-dependencies] ner comment).
This module is safe to import when spaCy or the model are absent.
"""

from __future__ import annotations

import asyncio
import threading

from corp_llm_gateway.detectors.base import Finding, PIIDetector, package_version

_EN_MODEL = "en_core_web_md"

_EN_LABEL_MAP: dict[str, str] = {
    "PERSON": "PERSON",
    "ORG": "ORG",
    "GPE": "LOCATION",  # geo-political entity
    "LOC": "LOCATION",  # non-GPE location
}

# Module-level model cache. Set once on first successful load.
_spacy_nlp: object | None = None
_spacy_tried: bool = False

# spaCy Language objects are not thread-safe for concurrent nlp(text) calls.
# Inference is serialised through this lock; model load happens on the event loop
# (single-threaded) so the lock is only needed during inference.
_en_lock = threading.Lock()


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
        _spacy_nlp = spacy.load(_EN_MODEL)
    except OSError as exc:
        raise RuntimeError(
            f"spaCy model {_EN_MODEL!r} not installed; "
            "install via its wheel URL (see pyproject.toml ner extra)"
        ) from exc
    return _spacy_nlp


def _infer_en(nlp: object, text: str) -> list[Finding]:
    """Run spaCy NER in a worker thread, serialised by _en_lock."""
    with _en_lock:
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


class EnNerDetector(PIIDetector):
    """English NER via spaCy en_core_web_md.

    Maps PERSON→PERSON, ORG→ORG, GPE/LOC→LOCATION.
    Span offsets (start_char, end_char) are character positions;
    text[start_char:end_char] == ent.text.
    Score is fixed at 0.8 (probabilistic model — not a hard rule).
    """

    def policy_signature(self) -> tuple[str, ...]:
        """Whether this process can actually run EN NER, plus spaCy + model versions.

        Loading is FORCED here for the same reason as ner_ru: ``_load_spacy``
        latches ``_spacy_tried``, so the answer cannot change after the Cache-A
        key has been written.
        """
        try:
            nlp = _load_spacy()
        except RuntimeError:
            return ("en_ner:none",)
        meta = getattr(nlp, "meta", None)
        model_version = meta.get("version", "unknown") if isinstance(meta, dict) else "unknown"
        return (f"en_ner:spacy:{package_version('spacy')}:{_EN_MODEL}:{model_version}",)

    async def detect(self, text: str) -> list[Finding]:
        if not text.strip():
            return []
        # Load model on the event loop (once, serialised by the loop); then offload
        # the CPU-bound inference to a thread so the event loop stays responsive.
        nlp = _load_spacy()
        return await asyncio.to_thread(_infer_en, nlp, text)
