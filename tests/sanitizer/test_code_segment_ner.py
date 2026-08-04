"""CODE segments stay in scope for every LOCAL detector, local NER included.

Guards a real leak: when the composition root narrowed `code_safe_detectors` to
regex/checksum only, PERSON/ORG/LOCATION inside fenced JSON, SQL values, config
examples and test fixtures stopped being detected and egressed unredacted.
Only NETWORK-backed detectors (corp NER) are excluded from CODE.

The NER engine here is a stub, so this runs without the `ner` extra.
"""

from __future__ import annotations

import pytest

from corp_llm_gateway import bootstrap
from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.detectors.dual_ner import DualNerDetector

_NAME = "John Smith"
_FENCED_JSON = 'Here is the fixture:\n```json\n{"owner": "John Smith"}\n```\n'
_PROSE = "Here is the fixture: the owner is John Smith.\n"


class _NeedleNerEngine(PIIDetector):
    """Stub NER engine: reports one literal as PERSON, so no model is loaded."""

    def __init__(self, needle: str) -> None:
        self._needle = needle

    async def detect(self, text: str) -> list[Finding]:
        start = text.find(self._needle)
        if start < 0:
            return []
        return [Finding(self._needle, "PERSON", start, start + len(self._needle), 0.9)]


@pytest.fixture(autouse=True)
def _clean_config(hermetic_gateway_config: None) -> None:
    """Resolve config hermetically (see tests/conftest.py)."""


@pytest.fixture
def _stub_local_ner(monkeypatch: pytest.MonkeyPatch) -> None:
    # CORP_NER_ENABLED is left at its default (0) — this is what every existing
    # deploy runs. Oracle off so the local cascade is the only detection path.
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", "0")
    monkeypatch.setattr(
        bootstrap,
        "DualNerDetector",
        lambda: DualNerDetector(engines=[_NeedleNerEngine(_NAME)], require_ner=False),
    )


async def _sanitize(text: str) -> tuple[str, set[str]]:
    guardrail = bootstrap.build_guardrail()
    result = await guardrail._orch._core.sanitize(text, team_id="t1", conversation_id="c1")
    return result.sanitized_text, {o for o, _ in result.pairs}


@pytest.mark.usefixtures("_stub_local_ner")
async def test_local_ner_redacts_a_name_inside_a_fenced_code_block() -> None:
    sanitized, originals = await _sanitize(_FENCED_JSON)

    assert _NAME not in sanitized
    assert _NAME in originals


@pytest.mark.usefixtures("_stub_local_ner")
async def test_local_ner_redacts_the_same_name_in_prose() -> None:
    sanitized, originals = await _sanitize(_PROSE)

    assert _NAME not in sanitized
    assert _NAME in originals
