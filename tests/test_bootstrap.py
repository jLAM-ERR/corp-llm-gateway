from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from corp_llm_gateway import bootstrap, config
from corp_llm_gateway.audit import StdoutSink
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
from corp_llm_gateway.storage import InMemoryMappingStore, RedisMappingStore
from corp_llm_gateway.team_config import InMemoryTeamConfigStore, PostgresTeamConfigStore
from corp_llm_gateway.tokens import InMemoryTokenStore

_BACKEND_ENV = ("CORP_LLM_PG_DSN", "REDIS_URL", "CORP_LLM_ENDPOINT", "DEMO_TEAM_TOKEN")


@pytest.fixture(autouse=True)
def _clean_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _BACKEND_ENV:
        monkeypatch.delenv(name, raising=False)
    config.reset_cache()
    yield
    config.reset_cache()


# ── build_guardrail: defaults ────────────────────────────────────────────────


def test_build_guardrail_returns_guardrail_with_in_memory_backends() -> None:
    guardrail = bootstrap.build_guardrail()

    assert isinstance(guardrail, CorpLlmGuardrail)
    assert isinstance(guardrail._auth._store, InMemoryTokenStore)
    assert isinstance(guardrail._orch._mapping_store, InMemoryMappingStore)


def test_module_level_guardrail_is_importable_instance() -> None:
    # LiteLLM `callbacks:` imports `corp_llm_gateway.bootstrap.guardrail`.
    assert isinstance(bootstrap.guardrail, CorpLlmGuardrail)


def test_gateway_version_is_metadata_not_demo_string() -> None:
    guardrail = bootstrap.build_guardrail()

    assert guardrail._audit._gateway_version == bootstrap.gateway_version()
    assert guardrail._audit._gateway_version != "demo"


def test_audit_sink_is_stdout() -> None:
    guardrail = bootstrap.build_guardrail()

    assert isinstance(guardrail._audit._sink, StdoutSink)


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


# ── config-only: no os.environ at call sites ─────────────────────────────────


def test_bootstrap_does_not_read_process_environment() -> None:
    # AST-level so docstring/comment mentions of os.environ don't false-trip:
    # the module must not import `os` nor touch os.environ/os.getenv in code.
    tree = ast.parse(Path(bootstrap.__file__).read_text())
    imports_os = any(
        (isinstance(n, ast.Import) and any(a.name == "os" for a in n.names))
        or (isinstance(n, ast.ImportFrom) and n.module == "os")
        for n in ast.walk(tree)
    )
    assert not imports_os, "bootstrap must resolve settings via config, not import os"
    env_reads = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id == "os"
        and n.attr in {"environ", "getenv"}
    ]
    assert not env_reads


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
    assert isinstance(guardrail._orch._mapping_store, InMemoryMappingStore)
    assert isinstance(guardrail._audit._sink, StdoutSink)

    ctx = await guardrail._auth.authenticate("demo-team-token")
    assert ctx.team_id == "demo-team"
    assert ctx.user_id == "demo-user"
