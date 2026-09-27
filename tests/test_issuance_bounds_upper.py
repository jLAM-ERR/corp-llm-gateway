"""Upper bounds of the issuance tunables: a value `config check` accepts must be
one the gateway can serve with. Otherwise the boot dies with a traceback instead
of exit 78, or every issuance answers 500."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from corp_llm_gateway import config, settings
from corp_llm_gateway.settings import ConfigError
from corp_llm_gateway.tokens import InMemoryTokenStore, IssuancePolicy, OidcClaims


@pytest.fixture
def issuance_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in settings.all_keys():
        monkeypatch.delenv(name, raising=False)
    cfg = tmp_path / "config.toml"
    cfg.write_text('[CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP]\n"/devs" = "t1"\n')
    monkeypatch.setenv("CORP_LLM_GATEWAY_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_ISSUER", "https://keycloak.corp.lan/realms/dev")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_AUDIENCE", "corp-gateway-issuance")
    monkeypatch.setenv("CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID", "corp-gateway-cli")
    config.reset_cache()
    yield
    config.reset_cache()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS", "3000000"),
        ("CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS", "1000000000"),
        ("CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS", "100000000000000"),
    ],
    ids=["ttl-past-year-9999", "ttl-past-timedelta", "interval-past-timedelta"],
)
async def test_an_accepted_bound_is_one_issuance_can_run_with(
    issuance_env: None, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    monkeypatch.setenv(key, value)
    try:
        resolved = settings.issuance()
    except ConfigError as exc:
        assert any(key in problem for problem in exc.problems)
        return
    assert resolved is not None

    policy = IssuancePolicy(InMemoryTokenStore(), resolved)
    result = await policy.issue(OidcClaims("u", "t1", issuer="i", subject="s", jti="j"))

    assert result.corp_token
