import pytest

from corp_llm_gateway.auth import (
    ApiKeyHeaderAuthProvider,
    AuthArtifacts,
    BearerAuthProvider,
    CorpLlmAuthProvider,
    MtlsAuthProvider,
    NoopAuthProvider,
    OidcAuthProvider,
    get_auth_provider,
)


def test_noop_returns_empty_artifacts() -> None:
    artifacts = NoopAuthProvider().artifacts()
    assert artifacts == AuthArtifacts()
    assert artifacts.headers == {}
    assert artifacts.client_cert is None


@pytest.mark.parametrize(
    ("provider_factory",),
    [
        # BearerAuthProvider is no longer a stub — it returns the
        # Authorization header. Covered separately by
        # ``test_bearer_provider_emits_authorization_header``.
        (lambda: MtlsAuthProvider(cert_path="/c", key_path="/k"),),
        (lambda: OidcAuthProvider(issuer="i", client_id="c", client_secret="s"),),
        (lambda: ApiKeyHeaderAuthProvider(header_name="X-Api-Key", key="k"),),
    ],
)
def test_stub_providers_raise_not_implemented(
    provider_factory: object,
) -> None:
    instance: CorpLlmAuthProvider = provider_factory()  # type: ignore[operator]
    with pytest.raises(NotImplementedError):
        instance.artifacts()


def test_bearer_provider_emits_authorization_header() -> None:
    artifacts = BearerAuthProvider(token="my-token").artifacts()
    assert artifacts.headers == {"Authorization": "Bearer my-token"}
    assert artifacts.client_cert is None


def test_factory_default_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORP_LLM_AUTH_PROVIDER", raising=False)
    assert isinstance(get_auth_provider(), NoopAuthProvider)


def test_factory_explicit_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "noop")
    assert isinstance(get_auth_provider(), NoopAuthProvider)


def test_factory_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "NOOP")
    assert isinstance(get_auth_provider(), NoopAuthProvider)


def test_factory_bearer_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "bearer")
    monkeypatch.delenv("CORP_LLM_BEARER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="CORP_LLM_BEARER_TOKEN"):
        get_auth_provider()


def test_factory_bearer_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "bearer")
    monkeypatch.setenv("CORP_LLM_BEARER_TOKEN", "tok")
    assert isinstance(get_auth_provider(), BearerAuthProvider)


def test_factory_mtls_requires_both_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "mtls")
    monkeypatch.setenv("CORP_LLM_MTLS_CERT", "/c")
    monkeypatch.delenv("CORP_LLM_MTLS_KEY", raising=False)
    with pytest.raises(RuntimeError, match="CORP_LLM_MTLS_KEY"):
        get_auth_provider()


def test_factory_apikey_default_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "apikey")
    monkeypatch.setenv("CORP_LLM_API_KEY", "k")
    monkeypatch.delenv("CORP_LLM_API_KEY_HEADER", raising=False)
    provider = get_auth_provider()
    assert isinstance(provider, ApiKeyHeaderAuthProvider)


def test_factory_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORP_LLM_AUTH_PROVIDER", "wat")
    with pytest.raises(ValueError, match="Unknown CORP_LLM_AUTH_PROVIDER"):
        get_auth_provider()
