import os

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
    name = os.environ.get("CORP_LLM_AUTH_PROVIDER", "noop").lower()

    if name == "noop":
        return NoopAuthProvider()
    if name == "bearer":
        return BearerAuthProvider(token=_required_env("CORP_LLM_BEARER_TOKEN"))
    if name == "mtls":
        return MtlsAuthProvider(
            cert_path=_required_env("CORP_LLM_MTLS_CERT"),
            key_path=_required_env("CORP_LLM_MTLS_KEY"),
        )
    if name == "oidc":
        return OidcAuthProvider(
            issuer=_required_env("CORP_LLM_OIDC_ISSUER"),
            client_id=_required_env("CORP_LLM_OIDC_CLIENT_ID"),
            client_secret=_required_env("CORP_LLM_OIDC_CLIENT_SECRET"),
        )
    if name == "apikey":
        return ApiKeyHeaderAuthProvider(
            header_name=os.environ.get("CORP_LLM_API_KEY_HEADER", "X-Api-Key"),
            key=_required_env("CORP_LLM_API_KEY"),
        )

    raise ValueError(
        f"Unknown CORP_LLM_AUTH_PROVIDER={name!r}; expected one of {_KNOWN_PROVIDERS}"
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"environment variable {name} is required for selected auth provider"
        )
    return value
