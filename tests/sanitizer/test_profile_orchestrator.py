"""D4: ProfileAwareOrchestrator wrapper + its litellm_hook pre_call wiring.

The wrapper activates the profile architecture at request time: it resolves a
team's merged ProfileBundle (D1/D2), memoizes ONE inner SanitizationOrchestrator
per resolved layer-key, and folds the D3 bundle_fingerprint into Cache A so a
result never bleeds across profiles. Empty profile_ids → the core orchestrator +
no fingerprint (byte-identical to today).
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from corp_llm_gateway import config as _cfg_module
from corp_llm_gateway.audit import AuditLogger, ListSink
from corp_llm_gateway.corp_llm import CorpLlmClient
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail, GuardrailHttpException
from corp_llm_gateway.profiles import (
    FileProfileLoader,
    ProfileNotFoundError,
    ProfileResolver,
    bundle_fingerprint,
)
from corp_llm_gateway.rules import Rules, RulesLoader
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.profile_orchestrator import (
    ProfileAwareOrchestrator,
    build_inner_orchestrator,
    passthrough_resolved,
)
from corp_llm_gateway.storage import InMemoryMappingStore, MappingStore
from corp_llm_gateway.team_config import InMemoryTeamConfigStore, TeamConfig
from corp_llm_gateway.tokens import AuthMiddleware, InMemoryTokenStore, TokenInfo
from tests.test_litellm_hook import _corp_llm_returning

_CONFIG_PAYLOAD = (
    "DATABASE_URL=postgres://admin:pass@db/prod\n"
    "SECRET_KEY=abc123\nDEBUG=False\nREDIS_URL=redis://cache\nLOG_LEVEL=ERROR\n"
)


class _StaticRules(RulesLoader):
    async def load(self, team_id: str) -> Rules:
        return Rules(rules=())


def _write_profile(root: Path, pid: str, toml: str, *, products: str | None = None) -> None:
    directory = root / pid
    directory.mkdir(parents=True)
    (directory / "profile.toml").write_text(toml, encoding="utf-8")
    if products is not None:
        (directory / "products.txt").write_text(products, encoding="utf-8")


def _core_orch(store: MappingStore, corp_llm: CorpLlmClient) -> SanitizationOrchestrator:
    return SanitizationOrchestrator(corp_llm, store, _StaticRules())


def _wrapper(
    root: Path,
    *,
    team_store: InMemoryTeamConfigStore,
    store: MappingStore,
    corp_llm: CorpLlmClient,
    core: SanitizationOrchestrator,
) -> ProfileAwareOrchestrator:
    resolver = ProfileResolver(FileProfileLoader(root))

    def build_inner(bundle: object) -> SanitizationOrchestrator:
        return build_inner_orchestrator(
            bundle,  # type: ignore[arg-type]
            corp_llm=corp_llm,
            mapping_store=store,
            base_rules_loader=_StaticRules(),
        )

    return ProfileAwareOrchestrator(
        core, team_store=team_store, resolver=resolver, build_inner=build_inner
    )


async def _team_store(**teams: tuple[str, ...]) -> InMemoryTeamConfigStore:
    store = InMemoryTeamConfigStore()
    for team_id, profile_ids in teams.items():
        await store.upsert(TeamConfig(team_id=team_id, name=team_id, profile_ids=profile_ids))
    return store


# --- wrapper unit tests ----------------------------------------------------


async def test_empty_profile_resolves_to_core_no_fingerprint(tmp_path: Path) -> None:
    team_store = await _team_store(t1=())
    store = InMemoryMappingStore()
    corp = _corp_llm_returning([])
    core = _core_orch(store, corp)
    wrapper = _wrapper(tmp_path, team_store=team_store, store=store, corp_llm=corp, core=core)

    resolved = await wrapper.resolve("t1")
    assert resolved.orchestrator is core
    assert resolved.fingerprint is None
    assert resolved.profile_ids == ()


async def test_unknown_team_resolves_to_core(tmp_path: Path) -> None:
    team_store = InMemoryTeamConfigStore()  # no teams at all
    store = InMemoryMappingStore()
    corp = _corp_llm_returning([])
    core = _core_orch(store, corp)
    wrapper = _wrapper(tmp_path, team_store=team_store, store=store, corp_llm=corp, core=core)

    resolved = await wrapper.resolve("ghost-team")
    assert resolved.orchestrator is core
    assert resolved.fingerprint is None


async def test_profile_selects_and_memoizes_inner(tmp_path: Path) -> None:
    _write_profile(tmp_path, "p1", 'name = "p1"\ndetectors = ["regex_checksum"]\n')
    _write_profile(tmp_path, "p2", 'name = "p2"\ndetectors = ["regex_checksum"]\n')
    team_store = await _team_store(t1=("p1",), t2=("p1",), t3=("p2",))
    store = InMemoryMappingStore()
    corp = _corp_llm_returning([])
    core = _core_orch(store, corp)
    wrapper = _wrapper(tmp_path, team_store=team_store, store=store, corp_llm=corp, core=core)

    r1 = await wrapper.resolve("t1")
    r1_again = await wrapper.resolve("t1")
    r2 = await wrapper.resolve("t2")  # different team, SAME layer-key
    r3 = await wrapper.resolve("t3")  # different layer-key

    assert r1.orchestrator is not core
    assert r1.orchestrator is r1_again.orchestrator, "same team reuses the memoized inner"
    assert r2.orchestrator is r1.orchestrator, "same layer-key shared across teams"
    assert r3.orchestrator is not r1.orchestrator, "different layer-key → different inner"
    assert r1.profile_ids == ("p1",)
    assert r3.profile_ids == ("p2",)


async def test_resolved_fingerprint_matches_bundle(tmp_path: Path) -> None:
    _write_profile(tmp_path, "p1", 'name = "p1"\ndetectors = ["regex_checksum"]\n')
    team_store = await _team_store(t1=("p1",))
    store = InMemoryMappingStore()
    corp = _corp_llm_returning([])
    core = _core_orch(store, corp)
    wrapper = _wrapper(tmp_path, team_store=team_store, store=store, corp_llm=corp, core=core)

    resolved = await wrapper.resolve("t1")
    bundle = await ProfileResolver(FileProfileLoader(tmp_path)).resolve_team(
        TeamConfig(team_id="t1", name="t1", profile_ids=("p1",))
    )
    assert resolved.fingerprint is not None
    assert resolved.fingerprint == bundle_fingerprint(bundle)


async def test_sanitize_passes_bundle_fingerprint_to_inner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_profile(tmp_path, "p1", 'name = "p1"\ndetectors = ["regex_checksum"]\n')
    team_store = await _team_store(t1=("p1",))
    store = InMemoryMappingStore()
    corp = _corp_llm_returning([])
    core = _core_orch(store, corp)
    wrapper = _wrapper(tmp_path, team_store=team_store, store=store, corp_llm=corp, core=core)

    resolved = await wrapper.resolve("t1")
    captured: dict[str, str | None] = {}
    inner_sanitize = resolved.orchestrator.sanitize

    async def _spy(text: str, *, team_id: str, conversation_id: str, profile_fingerprint=None):  # type: ignore[no-untyped-def]
        captured["fp"] = profile_fingerprint
        return await inner_sanitize(
            text,
            team_id=team_id,
            conversation_id=conversation_id,
            profile_fingerprint=profile_fingerprint,
        )

    monkeypatch.setattr(resolved.orchestrator, "sanitize", _spy)
    await wrapper.sanitize("hello", team_id="t1", conversation_id="c1")
    assert captured["fp"] == resolved.fingerprint
    assert captured["fp"] is not None


async def test_empty_profile_sanitize_passes_none_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team_store = await _team_store(t1=())
    store = InMemoryMappingStore()
    corp = _corp_llm_returning([])
    core = _core_orch(store, corp)
    wrapper = _wrapper(tmp_path, team_store=team_store, store=store, corp_llm=corp, core=core)

    captured: dict[str, str | None] = {"fp": "SENTINEL"}
    core_sanitize = core.sanitize

    async def _spy(text: str, *, team_id: str, conversation_id: str, profile_fingerprint=None):  # type: ignore[no-untyped-def]
        captured["fp"] = profile_fingerprint
        return await core_sanitize(
            text,
            team_id=team_id,
            conversation_id=conversation_id,
            profile_fingerprint=profile_fingerprint,
        )

    monkeypatch.setattr(core, "sanitize", _spy)
    await wrapper.sanitize("hello", team_id="t1", conversation_id="c1")
    assert captured["fp"] is None


async def test_fingerprint_prevents_cross_profile_cache_bleed(tmp_path: Path) -> None:
    """D3 regression at the wrapper level: same team_id + text, two profiles that
    differ ONLY by gazetteer (NOT rules, so the plain content-hash is identical),
    one SHARED Cache A. The permissive profile seeds an un-redacted entry; the
    strict profile must MISS it (distinct fingerprint) and redact the term."""
    _write_profile(tmp_path, "permissive", 'name = "permissive"\ndetectors = ["regex_checksum"]\n')
    _write_profile(
        tmp_path,
        "strict",
        'name = "strict"\ndetectors = ["regex_checksum"]\n',
        products="sekret\n",
    )
    store = InMemoryMappingStore()  # ONE shared Cache A
    corp = _corp_llm_returning([])
    # Two wrappers mapping the SAME team_id to different profiles keeps team_id
    # (which IS in the content hash) constant, isolating the fingerprint's effect.
    ts_perm = await _team_store(t1=("permissive",))
    ts_strict = await _team_store(t1=("strict",))
    w_perm = _wrapper(
        tmp_path, team_store=ts_perm, store=store, corp_llm=corp, core=_core_orch(store, corp)
    )
    w_strict = _wrapper(
        tmp_path, team_store=ts_strict, store=store, corp_llm=corp, core=_core_orch(store, corp)
    )

    text = "deploy sekret now"
    r_perm = await w_perm.sanitize(text, team_id="t1", conversation_id="c-perm")
    assert "sekret" in r_perm.sanitized_text, "permissive profile redacts nothing"

    r_strict = await w_strict.sanitize(text, team_id="t1", conversation_id="c-strict")
    assert r_strict.cache_a_hit is False, "distinct fingerprint must miss the permissive entry"
    assert "sekret" not in r_strict.sanitized_text, "LEAK: strict profile must redact the term"

    fp_perm = (await w_perm.resolve("t1")).fingerprint
    fp_strict = (await w_strict.resolve("t1")).fingerprint
    assert fp_perm != fp_strict


async def test_unknown_profile_resolve_fails_closed(tmp_path: Path) -> None:
    team_store = await _team_store(t1=("ghost",))  # ghost never written to disk
    store = InMemoryMappingStore()
    corp = _corp_llm_returning([])
    wrapper = _wrapper(
        tmp_path, team_store=team_store, store=store, corp_llm=corp, core=_core_orch(store, corp)
    )
    with pytest.raises(ProfileNotFoundError, match="ghost"):
        await wrapper.resolve("t1")


def test_passthrough_resolved_shape() -> None:
    store = InMemoryMappingStore()
    core = _core_orch(store, _corp_llm_returning([]))
    resolved = passthrough_resolved(core)
    assert resolved.orchestrator is core
    assert resolved.fingerprint is None
    assert resolved.profile_ids == ()
    assert resolved.policy.allowed_providers is None


# --- litellm_hook pre_call wiring ------------------------------------------


def _guardrail(
    root: Path,
    team_store: InMemoryTeamConfigStore,
    *,
    corp_llm: CorpLlmClient | None = None,
    dlp_guard: object | None = None,
) -> tuple[CorpLlmGuardrail, ListSink]:
    store = InMemoryMappingStore()
    corp = corp_llm if corp_llm is not None else _corp_llm_returning([])
    core = _core_orch(store, corp)
    wrapper = _wrapper(root, team_store=team_store, store=store, corp_llm=corp, core=core)
    token_store = InMemoryTokenStore()
    now = datetime.now(UTC)
    token_store.upsert(
        TokenInfo(
            corp_token="tok-1",
            user_id="alice",
            team_id="t1",
            scopes=("read",),
            issued_at=now,
            expires_at=now + timedelta(days=30),
        )
    )
    sink = ListSink()
    guardrail = CorpLlmGuardrail(
        wrapper,
        AuthMiddleware(token_store),
        AuditLogger(sink, gateway_version="0.0.1"),
        dlp_guard=dlp_guard,  # type: ignore[arg-type]
    )
    return guardrail, sink


def _data(*, content: str = "hello", model: str = "claude") -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "headers": {"X-Corp-Auth": "tok-1", "Authorization": "Bearer byok"},
    }


async def test_pre_call_provider_gate_rejects_banned_provider(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "anthropic-only",
        'name = "anthropic-only"\ndetectors = ["regex_checksum"]\n'
        '[policy]\nallowed_providers = ["anthropic"]\n',
    )
    team_store = await _team_store(t1=("anthropic-only",))
    g, sink = _guardrail(tmp_path, team_store)

    data = _data(model="gpt-4o")  # → openai, not in allowed set
    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(data)
    assert ei.value.status_code == 403
    assert ei.value.error_code == "E_PROVIDER_BLOCKED"
    assert len(sink.records) == 1
    rec = sink.records[0]
    assert rec.get("block_reason") == "provider:not_allowed"
    assert rec.get("error_code") == "E_PROVIDER_BLOCKED"
    assert rec.get("profile_ids") == ["anthropic-only"]


async def test_pre_call_provider_gate_allows_permitted_provider(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "anthropic-only",
        'name = "anthropic-only"\ndetectors = ["regex_checksum"]\n'
        '[policy]\nallowed_providers = ["anthropic"]\n',
    )
    team_store = await _team_store(t1=("anthropic-only",))
    g, _ = _guardrail(tmp_path, team_store)

    out = await g.pre_call(_data(model="claude-3-5-sonnet"))  # → anthropic, allowed
    assert out["messages"][0]["content"] == "hello"


async def test_pre_call_stage0_block_payloads_from_policy(tmp_path: Path) -> None:
    """Global classifier OFF, profile policy block_payloads ON → still blocks."""
    _write_profile(
        tmp_path,
        "blocker",
        'name = "blocker"\ndetectors = ["regex_checksum"]\n[policy]\nblock_payloads = true\n',
    )
    team_store = await _team_store(t1=("blocker",))
    os.environ["CORP_LLM_BLOCK_PAYLOADS"] = "0"
    _cfg_module.reset_cache()
    try:
        g, _ = _guardrail(tmp_path, team_store)
        with pytest.raises(GuardrailHttpException) as ei:
            await g.pre_call(_data(content=_CONFIG_PAYLOAD))
        assert ei.value.status_code == 422
        assert ei.value.error_code == "E_POLICY_BLOCKED"
    finally:
        del os.environ["CORP_LLM_BLOCK_PAYLOADS"]
        _cfg_module.reset_cache()


async def test_pre_call_stage0_no_policy_and_global_off_allows(tmp_path: Path) -> None:
    """Control: global OFF + empty profile → the same payload passes through."""
    team_store = await _team_store(t1=())
    os.environ["CORP_LLM_BLOCK_PAYLOADS"] = "0"
    _cfg_module.reset_cache()
    try:
        g, _ = _guardrail(tmp_path, team_store)
        out = await g.pre_call(_data(content=_CONFIG_PAYLOAD))
        assert out["messages"][0]["content"] == _CONFIG_PAYLOAD
    finally:
        del os.environ["CORP_LLM_BLOCK_PAYLOADS"]
        _cfg_module.reset_cache()


async def test_pre_call_stage5_canary_from_policy(tmp_path: Path) -> None:
    canary = "POLICY-CANARY-XYZ"
    _write_profile(
        tmp_path,
        "canary",
        'name = "canary"\ndetectors = ["regex_checksum"]\n'
        f'[policy]\ncanary_patterns = ["{canary}"]\n',
    )
    team_store = await _team_store(t1=("canary",))
    g, sink = _guardrail(tmp_path, team_store)  # default base guard: no canaries

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(_data(content=f"here is {canary} token"))
    assert ei.value.status_code == 422
    assert ei.value.error_code == "E_DLP_BLOCKED"
    rec = sink.records[0]
    assert rec.get("block_reason") == "dlp:canary"
    assert canary not in json.dumps(rec), "raw canary must never appear in the audit record"


async def test_pre_call_stage5_no_policy_canary_passes(tmp_path: Path) -> None:
    """Control: empty profile → the same canary string is not a policy canary."""
    team_store = await _team_store(t1=())
    g, _ = _guardrail(tmp_path, team_store)
    out = await g.pre_call(_data(content="here is POLICY-CANARY-XYZ token"))
    assert "POLICY-CANARY-XYZ" in out["messages"][0]["content"]


async def test_pre_call_empty_profile_backcompat(tmp_path: Path) -> None:
    team_store = await _team_store(t1=())
    corp = _corp_llm_returning([("alice", "[NAME_001]")])
    g, sink = _guardrail(tmp_path, team_store, corp_llm=corp)

    out = await g.pre_call(_data(content="hello alice"))
    assert out["messages"][0]["content"] == "hello [NAME_001]"

    now = datetime.now(UTC)
    await g.audit(out, None, start_time=now, end_time=now, status="ok")
    assert len(sink.records) == 1
    assert "profile_ids" not in sink.records[0], "no profile applied → no profile_ids in audit"


async def test_pre_call_misconfigured_profile_fails_closed(tmp_path: Path) -> None:
    team_store = await _team_store(t1=("ghost",))  # ghost never written
    g, sink = _guardrail(tmp_path, team_store)

    with pytest.raises(GuardrailHttpException) as ei:
        await g.pre_call(_data(content="hello"))
    assert ei.value.status_code == 503
    assert ei.value.error_code == "E_PROFILE_UNAVAILABLE"
    assert sink.records[0].get("status") == "failed"
    assert sink.records[0].get("error_code") == "E_PROFILE_UNAVAILABLE"
