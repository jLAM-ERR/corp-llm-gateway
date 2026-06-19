"""End-to-end test running the SanitizationOrchestrator against the
corp-llm-mock + real Redis. Skipped unless explicit env vars are set
(so the suite is no-op outside docker-compose).

Run via:
  docker compose run --rm e2e

Or locally with running Redis + corp-llm-mock:
  REDIS_URL=redis://localhost:6379/0 \
  CORP_LLM_ENDPOINT=http://localhost:8000 \
  PYTHONPATH=src .venv/bin/pytest tests/e2e -q
"""

from __future__ import annotations

import contextlib
import os

import pytest
import redis.asyncio as redis_asyncio

from corp_llm_gateway.corp_llm import CorpLlmClient
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import RedisMappingStore

REDIS_URL = os.environ.get("REDIS_URL")
CORP_LLM_ENDPOINT = os.environ.get("CORP_LLM_ENDPOINT")

skip_if_no_e2e = pytest.mark.skipif(
    not (REDIS_URL and CORP_LLM_ENDPOINT),
    reason="REDIS_URL and CORP_LLM_ENDPOINT must be set for e2e",
)


class _StaticRules(RulesLoader):
    async def load(self, team_id: str) -> Rules:
        return Rules(rules=())


@pytest.fixture
async def orch():
    assert REDIS_URL and CORP_LLM_ENDPOINT
    r = redis_asyncio.from_url(REDIS_URL, decode_responses=True)
    with contextlib.suppress(Exception):
        await r.flushdb()

    client = CorpLlmClient(CORP_LLM_ENDPOINT, model="mock")
    store = RedisMappingStore(r)
    yield SanitizationOrchestrator(client, store, _StaticRules())
    await client.aclose()
    await r.aclose()


@skip_if_no_e2e
async def test_round_trip_against_mock(orch) -> None:
    result = await orch.sanitize(
        "send a note to alice@corp.lan for me",
        team_id="t1",
        conversation_id="c1",
    )
    assert "[EMAIL_001]" in result.sanitized_text
    assert "[NAME_001]" in result.sanitized_text
    assert "alice@corp.lan" not in result.sanitized_text
    assert result.cache_a_hit is False


@skip_if_no_e2e
async def test_cache_a_hit_on_repeat(orch) -> None:
    text = "ping alice@corp.lan again"
    a = await orch.sanitize(text, team_id="t1", conversation_id="c1")
    b = await orch.sanitize(text, team_id="t1", conversation_id="c2")
    assert a.cache_a_hit is False
    assert b.cache_a_hit is True
    assert a.sanitized_text == b.sanitized_text


@skip_if_no_e2e
async def test_per_team_cache_isolation(orch) -> None:
    text = "alice writes mail"
    await orch.sanitize(text, team_id="t1", conversation_id="c1")
    b = await orch.sanitize(text, team_id="t2", conversation_id="c1")
    assert b.cache_a_hit is False, "different teams must not share Cache A"
