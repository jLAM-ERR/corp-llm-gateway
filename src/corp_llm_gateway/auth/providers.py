from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AuthArtifacts:
    headers: dict[str, str] = field(default_factory=dict)
    client_cert: tuple[str, str] | None = None


class CorpLlmAuthProvider(ABC):
    @abstractmethod
    def artifacts(self) -> AuthArtifacts: ...


class NoopAuthProvider(CorpLlmAuthProvider):
    def artifacts(self) -> AuthArtifacts:
        return AuthArtifacts()


class BearerAuthProvider(CorpLlmAuthProvider):
    def __init__(self, token: str) -> None:
        self._token = token

    def artifacts(self) -> AuthArtifacts:
        raise NotImplementedError(
            "BearerAuthProvider stub — implement when corp LLM enables Bearer auth"
        )


class MtlsAuthProvider(CorpLlmAuthProvider):
    def __init__(self, cert_path: str, key_path: str) -> None:
        self._cert_path = cert_path
        self._key_path = key_path

    def artifacts(self) -> AuthArtifacts:
        raise NotImplementedError(
            "MtlsAuthProvider stub — implement when corp LLM enables mTLS"
        )


class OidcAuthProvider(CorpLlmAuthProvider):
    def __init__(self, issuer: str, client_id: str, client_secret: str) -> None:
        self._issuer = issuer
        self._client_id = client_id
        self._client_secret = client_secret

    def artifacts(self) -> AuthArtifacts:
        raise NotImplementedError(
            "OidcAuthProvider stub — implement when corp LLM enables OIDC"
        )


class ApiKeyHeaderAuthProvider(CorpLlmAuthProvider):
    def __init__(self, header_name: str, key: str) -> None:
        self._header_name = header_name
        self._key = key

    def artifacts(self) -> AuthArtifacts:
        raise NotImplementedError(
            "ApiKeyHeaderAuthProvider stub — implement when corp LLM enables API key"
        )
