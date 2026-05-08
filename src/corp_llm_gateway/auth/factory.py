from corp_llm_gateway import config
from corp_llm_gateway.auth.providers import (
    ApiKeyHeaderAuthProvider,
    BearerAuthProvider,
    CorpLlmAuthProvider,
    MtlsAuthProvider,
    NoopAuthProvider,
    OidcAuthProvider,
)

_KNOWN_PROVIDERS = ("noop", "bearer", "mtls", "oidc", "apikey")


def get_auth_provider() -> CorpLlmAuthProvider:
    name = (config.get("CORP_LLM_AUTH_PROVIDER", "noop") or "noop").lower()

    if name == "noop":
        return NoopAuthProvider()
    if name == "bearer":
        return BearerAuthProvider(token=config.get_required("CORP_LLM_BEARER_TOKEN"))
    if name == "mtls":
        return MtlsAuthProvider(
            cert_path=config.get_required("CORP_LLM_MTLS_CERT"),
            key_path=config.get_required("CORP_LLM_MTLS_KEY"),
        )
    if name == "oidc":
        return OidcAuthProvider(
            issuer=config.get_required("CORP_LLM_OIDC_ISSUER"),
            client_id=config.get_required("CORP_LLM_OIDC_CLIENT_ID"),
            client_secret=config.get_required("CORP_LLM_OIDC_CLIENT_SECRET"),
        )
    if name == "apikey":
        return ApiKeyHeaderAuthProvider(
            header_name=config.get("CORP_LLM_API_KEY_HEADER", "X-Api-Key") or "X-Api-Key",
            key=config.get_required("CORP_LLM_API_KEY"),
        )

    raise ValueError(
        f"Unknown CORP_LLM_AUTH_PROVIDER={name!r}; expected one of {_KNOWN_PROVIDERS}"
    )
