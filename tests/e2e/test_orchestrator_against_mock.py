"""End-to-end test running the SanitizationOrchestrator against the
corp-llm-mock over HTTP + real Redis, wired local-first the way bootstrap
wires it: regex+checksum locally, a gazetteer, and the oracle called only on
a gazetteer hit. Skipped unless explicit env vars are set.

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

import httpx
import pytest
import redis.asyncio as redis_asyncio

from corp_llm_gateway.corp_llm import CorpLlmClient
from corp_llm_gateway.detectors import RegexChecksumDetector
from corp_llm_gateway.rules import Gazetteer, Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.storage import RedisMappingStore

REDIS_URL = os.environ.get("REDIS_URL")
CORP_LLM_ENDPOINT = os.environ.get("CORP_LLM_ENDPOINT")

skip_if_no_e2e = pytest.mark.skipif(
    not (REDIS_URL and CORP_LLM_ENDPOINT),
    reason="REDIS_URL and CORP_LLM_ENDPOINT must be set for e2e",
)

# A made-up product term: the gazetteer hit that makes the orchestrator call the oracle.
PRODUCT = "Zorblax"
# The mock's default pairs: alice@corp.lan -> [EMAIL_001], alice -> [NAME_001].
# No local detector finds a bare name, so [NAME_001] in a result came from the oracle.
HIT_TEXT = f"alice asked about the {PRODUCT} launch, reply to alice@corp.lan"
NO_HIT_TEXT = "alice asked me to reply to alice@corp.lan"


class _StaticRules(RulesLoader):
    async def load(self, team_id: str) -> Rules:
        return Rules(rules=())


class _Mock:
    def __init__(self, control: httpx.AsyncClient) -> None:
        self._control = control

    async def calls(self) -> int:
        resp = await self._control.get("/__calls")
        resp.raise_for_status()
        return resp.json()["count"]


@pytest.fixture
async def redis_client():
    assert REDIS_URL
    r = redis_asyncio.from_url(REDIS_URL, decode_responses=True)
    with contextlib.suppress(Exception):
        await r.flushdb()
    yield r
    await r.aclose()


@pytest.fixture
async def mock():
    assert CORP_LLM_ENDPOINT
    async with httpx.AsyncClient(base_url=CORP_LLM_ENDPOINT, timeout=5.0) as control:
        (await control.delete("/__calls")).raise_for_status()
        yield _Mock(control)


@pytest.fixture
async def orch(redis_client):
    assert CORP_LLM_ENDPOINT
    client = CorpLlmClient(CORP_LLM_ENDPOINT, model="mock")
    yield SanitizationOrchestrator(
        client,
        RedisMappingStore(redis_client),
        _StaticRules(),
        local_detectors=[RegexChecksumDetector()],
        gazetteer=Gazetteer({PRODUCT: "PRODUCT"}),
    )
    await client.aclose()


@skip_if_no_e2e
async def test_round_trip_against_mock(orch, mock) -> None:
    result = await orch.sanitize(HIT_TEXT, team_id="t1", conversation_id="c1")

    assert await mock.calls() == 1
    assert result.sanitized_text == (
        "[NAME_001] asked about the [PRODUCT_001] launch, reply to [EMAIL_001]"
    )
    assert ("alice", "[NAME_001]") in result.pairs
    assert result.cache_a_hit is False
    # Cache B, read back from Redis over a separate connection.
    assert REDIS_URL
    r = redis_asyncio.from_url(REDIS_URL, decode_responses=True)
    try:
        store = RedisMappingStore(r)
        assert await store.get_original("c1", "[NAME_001]") == "alice"
        assert await store.get_original("c1", "[EMAIL_001]") == "alice@corp.lan"
    finally:
        await r.aclose()


@skip_if_no_e2e
async def test_no_gazetteer_hit_skips_the_oracle(orch, mock) -> None:
    result = await orch.sanitize(NO_HIT_TEXT, team_id="t1", conversation_id="c1")

    assert await mock.calls() == 0
    assert result.sanitized_text == "alice asked me to reply to [EMAIL_001]"
    assert result.pairs == (("alice@corp.lan", "[EMAIL_001]"),)


@skip_if_no_e2e
async def test_cache_a_hit_on_repeat(orch, mock) -> None:
    a = await orch.sanitize(HIT_TEXT, team_id="t1", conversation_id="c1")
    b = await orch.sanitize(HIT_TEXT, team_id="t1", conversation_id="c2")
    assert a.cache_a_hit is False
    assert b.cache_a_hit is True
    assert await mock.calls() == 1, "a Cache A hit must not call the oracle again"
    assert a.sanitized_text == b.sanitized_text
    assert "[NAME_001]" in b.sanitized_text


@skip_if_no_e2e
async def test_per_team_cache_isolation(orch, mock) -> None:
    await orch.sanitize(HIT_TEXT, team_id="t1", conversation_id="c1")
    b = await orch.sanitize(HIT_TEXT, team_id="t2", conversation_id="c1")
    assert b.cache_a_hit is False, "different teams must not share Cache A"
    assert await mock.calls() == 2
    # Cache A is on for t2 too, so the miss above is isolation, not a disabled cache.
    c = await orch.sanitize(HIT_TEXT, team_id="t2", conversation_id="c2")
    assert c.cache_a_hit is True
    assert await mock.calls() == 2
