from pathlib import Path

import pytest

from corp_llm_gateway.detectors.base import PIIDetector
from corp_llm_gateway.detectors.dual_ner import DualNerDetector
from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector
from corp_llm_gateway.profiles import DETECTOR_REGISTRY, build_detectors
from corp_llm_gateway.settings import ConfigError


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


def test_registry_maps_every_known_name_to_a_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    # corp_ner is the one network-backed detector; it needs an endpoint to build.
    monkeypatch.setenv("CORP_NER_ENDPOINT", "https://corp-ner.test")
    for name in DETECTOR_REGISTRY:
        (detector,) = build_detectors([name])
        assert isinstance(detector, PIIDetector)


def test_corp_ner_is_registered_for_profile_bundles() -> None:
    assert "corp_ner" in DETECTOR_REGISTRY


def test_corp_ner_endpoint_comes_from_the_bundle_cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORP_NER_ENDPOINT", raising=False)
    (detector,) = build_detectors(["corp_ner"], {"corp_ner_endpoint": "https://from-cfg.test"})
    assert detector._client._base_url == "https://from-cfg.test"


def test_corp_ner_profile_client_reads_the_transport_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same knobs, same values as bootstrap.build_corp_ner: a profile-declared
    # detector that ignored them silently ran on the code defaults.
    monkeypatch.setenv("CORP_NER_ENDPOINT", "https://corp-ner.test")
    monkeypatch.setenv("CORP_NER_TIMEOUT_S", "7")
    monkeypatch.setenv("CORP_NER_MAX_TEXTS", "11")
    monkeypatch.setenv("CORP_NER_MAX_INPUT_CHARS", "1234")

    (detector,) = build_detectors(["corp_ner"])

    client = detector._client
    assert (client._timeout, client._max_texts, client._max_input_chars) == (7.0, 11, 1234)


def test_corp_ner_profile_client_uses_the_configured_ca_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Ignoring CORP_NER_CA_BUNDLE made an HTTPS NER service signed by the internal
    # CA fail TLS verification, and every request of that team 503'd fail-closed.
    # A bundle path that does not exist must fail loudly, not fall back to the
    # system trust store.
    monkeypatch.setenv("CORP_NER_ENDPOINT", "https://corp-ner.test")
    monkeypatch.setenv("CORP_NER_CA_BUNDLE", str(tmp_path / "absent-ca.pem"))

    with pytest.raises(FileNotFoundError):
        build_detectors(["corp_ner"])


def test_corp_ner_profile_client_refuses_a_non_numeric_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORP_NER_ENDPOINT", "https://corp-ner.test")
    monkeypatch.setenv("CORP_NER_TIMEOUT_S", "soon")

    with pytest.raises(ConfigError, match="CORP_NER_TIMEOUT_S"):
        build_detectors(["corp_ner"])


def test_corp_ner_without_an_endpoint_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Never build a detector pointed nowhere: a connect error per request would
    # read as an outage instead of the misconfiguration it is.
    monkeypatch.delenv("CORP_NER_ENDPOINT", raising=False)
    with pytest.raises(ValueError, match="CORP_NER_ENDPOINT"):
        build_detectors(["corp_ner"])


def test_registry_values_are_lazy_factories_not_instances() -> None:
    assert all(callable(factory) for factory in DETECTOR_REGISTRY.values())
    assert not any(isinstance(factory, PIIDetector) for factory in DETECTOR_REGISTRY.values())
