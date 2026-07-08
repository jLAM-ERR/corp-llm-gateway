import pytest

from corp_llm_gateway.detectors.base import PIIDetector
from corp_llm_gateway.detectors.dual_ner import DualNerDetector
from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector
from corp_llm_gateway.profiles import DETECTOR_REGISTRY, build_detectors


def test_build_detectors_maps_names_in_order() -> None:
    detectors = build_detectors(["regex_checksum", "dual_ner"])
    assert isinstance(detectors[0], RegexChecksumDetector)
    assert isinstance(detectors[1], DualNerDetector)


def test_build_detectors_dedups_preserving_first() -> None:
    assert len(build_detectors(["regex_checksum", "regex_checksum"])) == 1


def test_build_detectors_empty_is_empty_tuple() -> None:
    assert build_detectors([]) == ()


def test_build_detectors_accepts_cfg() -> None:
    assert len(build_detectors(["regex_checksum"], {"tuning": "on"})) == 1


def test_unknown_detector_raises_listing_known_set() -> None:
    with pytest.raises(ValueError, match="unknown detector 'nope'") as exc:
        build_detectors(["nope"])
    message = str(exc.value)
    for known in ("regex_checksum", "dual_ner", "ner_ru", "ner_en"):
        assert known in message


def test_registry_maps_every_known_name_to_a_detector() -> None:
    for name in DETECTOR_REGISTRY:
        (detector,) = build_detectors([name])
        assert isinstance(detector, PIIDetector)


def test_registry_values_are_lazy_factories_not_instances() -> None:
    assert all(callable(factory) for factory in DETECTOR_REGISTRY.values())
    assert not any(isinstance(factory, PIIDetector) for factory in DETECTOR_REGISTRY.values())
