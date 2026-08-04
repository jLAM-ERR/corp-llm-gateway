"""Fail-closed error codes for the corp NER detector (B4).

``ner_error_code`` existed but nothing called it, so a corp NER outage was
reported as ``E_NER_UNAVAILABLE`` — the code for a MISSING LOCAL NER MODEL. An
operator paged on that would go looking at the wrong component.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.corp_ner import (
    E_CORP_NER_UNAVAILABLE,
    E_NER_UNAVAILABLE,
    CorpNerUnavailableError,
)
from corp_llm_gateway.detectors import NerUnavailableError
from corp_llm_gateway.detectors.base import Finding, PIIDetector
from corp_llm_gateway.detectors.corp_ner import FAILURE_COMPONENT
from corp_llm_gateway.litellm_hook import (
    E_DETECTOR_CONTRACT,
    CorpLlmGuardrail,
    GuardrailHttpException,
    _failure_component,
)
from corp_llm_gateway.metrics import MetricsExporter
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.local_pass import DetectorContractError
from corp_llm_gateway.storage import InMemoryMappingStore
from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo

_SECRET = "Мамонтов Пётр Ильич"


class _NoRules(RulesLoader):
    async def load(self, team_id: str) -> Rules:
        return Rules(rules=())


class _RaisingDetector(PIIDetector):
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def detect(self, text: str) -> list[Finding]:
        raise self._exc


class _RecordingMetrics(MetricsExporter):
    def __init__(self) -> None:
        self.failures: list[str] = []

    def record_block(self, block_reason: str) -> None:
        pass

    def record_failure(self, component: str) -> None:
        self.failures.append(component)

    def observe_request_latency(self, seconds: float, *, status: str) -> None:
        pass


def _guardrail(exc: Exception) -> tuple[CorpLlmGuardrail, ListSink, _RecordingMetrics]:
    store = InMemoryTokenStore()
    now = datetime.now(UTC)
    store.upsert(
        TokenInfo(
            corp_token="tok-1",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    orch = SanitizationOrchestrator(
        None,
        InMemoryMappingStore(),
        _NoRules(),
        local_detectors=[_RaisingDetector(exc)],
        oracle_enabled=False,
    )
    sink = ListSink()
    metrics = _RecordingMetrics()
    return (
        CorpLlmGuardrail(
            orch,
            AuthMiddleware(store),
            AuditLogger(sink, gateway_version="0.0.1"),
            metrics=metrics,
        ),
        sink,
        metrics,
    )


def _data(*, content: str = "hello", system: str | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "model": "claude",
        "messages": [{"role": "user", "content": content}],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }
    if system is not None:
        data["system"] = system
    return data


# ---- messages loop ----------------------------------------------------------


async def test_corp_ner_outage_is_distinguishable_from_a_missing_local_model() -> None:
    guardrail, _, metrics = _guardrail(CorpNerUnavailableError("corp-ner returned 502"))

    with pytest.raises(GuardrailHttpException) as ei:
        await guardrail.pre_call(_data(content=f"ping {_SECRET}"))

    assert ei.value.status_code == 503
    assert ei.value.error_code == E_CORP_NER_UNAVAILABLE
    assert metrics.failures == [FAILURE_COMPONENT]


async def test_missing_local_ner_model_keeps_its_own_code() -> None:
    guardrail, _, metrics = _guardrail(NerUnavailableError("model absent"))

    with pytest.raises(GuardrailHttpException) as ei:
        await guardrail.pre_call(_data(content=f"ping {_SECRET}"))

    assert ei.value.error_code == E_NER_UNAVAILABLE
    assert metrics.failures == ["ner"]


async def test_detector_contract_violation_gets_its_own_code() -> None:
    guardrail, _, metrics = _guardrail(DetectorContractError("detect_batch returned 2 for 3"))

    with pytest.raises(GuardrailHttpException) as ei:
        await guardrail.pre_call(_data(content=f"ping {_SECRET}"))

    assert ei.value.status_code == 503
    assert ei.value.error_code == E_DETECTOR_CONTRACT
    assert metrics.failures == ["sanitize"]


# ---- system / prompt-field path --------------------------------------------


async def test_prompt_field_path_uses_the_same_codes() -> None:
    guardrail, _, metrics = _guardrail(CorpNerUnavailableError("corp-ner timeout"))

    with pytest.raises(GuardrailHttpException) as ei:
        await guardrail.pre_call(_data(content="", system=f"owner {_SECRET}"))

    assert ei.value.error_code == E_CORP_NER_UNAVAILABLE
    assert metrics.failures == [FAILURE_COMPONENT]


async def test_prompt_field_contract_violation_uses_the_same_codes() -> None:
    guardrail, _, _ = _guardrail(DetectorContractError("out-of-range offset"))

    with pytest.raises(GuardrailHttpException) as ei:
        await guardrail.pre_call(_data(content="", system=f"owner {_SECRET}"))

    assert ei.value.error_code == E_DETECTOR_CONTRACT


# ---- M1-14: no originals in the failure surfaces ---------------------------


async def test_corp_ner_failure_logs_and_audit_carry_no_originals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    guardrail, sink, _ = _guardrail(CorpNerUnavailableError("corp-ner returned 502"))

    with caplog.at_level(logging.INFO), pytest.raises(GuardrailHttpException) as ei:
        await guardrail.pre_call(_data(content=f"ping {_SECRET}"))

    assert f"error_code={E_CORP_NER_UNAVAILABLE}" in caplog.text
    assert _SECRET not in caplog.text
    assert _SECRET not in str(ei.value)
    assert len(sink.records) == 1
    assert _SECRET not in json.dumps(sink.records[0], ensure_ascii=False)


# ---- the metric map an operator watches ------------------------------------


def test_every_detection_failure_code_maps_to_a_named_component() -> None:
    # An unmapped code silently becomes "other" — the gap that passes every test
    # and then blinds the operator during the outage it was added for.
    assert _failure_component(E_CORP_NER_UNAVAILABLE) == FAILURE_COMPONENT
    assert _failure_component(E_NER_UNAVAILABLE) == "ner"
    assert _failure_component(E_DETECTOR_CONTRACT) != "other"
