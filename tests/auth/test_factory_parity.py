"""Parity checks for the generalized ``auth/factory.py`` keyed dispatch.

The if/elif was replaced by ``_PROVIDER_FACTORIES``; assert every known name
still dispatches to the right provider and that an unknown name fails closed
with the ADR-001 listing ValueError. Complements ``test_providers.py``.
"""

import pytest

from corp_llm_gateway.auth import (
    ApiKeyHeaderAuthProvider,
    BearerAuthProvider,
    CorpLlmAuthProvider,
    MtlsAuthProvider,
    NoopAuthProvider,
    OidcAuthProvider,
    get_auth_provider,
)

_ALL_AUTH_ENV = (
    "CORP_LLM_AUTH_PROVIDER",
    "CORP_LLM_BEARER_TOKEN",
    "CORP_LLM_MTLS_CERT",
    "CORP_LLM_MTLS_KEY",
    "CORP_LLM_OIDC_ISSUER",
    "CORP_LLM_OIDC_CLIENT_ID",
    "CORP_LLM_OIDC_CLIENT_SECRET",
    "CORP_LLM_API_KEY",
    "CORP_LLM_API_KEY_HEADER",
)


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _ALL_AUTH_ENV:
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize(
    ("name", "env", "expected"),
    [
        ("noop", {}, NoopAuthProvider),
        ("bearer", {"CORP_LLM_BEARER_TOKEN": "tok"}, BearerAuthProvider),
        ("mtls", {"CORP_LLM_MTLS_CERT": "/c", "CORP_LLM_MTLS_KEY": "/k"}, MtlsAuthProvider),
        (
            "oidc",
            {
                "CORP_LLM_OIDC_ISSUER": "https://kc",
                "CORP_LLM_OIDC_CLIENT_ID": "cid",
                "CORP_LLM_OIDC_CLIENT_SECRET": "sec",
            },
            OidcAuthProvider,
        ),
        ("apikey", {"CORP_LLM_API_KEY": "k"}, ApiKeyHeaderAuthProvider),
    ],
)
def test_each_known_provider_dispatches(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    env: dict[str, str],
    expected: type[CorpLlmAuthProvider],
) -> None:
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", name)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    provider = get_auth_provider()
    assert isinstance(provider, expected)
    assert isinstance(provider, CorpLlmAuthProvider)


def test_unknown_provider_error_lists_every_known_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "bedrock")
    with pytest.raises(ValueError) as exc:
        get_auth_provider()
    msg = str(exc.value)
    assert "Unknown CORP_LLM_AUTH_PROVIDER" in msg
    for known in ("noop", "bearer", "mtls", "oidc", "apikey"):
        assert known in msg
