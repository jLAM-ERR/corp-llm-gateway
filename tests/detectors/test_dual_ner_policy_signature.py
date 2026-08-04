"""`DualNerDetector.policy_signature()` — the Cache-A capability probe.

Fake / patched engines throughout, so these run identically on 3.14 (no
natasha/spaCy) and on 3.12/CI. Nothing here may skip.
"""

from __future__ import annotations

import pytest

from corp_llm_gateway.detectors import DualNerDetector
from corp_llm_gateway.detectors.base import Finding, PIIDetector, PolicySignatureDetector
from corp_llm_gateway.detectors.ner_en import EnNerDetector
from corp_llm_gateway.detectors.ner_ru import RuNerDetector

_PERSON = "John Smith met Анна Кузнецова in Moscow"


class _RaisingEngine(PIIDetector):
    async def detect(self, text: str) -> list[Finding]:
        raise RuntimeError("ner deps absent")


class _HookLessEngine(PIIDetector):
    async def detect(self, text: str) -> list[Finding]:
        return []


def _no_models(*_args: object, **_kwargs: object) -> object:
    raise RuntimeError("ner extra absent")


def _patch_models_present(m: pytest.MonkeyPatch) -> None:
    from corp_llm_gateway.detectors import ner_en, ner_ru

    m.setattr(ner_ru, "_load_natasha", lambda: ("segmenter", "tagger"))
    m.setattr(ner_en, "_load_spacy", lambda: "nlp")


def _patch_models_absent(m: pytest.MonkeyPatch) -> None:
    from corp_llm_gateway.detectors import ner_en, ner_ru

    m.setattr(ner_ru, "_load_natasha", _no_models)
    m.setattr(ner_en, "_load_spacy", _no_models)


def _dual() -> DualNerDetector:
    return DualNerDetector(require_ner=False, engines=[RuNerDetector(), EnNerDetector()])


def test_dual_ner_satisfies_the_policy_signature_protocol() -> None:
    assert isinstance(DualNerDetector(require_ner=False, engines=[]), PolicySignatureDetector)


def test_signature_differs_between_present_and_absent_models() -> None:
    with pytest.MonkeyPatch.context() as m:
        _patch_models_absent(m)
        absent = _dual().policy_signature()
    with pytest.MonkeyPatch.context() as m:
        _patch_models_present(m)
        present = _dual().policy_signature()

    assert absent != present
    assert all("none" in part for part in absent)


def test_signature_is_deterministic_and_all_strings() -> None:
    with pytest.MonkeyPatch.context() as m:
        _patch_models_present(m)
        first = _dual().policy_signature()
        second = _dual().policy_signature()

    assert first == second
    assert all(isinstance(part, str) for part in first)
    assert list(first) == sorted(first), "sorted, so engine order cannot fork the key"


def test_engine_order_does_not_change_the_signature() -> None:
    with pytest.MonkeyPatch.context() as m:
        _patch_models_present(m)
        forward = DualNerDetector(
            require_ner=False, engines=[RuNerDetector(), EnNerDetector()]
        ).policy_signature()
        reversed_ = DualNerDetector(
            require_ner=False, engines=[EnNerDetector(), RuNerDetector()]
        ).policy_signature()
    assert forward == reversed_


async def test_signature_is_immutable_across_a_request_time_engine_failure() -> None:
    """`_disabled` grows at REQUEST time; the signature must not follow it, or a
    key written at construction would change meaning after the fact."""
    det = DualNerDetector(require_ner=False, engines=[_RaisingEngine(), _HookLessEngine()])
    before = det.policy_signature()

    assert await det.detect(_PERSON) == []
    assert det._disabled == {0}, "the engine really did self-disable mid-life"

    assert det.policy_signature() == before


def test_engine_without_the_hook_falls_back_to_class_identity() -> None:
    sig = DualNerDetector(require_ner=False, engines=[_HookLessEngine()]).policy_signature()
    assert sig == (f"{_HookLessEngine.__module__}.{_HookLessEngine.__qualname__}",)


def test_require_ner_does_not_fragment_the_signature() -> None:
    """It changes what happens when an engine is MISSING, not what an intact pod
    redacts — two intact pods must keep sharing the cache."""
    with pytest.MonkeyPatch.context() as m:
        _patch_models_present(m)
        lax = DualNerDetector(require_ner=False, engines=[RuNerDetector()]).policy_signature()
        strict = DualNerDetector(require_ner=True, engines=[RuNerDetector()]).policy_signature()
    assert lax == strict


def test_no_engines_yields_an_empty_signature() -> None:
    assert DualNerDetector(require_ner=False, engines=[]).policy_signature() == ()
