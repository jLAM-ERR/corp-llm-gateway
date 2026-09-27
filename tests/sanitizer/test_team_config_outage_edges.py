"""A team-config store that cannot answer: where the timeout fires does not change
the outcome, a cancellation is never mistaken for an outage, and neither the
error nor the audit record carries the driver's text."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from corp_llm_gateway.litellm_hook import GuardrailHttpException
from corp_llm_gateway.sanitizer.profile_orchestrator import (
    ProfileAwareOrchestrator,
    TeamConfigUnavailableError,
)
from corp_llm_gateway.team_config import InMemoryTeamConfigStore, TeamConfig
from tests.sanitizer.test_profile_orchestrator import (
    _STORE_CANARY,
    _data,
    _guardrail,
)
from tests.test_litellm_hook import _RecordingMetrics


class _Store(InMemoryTeamConfigStore):
    def __init__(self, get: object) -> None:
        super().__init__()
        self._get = get

    async def get(self, team_id: str) -> TeamConfig:
        return await self._get()  # type: ignore[operator]


async def _nested_timeout() -> TeamConfig:
    async def statement() -> None:
        async def fetch() -> None:
            raise TimeoutError(f"statement on {_STORE_CANARY} timed out")

        await fetch()

    await statement()
    raise AssertionError("unreachable")


async def _acquire_timeout() -> TeamConfig:
    raise TimeoutError(f"pool acquire for {_STORE_CANARY} timed out")


async def _reset() -> TeamConfig:
    raise ConnectionResetError(f"reset by {_STORE_CANARY}")


@pytest.mark.parametrize(
    ("get", "error_class"),
    [
        (_nested_timeout, "TimeoutError"),
        (_acquire_timeout, "TimeoutError"),
        (_reset, "ConnectionResetError"),
    ],
    ids=["nested-await", "acquire", "reset"],
)
async def test_the_outage_error_carries_the_class_name_and_nothing_else(
    tmp_path: Path, get: object, error_class: str
) -> None:
    orch = _guardrail(tmp_path, _Store(get))[0].orchestrator
    assert isinstance(orch, ProfileAwareOrchestrator)

    with pytest.raises(TeamConfigUnavailableError) as caught:
        await orch.resolve("t1")

    err = caught.value
    assert err.error_class == error_class
    assert str(err) == error_class
    assert err.__cause__ is None
    assert err.__context__ is None
    assert _STORE_CANARY not in repr(err)


@pytest.mark.parametrize("get", [_nested_timeout, _acquire_timeout, _reset])
async def test_the_outage_audit_record_carries_no_driver_text(tmp_path: Path, get: object) -> None:
    g, sink = _guardrail(tmp_path, _Store(get), metrics=_RecordingMetrics())

    with pytest.raises(GuardrailHttpException) as caught:
        await g.pre_call(_data(content="hello"))

    assert caught.value.status_code == 503
    (record,) = sink.records
    assert record["error_code"] == "E_PROFILE_UNAVAILABLE"
    assert _STORE_CANARY not in json.dumps(record)


async def test_a_cancelled_lookup_is_a_cancellation_not_an_outage(tmp_path: Path) -> None:
    entered = asyncio.Event()

    async def stalled() -> TeamConfig:
        entered.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    metrics = _RecordingMetrics()
    g, sink = _guardrail(tmp_path, _Store(stalled), metrics=metrics)
    task = asyncio.create_task(g.pre_call(_data(content="hello")))
    await asyncio.wait_for(entered.wait(), 2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)

    assert "team_config" not in metrics.failures
    assert all(r.get("error_code") != "E_PROFILE_UNAVAILABLE" for r in sink.records)
