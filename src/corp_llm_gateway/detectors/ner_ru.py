"""RU NER detector backed by Natasha/Slovnet.

Models are lazy-loaded at first detect() call. Natasha/Slovnet are optional
(install via the 'ner' extra). This module is safe to import when they are absent.
"""

from __future__ import annotations

import asyncio
import threading

from corp_llm_gateway.detectors.base import Finding, PIIDetector

_RU_LABEL_MAP: dict[str, str] = {
    "PER": "PERSON",
    "LOC": "LOCATION",
    "ORG": "ORG",
}

# Module-level model cache. Set once on first successful load.
_natasha_models: tuple[object, object] | None = None
_natasha_tried: bool = False

# Natasha Doc/segmenter/tagger are not thread-safe for concurrent calls.
# Inference is serialised through this lock; model load happens on the event loop
# (single-threaded) so the lock is only needed during inference.
_ru_lock = threading.Lock()


def _load_natasha() -> tuple[object, object]:
    """Return cached (Segmenter, NewsNERTagger); raise RuntimeError if natasha absent."""
    global _natasha_models, _natasha_tried
    if _natasha_tried:
        if _natasha_models is None:
            raise RuntimeError("ner_ru requires the 'ner' extra: pip install -e '.[ner]'")
        return _natasha_models
    _natasha_tried = True
    try:
        from natasha import NewsEmbedding, NewsNERTagger, Segmenter
    except ImportError as exc:
        raise RuntimeError("ner_ru requires the 'ner' extra: pip install -e '.[ner]'") from exc
    _natasha_models = (Segmenter(), NewsNERTagger(NewsEmbedding()))
    return _natasha_models


def _infer_ru(models: tuple[object, object], text: str) -> list[Finding]:
    """Run Natasha NER in a worker thread, serialised by _ru_lock."""
    from natasha import Doc  # only reachable when natasha is available

    with _ru_lock:
        segmenter, ner_tagger = models
        doc = Doc(text)
        doc.segment(segmenter)  # type: ignore[attr-defined]
        doc.tag_ner(ner_tagger)  # type: ignore[attr-defined]
        findings: list[Finding] = []
        for span in doc.spans:  # type: ignore[attr-defined]
            label = _RU_LABEL_MAP.get(span.type)
            if label is None:
                continue
            findings.append(
                Finding(text=span.text, label=label, start=span.start, end=span.stop, score=0.8)
            )
        return findings


class RuNerDetector(PIIDetector):
    """Russian NER via Natasha/Slovnet. Maps PER→PERSON, LOC→LOCATION, ORG→ORG.

    Span offsets (.start, .stop) are character positions; text[start:stop] == span.text.
    Score is fixed at 0.8 (probabilistic model — not a hard rule).
    """

    async def detect(self, text: str) -> list[Finding]:
        if not text.strip():
            return []
        # Load model on the event loop (once, serialised by the loop); then offload
        # the CPU-bound inference to a thread so the event loop stays responsive.
        models = _load_natasha()
        return await asyncio.to_thread(_infer_ru, models, text)
