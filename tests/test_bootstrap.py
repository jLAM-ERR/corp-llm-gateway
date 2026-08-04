from __future__ import annotations

import ast
import importlib
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from corp_llm_gateway import bootstrap, config, settings
from corp_llm_gateway.audit import StdoutSink
from corp_llm_gateway.extensions import EXTENSION_API_VERSION, REGISTRY
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
from corp_llm_gateway.metrics import MetricsExporter, NoopExporter
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.profile_orchestrator import ProfileAwareOrchestrator
from corp_llm_gateway.settings import ConfigError
from corp_llm_gateway.storage import InMemoryMappingStore, RedisMappingStore
from corp_llm_gateway.team_config import (
    InMemoryTeamConfigStore,
    PostgresTeamConfigStore,
    TeamConfig,
)
from corp_llm_gateway.tokens import InMemoryTokenStore, InvalidTokenError, MissingTokenError


@pytest.fixture(autouse=True)
def _clean_config(hermetic_gateway_config: None) -> None:
    """Resolve config hermetically for every test here (see tests/conftest.py)."""


# ── build_guardrail: defaults ────────────────────────────────────────────────


def test_build_guardrail_returns_guardrail_with_in_memory_backends() -> None:
    guardrail = bootstrap.build_guardrail()

    assert isinstance(guardrail, CorpLlmGuardrail)
    assert isinstance(guardrail._auth._store, InMemoryTokenStore)
    # _orch is now the ProfileAwareOrchestrator wrapper; the core carries the store.
    assert isinstance(guardrail._orch._core._mapping_store, InMemoryMappingStore)


def test_module_level_guardrail_is_importable_instance() -> None:
    # LiteLLM `callbacks:` imports `corp_llm_gateway.bootstrap.guardrail`.
    assert isinstance(bootstrap.guardrail, CorpLlmGuardrail)


def test_importing_module_does_not_build_guardrail() -> None:
    # A fresh import (simulated via reload) must leave the guardrail unbuilt;
    # construction is deferred to first attribute access (PEP 562 __getattr__).
    reloaded = importlib.reload(bootstrap)

    assert reloaded._guardrail is None
    assert "guardrail" not in vars(reloaded)


def test_guardrail_attribute_builds_once_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    real = bootstrap.build_guardrail

    def counting(*args: object, **kwargs: object) -> CorpLlmGuardrail:
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(bootstrap, "_guardrail", None)
    monkeypatch.setattr(bootstrap, "build_guardrail", counting)

    assert calls == 0  # patching / importing alone builds nothing
    first = bootstrap.guardrail
    assert calls == 1  # first access is the single build
    assert first is bootstrap.guardrail  # cached: second access does not rebuild
    assert calls == 1
    assert isinstance(first, CorpLlmGuardrail)


def test_getattr_raises_for_unknown_attribute() -> None:
    with pytest.raises(AttributeError):
        bootstrap.does_not_exist  # noqa: B018


def test_gateway_version_is_metadata_not_demo_string() -> None:
    guardrail = bootstrap.build_guardrail()

    assert guardrail._audit._gateway_version == bootstrap.gateway_version()
    assert guardrail._audit._gateway_version != "demo"


def test_audit_sink_is_stdout() -> None:
    guardrail = bootstrap.build_guardrail()

    assert isinstance(guardrail._audit._sink, StdoutSink)


# ── B4: metrics exporter wired (default noop) ────────────────────────────────


def test_build_guardrail_carries_metrics_exporter_noop_by_default() -> None:
    # B4 follow-up: build_guardrail wires get_exporter(); default is noop, so
    # nothing is emitted (zero behavior change) but the seam is live.
    guardrail = bootstrap.build_guardrail()

    assert isinstance(guardrail._metrics, MetricsExporter)
    assert isinstance(guardrail._metrics, NoopExporter)


# ── D4: profiles activated in the composition root ───────────────────────────


def test_build_guardrail_wraps_orchestrator_in_profile_aware() -> None:
    # D4 follow-up: _orch is the ProfileAwareOrchestrator wrapping the core.
    guardrail = bootstrap.build_guardrail()

    assert isinstance(guardrail._orch, ProfileAwareOrchestrator)
    assert isinstance(guardrail._orch._core, SanitizationOrchestrator)


async def test_no_profile_team_passes_through_to_core_unchanged() -> None:
    # Back-compat: a team with no profile_ids resolves to the CORE orchestrator
    # with no fingerprint — byte-identical to pre-D4 behavior.
    guardrail = bootstrap.build_guardrail()

    resolved = await guardrail._orch.resolve("team-with-no-profiles")

    assert resolved.orchestrator is guardrail._orch._core
    assert resolved.fingerprint is None
    assert resolved.profile_ids == ()


async def test_team_with_sealed_default_profile_resolves_and_applies() -> None:
    # A team selecting the shipped division-x bundle composes [core, ru-152fz,
    # division-x] and applies its tightened merged policy through a DISTINCT inner
    # orchestrator — proving profiles are live end-to-end from build_guardrail.
    team_store = InMemoryTeamConfigStore()
    await team_store.upsert(
        TeamConfig(team_id="div-team", name="Division X", profile_ids=("division-x",))
    )
    guardrail = bootstrap.build_guardrail(team_config_store=team_store)

    resolved = await guardrail._orch.resolve("div-team")

    assert resolved.profile_ids == ("core", "ru-152fz", "division-x")
    assert resolved.fingerprint is not None
    assert resolved.orchestrator is not guardrail._orch._core
    # division-x tightens the merged policy — proves the bundle is applied + merged.
    assert resolved.policy.allowed_providers == frozenset({"anthropic"})
    assert resolved.policy.size_threshold_bytes == 65536


# ── CORP_LLM_ORACLE_ENABLED: local mode boots without corp-LLM (Task 3) ──────


def test_oracle_disabled_build_guardrail_skips_client_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No CORP_LLM_ENDPOINT set (hermetic_gateway_config clears it) — must not be
    # required when the oracle is off.
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", "0")

    def _fail_if_called() -> None:
        raise AssertionError("build_corp_llm_client must not run when the oracle is disabled")

    monkeypatch.setattr(bootstrap, "build_corp_llm_client", _fail_if_called)

    guardrail = bootstrap.build_guardrail()

    core = guardrail._orch._core
    assert core._corp_llm is None
    assert core._oracle_enabled is False


def test_oracle_disabled_logs_one_info_at_build_time(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", "0")
    caplog.set_level(logging.INFO, logger="corp_llm_gateway.bootstrap")

    bootstrap.build_guardrail()

    assert "oracle_enabled=false" in caplog.text


async def test_oracle_disabled_profiled_team_inner_orchestrator_has_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A team WITH profile_ids (ProfileAwareOrchestrator inner path) in local
    mode: the inner orchestrator is also client-less; sanitization still runs
    via the profile's own gazetteer (division-x extends core's markings.txt)."""
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", "0")
    team_store = InMemoryTeamConfigStore()
    await team_store.upsert(
        TeamConfig(team_id="div-team", name="Division X", profile_ids=("division-x",))
    )
    guardrail = bootstrap.build_guardrail(team_config_store=team_store)

    resolved = await guardrail._orch.resolve("div-team")

    assert resolved.orchestrator is not guardrail._orch._core
    assert resolved.orchestrator._corp_llm is None
    assert resolved.orchestrator._oracle_enabled is False

    result = await resolved.sanitize(
        "Marked Confidential — internal review only.",
        team_id="div-team",
        conversation_id="c1",
    )
    assert "Confidential" not in result.sanitized_text


# ── flag parsing must not fork between settings.validate() and bootstrap ────


@pytest.mark.parametrize("falsy", ["off", "no", "0", "false", "OFF"])
def test_oracle_falsy_spellings_disable_oracle_without_endpoint(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    # Regression: settings._as_flag treats off/no/0/false (any case) as falsy;
    # bootstrap._flag must agree, or CORP_LLM_ORACLE_ENABLED=off passes
    # validation as disabled but build_guardrail still builds an oracle client.
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", falsy)

    def _fail_if_called() -> None:
        raise AssertionError("build_corp_llm_client must not run when the oracle is disabled")

    monkeypatch.setattr(bootstrap, "build_corp_llm_client", _fail_if_called)

    guardrail = bootstrap.build_guardrail()

    core = guardrail._orch._core
    assert core._corp_llm is None
    assert core._oracle_enabled is False


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
def test_oracle_truthy_spellings_enable_oracle_and_build_client(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", truthy)
    calls = 0
    real = bootstrap.build_corp_llm_client

    def counting() -> object:
        nonlocal calls
        calls += 1
        return real()

    monkeypatch.setattr(bootstrap, "build_corp_llm_client", counting)

    guardrail = bootstrap.build_guardrail()

    assert calls == 1, "the oracle client must be built when the flag is truthy"
    assert guardrail._orch._core._oracle_enabled is True


# ── forward_chatgpt_auth: build_guardrail must resolve CORP_LLM_FORWARD_CHATGPT_AUTH
# from config, not silently default to off (a real k8s deploy never passes the
# kwarg explicitly, so the env var must be the thing that flips it) ─────────


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
def test_forward_chatgpt_auth_truthy_spellings_enable_the_flag(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", truthy)

    guardrail = bootstrap.build_guardrail()

    assert guardrail._forward_chatgpt_auth is True


@pytest.mark.parametrize("falsy", ["0", "off", "no", "false", "OFF"])
def test_forward_chatgpt_auth_falsy_spellings_disable_the_flag(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", falsy)

    guardrail = bootstrap.build_guardrail()

    assert guardrail._forward_chatgpt_auth is False


def test_forward_chatgpt_auth_unset_defaults_off() -> None:
    guardrail = bootstrap.build_guardrail()

    assert guardrail._forward_chatgpt_auth is False


def test_forward_chatgpt_auth_explicit_argument_overrides_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An explicit caller argument (e.g. the demo shim resolving its own copy)
    # must win over whatever the env var says.
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", "1")

    guardrail = bootstrap.build_guardrail(forward_chatgpt_auth=False)

    assert guardrail._forward_chatgpt_auth is False


# ── strip_inbound_headers_to_upstream: build_guardrail must resolve
# CORP_LLM_STRIP_INBOUND_HEADERS from config (compose's hosted_vllm/ route needs
# this True or the corp ingress 503s on the forwarded Host header) ─────────────


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
def test_strip_inbound_headers_truthy_spellings_enable_the_flag(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    monkeypatch.setenv("CORP_LLM_STRIP_INBOUND_HEADERS", truthy)

    guardrail = bootstrap.build_guardrail()

    assert guardrail._strip_inbound_headers_to_upstream is True


@pytest.mark.parametrize("falsy", ["0", "off", "no", "false", "OFF"])
def test_strip_inbound_headers_falsy_spellings_disable_the_flag(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    monkeypatch.setenv("CORP_LLM_STRIP_INBOUND_HEADERS", falsy)

    guardrail = bootstrap.build_guardrail()

    assert guardrail._strip_inbound_headers_to_upstream is False


def test_strip_inbound_headers_unset_defaults_off() -> None:
    guardrail = bootstrap.build_guardrail()

    assert guardrail._strip_inbound_headers_to_upstream is False


def test_strip_inbound_headers_explicit_argument_overrides_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An explicit caller argument (e.g. the demo shim resolving its own copy)
    # must win over whatever the env var says.
    monkeypatch.setenv("CORP_LLM_STRIP_INBOUND_HEADERS", "1")

    guardrail = bootstrap.build_guardrail(strip_inbound_headers_to_upstream=False)

    assert guardrail._strip_inbound_headers_to_upstream is False


# ── forward_anthropic_auth: same config-resolution contract as the Codex flag ─


@pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
def test_forward_anthropic_auth_truthy_spellings_enable_the_flag(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", truthy)

    guardrail = bootstrap.build_guardrail()

    assert guardrail._forward_anthropic_auth is True


@pytest.mark.parametrize("falsy", ["0", "off", "no", "false", "OFF"])
def test_forward_anthropic_auth_falsy_spellings_disable_the_flag(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", falsy)

    guardrail = bootstrap.build_guardrail()

    assert guardrail._forward_anthropic_auth is False


def test_forward_anthropic_auth_unset_defaults_off() -> None:
    # Backward compatibility: an existing deploy that sets neither key keeps
    # today's behavior exactly.
    guardrail = bootstrap.build_guardrail()

    assert guardrail._forward_anthropic_auth is False
    assert guardrail._forward_chatgpt_auth is False


def test_forward_anthropic_auth_explicit_argument_overrides_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "1")

    assert bootstrap.build_guardrail(forward_anthropic_auth=False)._forward_anthropic_auth is False

    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "0")

    assert bootstrap.build_guardrail(forward_anthropic_auth=True)._forward_anthropic_auth is True


# ── flag exclusivity: enforced at RUNTIME, not only in `config check` (the
# compose/demo/bare-litellm boots this bridge ships on never call
# settings.validate()) ───────────────────────────────────────────────────────


def test_both_forward_auth_flags_raise_from_build_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", "1")
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "1")

    with pytest.raises(ConfigError) as exc_info:
        bootstrap.build_guardrail()

    assert settings.FORWARD_AUTH_EXCLUSIVE_MESSAGE in exc_info.value.problems


@pytest.mark.parametrize("truthy", ["true", "yes", "on"])
def test_both_forward_auth_flags_lenient_spellings_raise_from_build_guardrail(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", truthy)
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", truthy)

    with pytest.raises(ConfigError, match="mutually"):
        bootstrap.build_guardrail()


def test_both_forward_auth_flags_raise_when_one_comes_from_a_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The conflict is checked on the RESOLVED pair, so a caller-supplied kwarg
    # cannot smuggle the second bridge past the rule.
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", "1")

    with pytest.raises(ConfigError, match="mutually"):
        bootstrap.build_guardrail(forward_anthropic_auth=True)


def test_explicit_kwarg_off_takes_precedence_over_a_conflicting_env_pair(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Documented precedence, not an oversight: the kwarg resolves first, so the
    # pair the rule sees is legal and exactly one bridge ends up live. The env
    # pair is still broken (`config check` rejects it), so the divergence is
    # logged rather than silent.
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", "1")
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "1")

    with caplog.at_level(logging.WARNING, logger="corp_llm_gateway.bootstrap"):
        guardrail = bootstrap.build_guardrail(forward_chatgpt_auth=False)

    assert guardrail._forward_chatgpt_auth is False
    assert guardrail._forward_anthropic_auth is True
    warning = next(
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING and "CORP_LLM_FORWARD_CHATGPT_AUTH" in record.message
    )
    assert "CORP_LLM_FORWARD_ANTHROPIC_AUTH" in warning
    assert "disarmed CORP_LLM_FORWARD_CHATGPT_AUTH" in warning
    assert "config check" in warning


def test_no_disarm_warning_when_the_env_pair_is_already_legal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", "1")
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "0")

    with caplog.at_level(logging.WARNING, logger="corp_llm_gateway.bootstrap"):
        bootstrap.build_guardrail(forward_chatgpt_auth=False)

    assert not [r for r in caplog.records if "mutually exclusive" in r.getMessage()]


# ── a litellm master key cancels either bridge: same runtime enforcement, because
# docker-compose's `env_file:` is a developer-owned file no `config check` sees ──


def test_master_key_next_to_a_bridge_raises_from_build_guardrail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-key-fixture")

    with pytest.raises(ConfigError) as exc_info:
        bootstrap.build_guardrail()

    assert settings.MASTER_KEY_VS_FORWARD_AUTH_MESSAGE in exc_info.value.problems
    assert "master-key-fixture" not in str(exc_info.value)


def test_master_key_is_checked_against_the_resolved_flag_not_the_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The demo shim passes its own resolved flags, so the rule must see those —
    # a kwarg that turns the bridge on must not slip past an env-only check.
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-key-fixture")

    with pytest.raises(ConfigError, match="LITELLM_MASTER_KEY"):
        bootstrap.build_guardrail(forward_anthropic_auth=True)

    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "1")

    assert bootstrap.build_guardrail(forward_anthropic_auth=False) is not None


def test_master_key_alone_still_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-key-fixture")

    guardrail = bootstrap.build_guardrail()

    assert guardrail._forward_anthropic_auth is False
    assert guardrail._forward_chatgpt_auth is False


def test_master_key_refusal_precedes_any_component_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A misconfigured stack must name the master key, not die first on some
    # unrelated backend the build should never have started.
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "1")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-key-fixture")
    monkeypatch.setattr(
        bootstrap,
        "build_corp_llm_client",
        lambda: pytest.fail("built the corp-LLM client despite an invalid config"),
    )
    monkeypatch.setattr(
        bootstrap,
        "build_mapping_store",
        lambda: pytest.fail("built the mapping store despite an invalid config"),
    )

    with pytest.raises(ConfigError, match="LITELLM_MASTER_KEY"):
        bootstrap.build_guardrail()


@pytest.mark.parametrize(
    "chatgpt,anthropic",
    [("1", "0"), ("0", "1"), ("0", "0")],
)
def test_one_forward_auth_flag_at_a_time_builds_fine(
    monkeypatch: pytest.MonkeyPatch, chatgpt: str, anthropic: str
) -> None:
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", chatgpt)
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", anthropic)

    guardrail = bootstrap.build_guardrail()

    assert guardrail._forward_chatgpt_auth is (chatgpt == "1")
    assert guardrail._forward_anthropic_auth is (anthropic == "1")


# ── no-op-sanitizer floor: build_guardrail must not skip this even though it
# never calls settings.validate() (Helm's config-check initContainer does; this
# covers compose/demo/bare-litellm boots) ────────────────────────────────────


@pytest.mark.parametrize("local_first_off", ["0", "off"])
def test_oracle_off_and_local_first_off_raises_config_error_naming_both_keys(
    monkeypatch: pytest.MonkeyPatch, local_first_off: str
) -> None:
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", "0")
    monkeypatch.setenv("CORP_LLM_LOCAL_FIRST", local_first_off)

    with pytest.raises(ConfigError) as exc_info:
        bootstrap.build_guardrail()

    message = str(exc_info.value)
    assert "CORP_LLM_ORACLE_ENABLED" in message
    assert "CORP_LLM_LOCAL_FIRST" in message


def test_oracle_off_and_local_first_default_on_builds_fine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # CORP_LLM_LOCAL_FIRST defaults to on; oracle-off alone must not trip the guard.
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", "0")

    guardrail = bootstrap.build_guardrail()

    assert isinstance(guardrail, CorpLlmGuardrail)


def test_oracle_off_and_local_first_off_raises_even_with_corp_llm_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A caller-supplied client does not restore the missing local-first floor —
    # the guard must fire regardless of the `corp_llm` DI override.
    monkeypatch.setenv("CORP_LLM_ORACLE_ENABLED", "0")
    monkeypatch.setenv("CORP_LLM_LOCAL_FIRST", "0")

    with pytest.raises(ConfigError):
        bootstrap.build_guardrail(corp_llm=object())


def test_oracle_default_on_and_local_first_off_builds_fine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Oracle backstop (default-on) covers the missing local-first floor — unchanged.
    monkeypatch.setenv("CORP_LLM_LOCAL_FIRST", "0")

    guardrail = bootstrap.build_guardrail()

    assert isinstance(guardrail, CorpLlmGuardrail)


# ── backend selection by config ──────────────────────────────────────────────


def test_mapping_store_selects_redis_when_url_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://cache.corp.lan:6379/0")

    # Construction must not connect — assert the type with no live server.
    assert isinstance(bootstrap.build_mapping_store(), RedisMappingStore)


def test_mapping_store_in_memory_when_url_unset() -> None:
    assert isinstance(bootstrap.build_mapping_store(), InMemoryMappingStore)


def test_team_config_store_selects_postgres_when_dsn_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://gw:gw@pg:5432/gw")

    assert isinstance(bootstrap.build_team_config_store(), PostgresTeamConfigStore)


def test_team_config_store_in_memory_when_dsn_unset() -> None:
    assert isinstance(bootstrap.build_team_config_store(), InMemoryTeamConfigStore)


def test_build_guardrail_selects_postgres_token_store(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("asyncpg", reason="PostgresTokenStore requires the 'postgres' extra")
    from corp_llm_gateway.tokens import PostgresTokenStore

    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://gw:gw@pg:5432/gw")

    guardrail = bootstrap.build_guardrail()

    assert isinstance(guardrail._auth._store, PostgresTokenStore)


# ── CORP_LLM_DEV_TEAM_TOKEN: solo-mode auth path (Task 4) ────────────────────


async def test_dev_team_token_seeds_local_dev_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_DEV_TEAM_TOKEN", "solo-dev-token")

    guardrail = bootstrap.build_guardrail()
    ctx = await guardrail._auth.authenticate("solo-dev-token")

    assert ctx.team_id == "local-dev"
    assert isinstance(guardrail._auth._store, InMemoryTokenStore)


async def test_dev_team_token_unset_store_stays_empty() -> None:
    # Byte-identical to pre-Task-4 behavior: no token seeded, nothing to authenticate.
    guardrail = bootstrap.build_guardrail()

    with pytest.raises(MissingTokenError):
        await guardrail._auth.authenticate(None)
    with pytest.raises(InvalidTokenError):
        await guardrail._auth.authenticate("anything")


def test_dev_team_token_ignored_when_pg_dsn_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pytest.importorskip("asyncpg", reason="PostgresTokenStore requires the 'postgres' extra")
    from corp_llm_gateway.tokens import PostgresTokenStore

    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://gw:gw@pg:5432/gw")
    monkeypatch.setenv("CORP_LLM_DEV_TEAM_TOKEN", "solo-dev-token")
    caplog.set_level(logging.WARNING, logger="corp_llm_gateway.tokens.middleware")

    guardrail = bootstrap.build_guardrail()

    assert isinstance(guardrail._auth._store, PostgresTokenStore)
    assert "CORP_LLM_PG_DSN" in caplog.text
    assert "solo-dev-token" not in caplog.text


def test_dev_team_token_ignored_when_corp_env_prod(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("CORP_ENV", "prod")
    monkeypatch.setenv("CORP_LLM_DEV_TEAM_TOKEN", "solo-dev-token")
    caplog.set_level(logging.WARNING, logger="corp_llm_gateway.tokens.middleware")

    guardrail = bootstrap.build_guardrail()

    assert isinstance(guardrail._auth._store, InMemoryTokenStore)
    assert "CORP_ENV" in caplog.text
    assert "solo-dev-token" not in caplog.text


# ── config-only: no os.environ at call sites ─────────────────────────────────


def _assert_no_process_env_reads(path: Path) -> None:
    # AST-level so docstring/comment mentions of os.environ don't false-trip:
    # the module must not import `os` nor touch os.environ/os.getenv in code.
    tree = ast.parse(path.read_text())
    imports_os = any(
        (isinstance(n, ast.Import) and any(a.name == "os" for a in n.names))
        or (isinstance(n, ast.ImportFrom) and n.module == "os")
        for n in ast.walk(tree)
    )
    assert not imports_os, f"{path.name} must resolve settings via config, not import os"
    env_reads = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "os"
        and n.attr in {"environ", "getenv"}
    ]
    assert not env_reads, f"{path.name} must read config, not os.environ/os.getenv"


def test_bootstrap_does_not_read_process_environment() -> None:
    _assert_no_process_env_reads(Path(bootstrap.__file__))


def test_demo_guardrail_does_not_read_process_environment() -> None:
    # Pin the demo shim to the same config-only contract as the prod root.
    _assert_no_process_env_reads(Path(bootstrap.__file__).with_name("_demo_guardrail.py"))


def test_backends_resolve_from_config_file_without_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Proves selection flows through the config loader, not os.environ: the keys
    # live ONLY in the TOML file and env is cleared by the autouse fixture.
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'REDIS_URL = "redis://cache.corp.lan:6379/0"\n'
        'CORP_LLM_PG_DSN = "postgresql://gw:gw@pg:5432/gw"\n'
    )
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    config.reset_cache()

    assert isinstance(bootstrap.build_mapping_store(), RedisMappingStore)
    assert isinstance(bootstrap.build_team_config_store(), PostgresTeamConfigStore)


# ── demo shim ────────────────────────────────────────────────────────────────


@pytest.fixture
def _restore_pkg_logger() -> None:
    # Importing the demo module sets propagate=False + adds a handler on the
    # package logger; restore so caplog in other tests is unaffected.
    pkg = logging.getLogger("corp_llm_gateway")
    propagate, handlers = pkg.propagate, list(pkg.handlers)
    yield
    pkg.propagate = propagate
    pkg.handlers = handlers


@pytest.mark.usefixtures("_restore_pkg_logger")
async def test_demo_shim_yields_in_memory_deps_and_working_guardrail() -> None:
    from corp_llm_gateway import _demo_guardrail

    guardrail = _demo_guardrail.guardrail

    assert isinstance(guardrail, CorpLlmGuardrail)
    assert isinstance(guardrail._auth._store, InMemoryTokenStore)
    assert isinstance(guardrail._orch._core._mapping_store, InMemoryMappingStore)
    assert isinstance(guardrail._audit._sink, StdoutSink)

    ctx = await guardrail._auth.authenticate("demo-team-token")
    assert ctx.team_id == "demo-team"
    assert ctx.user_id == "demo-user"


@pytest.mark.usefixtures("_restore_pkg_logger")
def test_demo_shim_resolves_forward_anthropic_auth_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Lenient spelling on purpose: the demo shim must use settings.parse_flag,
    # not a raw `== "1"`.
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "yes")
    config.reset_cache()
    sys.modules.pop("corp_llm_gateway._demo_guardrail", None)

    module = importlib.import_module("corp_llm_gateway._demo_guardrail")

    assert module.guardrail._forward_anthropic_auth is True
    sys.modules.pop("corp_llm_gateway._demo_guardrail", None)


@pytest.mark.usefixtures("_restore_pkg_logger")
def test_demo_boot_path_raises_when_both_forward_auth_flags_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The demo/compose boot never calls settings.validate(), so the exclusivity
    # rule has to fire from build_guardrail() for this path to be covered.
    monkeypatch.setenv("CORP_LLM_FORWARD_CHATGPT_AUTH", "1")
    monkeypatch.setenv("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "1")
    config.reset_cache()
    sys.modules.pop("corp_llm_gateway._demo_guardrail", None)

    with pytest.raises(ConfigError, match="mutually"):
        importlib.import_module("corp_llm_gateway._demo_guardrail")

    sys.modules.pop("corp_llm_gateway._demo_guardrail", None)


@pytest.mark.usefixtures("_restore_pkg_logger")
def test_importing_demo_guardrail_with_pg_dsn_and_no_asyncpg_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: the demo container sets CORP_LLM_PG_DSN but ships without
    # asyncpg. Importing the demo shim must NOT eagerly trigger bootstrap's prod
    # build (a Postgres token store) — which used to crash at import time.
    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://gw:gw@10.255.255.1:5432/gw")
    config.reset_cache()
    sys.modules.pop("corp_llm_gateway._demo_guardrail", None)

    module = importlib.import_module("corp_llm_gateway._demo_guardrail")

    assert isinstance(module.guardrail, CorpLlmGuardrail)


# ── B4: corp NER wiring (default off) ────────────────────────────────────────


@pytest.fixture
def _isolate_registry() -> Iterator[None]:
    """Snapshot/restore the module-level REGISTRY so a corp-NER registration
    from build_guardrail never leaks into another test's health_all()."""
    specs = dict(REGISTRY._specs)
    factories = dict(REGISTRY._factories)
    try:
        yield
    finally:
        REGISTRY._specs.clear()
        REGISTRY._specs.update(specs)
        REGISTRY._factories.clear()
        REGISTRY._factories.update(factories)


def _enable_corp_ner(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    monkeypatch.setenv("CORP_NER_ENABLED", "1")
    monkeypatch.setenv("CORP_NER_ENDPOINT", "https://corp-ner.test")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def _local_pass(guardrail: CorpLlmGuardrail) -> object:
    return guardrail._orch._core._local


def test_corp_ner_disabled_by_default_changes_nothing() -> None:
    guardrail = bootstrap.build_guardrail()

    detectors = _local_pass(guardrail)._detectors
    assert [type(d).__name__ for d in detectors] == [
        "RegexChecksumDetector",
        "DualNerDetector",
    ]
    assert ("detector", "corp_ner") not in REGISTRY._factories


def test_ner_detectors_never_run_on_code_segments() -> None:
    # code_safe_detectors was a dead parameter: every detector ran on CODE
    # segments. Only the deterministic regex/checksum pass belongs there.
    guardrail = bootstrap.build_guardrail()

    code_detectors = _local_pass(guardrail)._code_detectors
    assert [type(d).__name__ for d in code_detectors] == ["RegexChecksumDetector"]
    # Chunk mode pulls regex out and runs it over the full text, so its CODE list
    # is empty — NOT the default-to-everything fallback.
    assert guardrail._orch._core._chunk_local._code_detectors == []


@pytest.mark.usefixtures("_isolate_registry")
def test_corp_ner_enabled_appends_detector_off_the_code_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_corp_ner(monkeypatch)

    guardrail = bootstrap.build_guardrail()

    names = [type(d).__name__ for d in _local_pass(guardrail)._detectors]
    assert names == ["RegexChecksumDetector", "DualNerDetector", "CorpNerDetector"]
    # Corp NER must never see CODE segments: its regex half fires on code tokens
    # and it would ship source code to an external service.
    assert [type(d).__name__ for d in _local_pass(guardrail)._code_detectors] == [
        "RegexChecksumDetector"
    ]


@pytest.mark.usefixtures("_isolate_registry")
def test_corp_ner_detector_gets_the_live_metrics_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without this, gateway_failure{component="corp_ner"} never fires in prod:
    # CorpNerDetector defaults to a Noop exporter of its own.
    _enable_corp_ner(monkeypatch)

    guardrail = bootstrap.build_guardrail()

    corp_ner = _local_pass(guardrail)._detectors[-1]
    assert corp_ner._metrics is guardrail._metrics


@pytest.mark.usefixtures("_isolate_registry")
def test_corp_ner_registers_a_detector_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_corp_ner(monkeypatch)

    guardrail = bootstrap.build_guardrail()

    ext = REGISTRY.get("detector", "corp_ner")
    assert ext.spec.kind == "detector"
    assert ext.spec.fail_policy == "fail-closed"
    REGISTRY.validate_api_version(EXTENSION_API_VERSION)
    # One HTTP client for the request path and the readiness poll.
    assert ext._http is _local_pass(guardrail)._detectors[-1]._client._http


@pytest.mark.usefixtures("_isolate_registry")
def test_corp_ner_client_reads_its_limits_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_corp_ner(
        monkeypatch,
        CORP_NER_TIMEOUT_S="7",
        CORP_NER_MAX_TEXTS="11",
        CORP_NER_MAX_INPUT_CHARS="1234",
    )

    guardrail = bootstrap.build_guardrail()

    client = _local_pass(guardrail)._detectors[-1]._client
    assert client._base_url == "https://corp-ner.test"
    assert client._timeout == 7.0
    assert client._max_texts == 11
    assert client._max_input_chars == 1234


def test_corp_ner_enabled_without_endpoint_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_NER_ENABLED", "1")

    with pytest.raises(ConfigError, match="CORP_NER_ENDPOINT"):
        bootstrap.build_guardrail()


@pytest.mark.usefixtures("_isolate_registry")
def test_corp_ner_reads_the_extensions_detector_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "with-corp-ner.toml"
    cfg.write_text(
        "[extensions.detector.corp_ner]\n"
        "enabled = true\n"
        'endpoint = "https://from-table.test"\n'
        "max_texts = 9\n"
    )
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    config.reset_cache()

    guardrail = bootstrap.build_guardrail()

    client = _local_pass(guardrail)._detectors[-1]._client
    assert client._base_url == "https://from-table.test"
    assert client._max_texts == 9


@pytest.mark.usefixtures("_isolate_registry")
def test_env_wins_over_the_extensions_detector_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "with-corp-ner.toml"
    cfg.write_text(
        '[extensions.detector.corp_ner]\nenabled = true\nendpoint = "https://from-table.test"\n'
    )
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("CORP_NER_ENDPOINT", "https://from-env.test")
    config.reset_cache()

    guardrail = bootstrap.build_guardrail()

    assert _local_pass(guardrail)._detectors[-1]._client._base_url == "https://from-env.test"


def test_table_disabled_corp_ner_stays_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "corp-ner-off.toml"
    cfg.write_text('[extensions.detector.corp_ner]\nenabled = false\nendpoint = "https://x.test"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    config.reset_cache()

    guardrail = bootstrap.build_guardrail()

    assert len(_local_pass(guardrail)._detectors) == 2
    assert ("detector", "corp_ner") not in REGISTRY._factories
