"""Baked-in example profile bundles (D5).

Bundles live under ``src/corp_llm_gateway/profiles/defaults/`` so a single
``FileProfileLoader(<that dir>)`` resolves ``core`` / ``ru-152fz`` /
``division-x`` by profile_id. These tests assert each bundle's data parses, the
``extends`` chain composes ``core -> ru-152fz -> division-x`` in order, the
merged PolicyKnobs tighten (size=min, providers=intersection, most-closed
fail-policy), and the worked example redacts an RU sample end-to-end.

The worked-example redaction targets a regex_checksum value (an ИНН) so it
passes on the 3.14 gate venv where NER degrades to []. A separate NER-gated case
asserts PERSON redaction and skips when natasha/spaCy are absent.
"""

from pathlib import Path

import httpx
import pytest

import corp_llm_gateway.profiles as profiles_pkg
from corp_llm_gateway.corp_llm import CorpLlmClient
from corp_llm_gateway.detectors.regex_checksum import RegexChecksumDetector
from corp_llm_gateway.profiles import (
    FileProfileLoader,
    ProfileBundle,
    ProfileResolver,
    bundle_fingerprint,
    resolve_extends,
)
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.team_config import TeamConfig

BUNDLES_ROOT = Path(profiles_pkg.__file__).parent / "defaults"

# A checksum-valid ИНН (Сбербанк's public legal-entity requisite; not personal
# PII) — deterministically caught by regex_checksum on every venv.
_VALID_INN = "7707083893"


_ner_available = False
try:
    import natasha as _nat  # noqa: F401
    import spacy as _spa  # noqa: F401

    _ner_available = True
except ImportError:
    pass


class _StaticRulesLoader(RulesLoader):
    def __init__(self, rules: Rules) -> None:
        self._rules = rules

    async def load(self, team_id: str) -> Rules:
        return self._rules


def _client_that_must_not_be_called() -> CorpLlmClient:
    """A corp-LLM client whose transport raises — proves the oracle is skipped
    on a no-gazetteer-hit request (the RU sample carries no gazetteer term)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("oracle must not be called on a no-gazetteer-hit request")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CorpLlmClient("https://corp-llm.example", model="m", http=http)


def _orchestrator_for(bundle: ProfileBundle) -> SanitizationOrchestrator:
    return SanitizationOrchestrator(
        _client_that_must_not_be_called(),
        InMemoryMappingStore(),
        _StaticRulesLoader(bundle.rules),
        size_threshold_bytes=bundle.policy.size_threshold_bytes,
        local_detectors=list(bundle.detectors),
        gazetteer=bundle.gazetteer,
        allowlist=bundle.allowlist,
    )


async def _resolve_division_x() -> ProfileBundle:
    resolver = ProfileResolver(FileProfileLoader(BUNDLES_ROOT))
    return await resolver.resolve_team(
        TeamConfig(team_id="div-x-team", name="Division X", profile_ids=("division-x",))
    )


# each bundle's manifest + data files parse ----------------------------------


@pytest.mark.parametrize("profile_id", ["core", "ru-152fz", "division-x"])
async def test_bundle_manifest_parses(profile_id: str) -> None:
    manifest = await FileProfileLoader(BUNDLES_ROOT).read_manifest(profile_id)
    assert manifest.name == profile_id


async def test_core_bundle_terms_and_detectors() -> None:
    bundle = await FileProfileLoader(BUNDLES_ROOT).load("core")
    assert {type(d) for d in bundle.detectors} == {RegexChecksumDetector}
    assert bundle.rules.rules == ()  # core ships no replace.md
    assert bundle.gazetteer is not None
    findings = await bundle.gazetteer.detect("we ship Project Polaris this quarter")
    assert any(f.label == "PRODUCT" for f in findings)


async def test_ru152fz_bundle_terms_rules_allowlist() -> None:
    bundle = await FileProfileLoader(BUNDLES_ROOT).load("ru-152fz")
    assert {type(d).__name__ for d in bundle.detectors} == {
        "RegexChecksumDetector",
        "DualNerDetector",
    }
    assert bundle.gazetteer is not None
    findings = await bundle.gazetteer.detect("мы обрабатываем персональные данные")
    assert any(f.label == "REGULATED" for f in findings)
    # replace.md parsed (2 org rules)
    assert "[ORG_1]" in {r.replacement for r in bundle.rules.rules}
    # allowlist parsed: the synthetic value drops out of the pair set
    assert bundle.allowlist.filter_pairs((("test@example.com", "[EMAIL_1]"),)) == ()


async def test_division_x_bundle_rules_and_allowlist() -> None:
    bundle = await FileProfileLoader(BUNDLES_ROOT).load("division-x")
    # division-x adds no detectors/term files of its own (inherits via extends).
    assert bundle.detectors == ()
    assert bundle.gazetteer is None
    patterns = {r.pattern for r in bundle.rules.rules}
    assert "Project Chimera" in patterns
    assert bundle.allowlist.filter_pairs((("sandbox@example.com", "[EMAIL_1]"),)) == ()


# extends resolves core -> ru-152fz -> division-x in order -------------------


async def test_extends_resolves_in_layer_order() -> None:
    loader = FileProfileLoader(BUNDLES_ROOT)
    ordered = await resolve_extends(["division-x"], loader.read_manifest)
    assert ordered == ("core", "ru-152fz", "division-x")


# worked example: division-x composes and redacts an RU sample --------------


async def test_worked_example_composes_layers() -> None:
    bundle = await _resolve_division_x()
    assert bundle.profile_ids == ("core", "ru-152fz", "division-x")
    assert {type(d).__name__ for d in bundle.detectors} == {
        "RegexChecksumDetector",
        "DualNerDetector",
    }
    # merged rules concat both ru-152fz and division-x replace.md layers.
    patterns = {r.pattern for r in bundle.rules.rules}
    replacements = {r.replacement for r in bundle.rules.rules}
    assert "Project Chimera" in patterns  # division-x layer
    assert "[ORG_1]" in replacements  # ru-152fz layer


async def test_worked_example_redacts_ru_inn_end_to_end() -> None:
    bundle = await _resolve_division_x()
    orch = _orchestrator_for(bundle)
    sample = f"Клиент оставил заявку. ИНН {_VALID_INN}, почта ivan@example.com."
    result = await orch.sanitize(
        sample,
        team_id="div-x-team",
        conversation_id="c1",
        profile_fingerprint=bundle_fingerprint(bundle),
    )
    # regex_checksum-detectable value is redacted (venv-independent).
    assert _VALID_INN not in result.sanitized_text
    inn_pairs = [(o, p) for o, p in result.pairs if o == _VALID_INN]
    assert inn_pairs, "the ИНН must be redacted"
    assert inn_pairs[0][1].startswith("[RU_INN_")


async def test_worked_example_allowlisted_value_passes_through() -> None:
    bundle = await _resolve_division_x()
    orch = _orchestrator_for(bundle)
    # test@example.com is allowlisted by the ru-152fz layer → not redacted.
    result = await orch.sanitize(
        "напишите на test@example.com сегодня",
        team_id="div-x-team",
        conversation_id="c2",
        profile_fingerprint=bundle_fingerprint(bundle),
    )
    assert "test@example.com" in result.sanitized_text
    # Assert the allowlisted value specifically, not zero-findings overall:
    # natasha (RU NER) false-positives «сегодня» as PERSON on some models,
    # which is unrelated to the allowlist behavior under test.
    redacted_originals = [original for original, _ in result.pairs]
    assert "test@example.com" not in redacted_originals


# merged PolicyKnobs: most-restrictive-wins ---------------------------------


async def test_merged_policy_knobs_are_most_restrictive() -> None:
    bundle = await _resolve_division_x()
    policy = bundle.policy
    assert policy.size_threshold_bytes == 65536  # min: division-x tightens
    assert policy.allowed_providers == frozenset({"anthropic"})  # intersection
    assert policy.block_payloads is True  # OR across layers
    assert policy.dlp_guard is True  # OR across layers
    assert policy.oracle_mode == "any_local_finding"  # most-coverage
    assert policy.fail_policy.pre_pass_down == "fail-closed"  # most-closed
    assert policy.fail_policy.audit_buffer_full == "fail-closed"


# NER-gated: PERSON redaction (skips without natasha/spaCy) ------------------


@pytest.mark.skipif(not _ner_available, reason="natasha/spacy not available")
async def test_worked_example_redacts_person_with_ner() -> None:
    bundle = await _resolve_division_x()
    orch = _orchestrator_for(bundle)
    sample = "Инженер Сергей Кузнецов подготовил отчёт."
    result = await orch.sanitize(
        sample,
        team_id="div-x-team",
        conversation_id="c3",
        profile_fingerprint=bundle_fingerprint(bundle),
    )
    assert "Сергей Кузнецов" not in result.sanitized_text
    assert any(placeholder.startswith("[PERSON_") for _, placeholder in result.pairs)
