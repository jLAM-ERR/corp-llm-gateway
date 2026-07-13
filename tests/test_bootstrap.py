from __future__ import annotations

import ast
import importlib
import logging
import sys
from pathlib import Path

import pytest

from corp_llm_gateway import bootstrap, config
from corp_llm_gateway.audit import StdoutSink
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
from corp_llm_gateway.metrics import MetricsExporter, NoopExporter
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.profile_orchestrator import ProfileAwareOrchestrator
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
