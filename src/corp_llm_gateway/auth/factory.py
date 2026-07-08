from collections.abc import Callable

from corp_llm_gateway import config
from corp_llm_gateway.auth.providers import (
    ApiKeyHeaderAuthProvider,
    BearerAuthProvider,
    CorpLlmAuthProvider,
    MtlsAuthProvider,
    NoopAuthProvider,
    OidcAuthProvider,
)

AuthProviderFactory = Callable[[], CorpLlmAuthProvider]


def _make_noop() -> CorpLlmAuthProvider:
    return NoopAuthProvider()


def _make_bearer() -> CorpLlmAuthProvider:
    return BearerAuthProvider(token=config.get_required("CORP_LLM_BEARER_TOKEN"))


def _make_mtls() -> CorpLlmAuthProvider:
    return MtlsAuthProvider(
        cert_path=config.get_required("CORP_LLM_MTLS_CERT"),
        key_path=config.get_required("CORP_LLM_MTLS_KEY"),
    )


def _make_oidc() -> CorpLlmAuthProvider:
    return OidcAuthProvider(
        issuer=config.get_required("CORP_LLM_OIDC_ISSUER"),
        client_id=config.get_required("CORP_LLM_OIDC_CLIENT_ID"),
        client_secret=config.get_required("CORP_LLM_OIDC_CLIENT_SECRET"),
    )


def _make_apikey() -> CorpLlmAuthProvider:
    return ApiKeyHeaderAuthProvider(
        header_name=config.get("CORP_LLM_API_KEY_HEADER", "X-Api-Key") or "X-Api-Key",
        key=config.get_required("CORP_LLM_API_KEY"),
    )


# Keyed dispatch — the concrete shape the generic extensions/registry.py
# generalizes. Factories build lazily so config is read only on selection.
_PROVIDER_FACTORIES: dict[str, AuthProviderFactory] = {
    "noop": _make_noop,
    "bearer": _make_bearer,
    "mtls": _make_mtls,
    "oidc": _make_oidc,
    "apikey": _make_apikey,
}

_KNOWN_PROVIDERS = tuple(_PROVIDER_FACTORIES)


def get_auth_provider() -> CorpLlmAuthProvider:
    name = (config.get("CORP_LLM_AUTH_PROVIDER", "noop") or "noop").lower()
    factory = _PROVIDER_FACTORIES.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown CORP_LLM_AUTH_PROVIDER={name!r}; expected one of {_KNOWN_PROVIDERS}"
        )
    return factory()
