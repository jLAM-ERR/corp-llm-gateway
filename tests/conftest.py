from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from corp_llm_gateway import config

# Every config key the composition root reads, plus decoy aliases a naive
# implementation might read instead of the canonical names. Cleared before each
# bootstrap test so process env can't leak into backend selection.
MANAGED_ENV: tuple[str, ...] = (
    "CORP_LLM_PG_DSN",
    "REDIS_URL",
    "CORP_LLM_ENDPOINT",
    "CORP_LLM_MODEL",
    "CORP_LLM_RULES_DIR",
    "CORP_LLM_LOCAL_FIRST",
    "CORP_LLM_GAZETTEER",
    "CORP_LLM_DLP_CANARIES",
    "CORP_LLM_CA_BUNDLE",
    "SSL_VERIFY",
    "CORP_LLM_AUTH_PROVIDER",
    "CORP_LLM_BEARER_TOKEN",
    "CORP_LLM_OIDC_ISSUER",
    "CORP_LLM_OIDC_CLIENT_ID",
    "CORP_LLM_OIDC_CLIENT_SECRET",
    "CORP_AUDIT_SINK",
    "CORP_LANGFUSE_URL",
    "CORP_LANGFUSE_PUBLIC_KEY",
    "CORP_LANGFUSE_SECRET_KEY",
    "DEMO_TEAM_TOKEN",
    "CORP_LLM_DEV_TEAM_TOKEN",
    "CORP_ENV",
    "CORP_LLM_GATEWAY_CONFIG_FILE",
    # decoy aliases (must be ignored):
    "DATABASE_URL",
    "POSTGRES_DSN",
    "CORP_LLM_DSN",
    "CORP_LLM_REDIS_URL",
    "CORP_LLM_CACHE_URL",
)


@pytest.fixture
def hermetic_gateway_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate bootstrap config resolution from both process env and a real
    ``~/.corp-llm-gateway/config.toml``: clear every managed key, then point the
    loader at an empty TOML so only a test's own explicit values resolve."""
    for name in MANAGED_ENV:
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path / "hermetic-config.toml"
    empty.write_text("")
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(empty))
    config.reset_cache()
    yield
    config.reset_cache()
