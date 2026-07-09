"""Boundary / error-path / config-edge hardening for the B1 composition root.

Complements tests/test_bootstrap.py (happy paths). Focus areas:
- required-but-missing config fails FAST at build, never lazily mid-request;
- Postgres/Redis construction performs no blocking I/O even for a bad/unreachable target;
- mixed & conflicting backend config selects the right stores;
- empty-string / decoy config values are handled or ignored;
- the demo shim's chosen backends are isolated from prod backend env;
- DLP canary parsing + the always-on secret rescan invariant.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from corp_llm_gateway import bootstrap, config
from corp_llm_gateway.corp_llm import CorpLlmClient
from corp_llm_gateway.storage import InMemoryMappingStore, RedisMappingStore
from corp_llm_gateway.team_config import InMemoryTeamConfigStore, PostgresTeamConfigStore
from corp_llm_gateway.tokens import InMemoryTokenStore, InvalidTokenError


@pytest.fixture(autouse=True)
def _clean_config(hermetic_gateway_config: None):
    """Resolve config hermetically for every test here (see tests/conftest.py)."""


@pytest.fixture
def _restore_pkg_logger():
    # Importing the demo module mutates the package logger (adds a handler,
    # sets propagate=False). Restore so other tests' caplog is unaffected.
    import logging

    pkg = logging.getLogger("corp_llm_gateway")
    propagate, handlers = pkg.propagate, list(pkg.handlers)
    yield
    pkg.propagate = propagate
    pkg.handlers = handlers


# ── required-but-missing config → fail FAST at build (not lazily) ─────────────


def test_bearer_provider_without_token_fails_fast_at_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # bearer auth needs CORP_LLM_BEARER_TOKEN; missing it must abort at
    # construction, not on the first upstream request.
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "bearer")
    config.reset_cache()

    with pytest.raises(RuntimeError, match="CORP_LLM_BEARER_TOKEN"):
        bootstrap.build_guardrail()


def test_unknown_auth_provider_fails_fast_at_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "totally-made-up")
    config.reset_cache()

    with pytest.raises(ValueError, match="CORP_LLM_AUTH_PROVIDER"):
        bootstrap.build_guardrail()


def test_oidc_provider_missing_subkeys_fails_fast_at_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "oidc")
    config.reset_cache()

    with pytest.raises(RuntimeError, match="CORP_LLM_OIDC_ISSUER"):
        bootstrap.build_guardrail()


# ── Postgres / Redis: no blocking I/O at construction ────────────────────────


def test_pg_dsn_token_store_selected_or_fastfails_without_asyncpg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unreachable host: construction must never try to connect to it.
    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://gw:gw@10.255.255.1:5432/gw")
    config.reset_cache()

    try:
        import asyncpg  # noqa: F401
    except ImportError:
        # asyncpg absent (Python 3.14 .venv): a configured DSN must fail FAST
        # at build, not lazily on the first token lookup.
        with pytest.raises(RuntimeError, match="asyncpg"):
            bootstrap.build_guardrail()
    else:
        # asyncpg present (Python 3.12 .venv-bench): the Postgres store is
        # selected and built WITHOUT connecting to the unreachable host.
        from corp_llm_gateway.tokens import PostgresTokenStore

        guardrail = bootstrap.build_guardrail()
        assert isinstance(guardrail._auth._store, PostgresTokenStore)


def test_team_config_store_postgres_constructs_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unreachable host; the call must return immediately (no connect attempt).
    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://gw:gw@10.255.255.1:5432/gw")
    config.reset_cache()

    store = bootstrap.build_team_config_store()

    assert isinstance(store, PostgresTeamConfigStore)


def test_mapping_store_redis_constructs_without_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `redis.asyncio.from_url` builds a client but must not open a connection;
    # an unroutable host would hang/raise if it did.
    monkeypatch.setenv("REDIS_URL", "redis://10.255.255.1:6379/0")
    config.reset_cache()

    store = bootstrap.build_mapping_store()

    assert isinstance(store, RedisMappingStore)


# ── partial / conflicting config → correct mixed backend selection ───────────


def test_redis_set_postgres_absent_selects_mixed_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://cache.corp.lan:6379/0")
    config.reset_cache()

    guardrail = bootstrap.build_guardrail()

    # _orch is the ProfileAwareOrchestrator wrapper; the core carries the backend.
    assert isinstance(guardrail._orch._core._mapping_store, RedisMappingStore)
    assert isinstance(guardrail._auth._store, InMemoryTokenStore)
    assert isinstance(bootstrap.build_team_config_store(), InMemoryTeamConfigStore)


def test_postgres_set_redis_absent_selects_mixed_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://gw:gw@pg:5432/gw")
    config.reset_cache()

    # Team store (a stub, asyncpg-free) → Postgres; mapping store → in-memory.
    assert isinstance(bootstrap.build_team_config_store(), PostgresTeamConfigStore)
    assert isinstance(bootstrap.build_mapping_store(), InMemoryMappingStore)


# ── empty-string / edge config values ────────────────────────────────────────


def test_empty_string_backend_config_falls_back_to_in_memory_and_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Empty strings (a common "unset via export FOO=" footgun) must be treated
    # as unset, not as a zero-length URL / endpoint.
    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setenv("CORP_LLM_PG_DSN", "")
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "")
    monkeypatch.setenv("CORP_LLM_MODEL", "")
    config.reset_cache()

    guardrail = bootstrap.build_guardrail()

    assert isinstance(guardrail._orch._core._mapping_store, InMemoryMappingStore)
    assert isinstance(guardrail._auth._store, InMemoryTokenStore)
    assert isinstance(bootstrap.build_team_config_store(), InMemoryTeamConfigStore)
    # Empty endpoint/model don't produce a broken client.
    assert isinstance(bootstrap.build_corp_llm_client(), CorpLlmClient)


@pytest.mark.parametrize(
    ("value", "local_enabled"),
    [
        ("0", False),
        ("false", False),
        ("False", False),
        ("1", True),
        ("", True),  # empty → default "1" (on)
        ("yes", True),
    ],
)
def test_local_first_flag_toggles_local_detectors(
    monkeypatch: pytest.MonkeyPatch, value: str, local_enabled: bool
) -> None:
    monkeypatch.setenv("CORP_LLM_LOCAL_FIRST", value)
    config.reset_cache()

    guardrail = bootstrap.build_guardrail()

    assert (guardrail._orch._core._local is not None) is local_enabled


def test_gazetteer_flag_off_disables_gazetteer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_GAZETTEER", "0")
    config.reset_cache()

    assert bootstrap.build_guardrail()._orch._core._gazetteer is None


# ── config source: no direct os.environ, decoys ignored ──────────────────────


def test_decoy_env_under_wrong_keys_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # Canonical keys are unset; plausible-but-wrong aliases hold live-looking
    # values. A composition root that reads only the canonical names must pick
    # in-memory for both stores.
    monkeypatch.setenv("DATABASE_URL", "postgresql://decoy@db.evil:5432/x")
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://decoy@db.evil:5432/x")
    monkeypatch.setenv("CORP_LLM_DSN", "postgresql://decoy@db.evil:5432/x")
    monkeypatch.setenv("CORP_LLM_REDIS_URL", "redis://decoy.evil:6379/0")
    monkeypatch.setenv("CORP_LLM_CACHE_URL", "redis://decoy.evil:6379/0")
    config.reset_cache()

    assert isinstance(bootstrap.build_mapping_store(), InMemoryMappingStore)
    assert isinstance(bootstrap.build_team_config_store(), InMemoryTeamConfigStore)


def test_config_file_values_win_over_decoy_env_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Authoritative values live ONLY in the TOML file under the canonical keys;
    # env holds decoys under wrong keys. The file-sourced values must win.
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'REDIS_URL = "redis://cache.corp.lan:6379/0"\n'
        'CORP_LLM_PG_DSN = "postgresql://gw:gw@pg:5432/gw"\n'
    )
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("DATABASE_URL", "postgresql://decoy@db.evil:5432/x")
    monkeypatch.setenv("CORP_LLM_REDIS_URL", "redis://decoy.evil:6379/0")
    config.reset_cache()

    assert isinstance(bootstrap.build_mapping_store(), RedisMappingStore)
    assert isinstance(bootstrap.build_team_config_store(), PostgresTeamConfigStore)


# ── demo shim isolated from prod config ──────────────────────────────────────


@pytest.mark.usefixtures("_restore_pkg_logger")
def test_demo_backends_ignore_prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Import under the cleaned env, THEN set prod backend env: the demo passes
    # explicit in-memory overrides, so prod REDIS_URL / PG DSN must not change
    # the demo's chosen stores.
    from corp_llm_gateway import _demo_guardrail

    monkeypatch.setenv("REDIS_URL", "redis://prod-cache:6379/0")
    monkeypatch.setenv("CORP_LLM_PG_DSN", "postgresql://prod@pg:5432/gw")
    config.reset_cache()

    guardrail = _demo_guardrail._build_demo_guardrail()

    assert isinstance(guardrail._orch._core._mapping_store, InMemoryMappingStore)
    assert isinstance(guardrail._auth._store, InMemoryTokenStore)


@pytest.mark.usefixtures("_restore_pkg_logger")
async def test_demo_seeded_token_absent_from_prod_guardrail() -> None:
    # The demo seeds its token into its OWN in-memory store; a fresh prod
    # guardrail (in-memory, unseeded) must not recognise it.
    from corp_llm_gateway import _demo_guardrail

    demo_ctx = await _demo_guardrail.guardrail._auth.authenticate("demo-team-token")
    assert demo_ctx.team_id == "demo-team"

    prod = bootstrap.build_guardrail()
    with pytest.raises(InvalidTokenError):
        await prod._auth.authenticate("demo-team-token")


# ── DLP egress guard config edges + invariant ────────────────────────────────


def test_dlp_canaries_parsed_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # Comma-separated, whitespace trimmed, empty entries dropped.
    monkeypatch.setenv("CORP_LLM_DLP_CANARIES", "CANARY-42,  , XYZZY ")
    config.reset_cache()

    guard = bootstrap.build_guardrail()._dlp_guard

    assert guard.scan("prefix CANARY-42 suffix") == "dlp:canary"
    assert guard.scan("prefix XYZZY suffix") == "dlp:canary"
    assert guard.scan("nothing to see here") is None


def test_dlp_secret_rescan_stays_on_without_canaries() -> None:
    # Invariant: even with zero configured canaries the Stage-5 guard still
    # rescans for raw secrets. A future edit must not silently disable this.
    guard = bootstrap.build_guardrail()._dlp_guard

    assert guard.scan("token sk-" + "a" * 40 + " end") == "dlp:secret_leak"


def test_malformed_canary_regex_fails_fast_at_build(monkeypatch: pytest.MonkeyPatch) -> None:
    # Canaries are compiled as regex at construction, so a malformed pattern
    # surfaces at build time — not lazily during a request scan.
    monkeypatch.setenv("CORP_LLM_DLP_CANARIES", "ok-canary,[unterminated")
    config.reset_cache()

    with pytest.raises(re.error):
        bootstrap.build_guardrail()


# ── corp-LLM endpoint placeholder warning ────────────────────────────────────


@pytest.mark.usefixtures("_restore_pkg_logger")
def test_endpoint_placeholder_default_warns(caplog: pytest.LogCaptureFixture) -> None:
    # CORP_LLM_ENDPOINT unset → the client falls back to the placeholder; a
    # WARNING at build time makes the misconfiguration diagnosable in prod logs
    # (the hard fail-fast lives in startup validation, not here).
    logging.getLogger("corp_llm_gateway").propagate = True
    with caplog.at_level(logging.WARNING, logger="corp_llm_gateway.bootstrap"):
        bootstrap.build_corp_llm_client()

    assert any(
        "CORP_LLM_ENDPOINT" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    )


@pytest.mark.usefixtures("_restore_pkg_logger")
def test_endpoint_set_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logging.getLogger("corp_llm_gateway").propagate = True
    monkeypatch.setenv("CORP_LLM_ENDPOINT", "https://corp-llm.corp.lan/v1")
    config.reset_cache()
    with caplog.at_level(logging.WARNING, logger="corp_llm_gateway.bootstrap"):
        bootstrap.build_corp_llm_client()

    assert not any("CORP_LLM_ENDPOINT" in r.getMessage() for r in caplog.records)
