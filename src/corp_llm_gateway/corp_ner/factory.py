"""The single reader of the corp-NER transport settings.

Two call sites build a ``CorpNerClient``: the composition root
(``bootstrap.build_corp_ner``) and the profile detector registry
(``profiles.registry._make_corp_ner``). They both need the same CA bundle,
timeout and batch limits, so they resolve them here. A second, independent
reader is exactly how the profile path came to ignore ``CORP_NER_CA_BUNDLE``
(TLS against an internal CA failed, every request of that team 503'd).

Endpoint resolution stays with the callers: bootstrap requires it at boot, a
profile bundle may name its own. Construction performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from corp_llm_gateway import config
from corp_llm_gateway.corp_ner.client import (
    DEFAULT_TIMEOUT_S,
    MAX_INPUT_CHARS,
    MAX_TEXTS,
    CorpNerClient,
)
from corp_llm_gateway.settings import ConfigError

if TYPE_CHECKING:
    from collections.abc import Mapping

# The one detector table that is actually read; a generic ExtensionRegistry
# .discover() over every [extensions.<kind>.<name>] stays out of scope (C2/C3).
CORP_NER_TABLE = "extensions.detector.corp_ner"


def corp_ner_setting(
    table: Mapping[str, object], env_name: str, table_key: str, default: str | None = None
) -> str | None:
    """Resolve one corp-NER value: env/scalar chain first, then the table.

    ``config.get`` already covers env → flat config scalar; the nested table is
    file-only (env carries scalars), so it sits underneath — a container can
    always override a baked-in config file.
    """
    value = config.get(env_name)
    if value:
        return value
    raw = table.get(table_key)
    if raw is not None:
        return str(raw)
    return default


def corp_ner_int(table: Mapping[str, object], env_name: str, table_key: str, default: str) -> int:
    raw = corp_ner_setting(table, env_name, table_key, default) or default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError([f"{env_name}={raw!r} is not an integer"]) from exc


@dataclass(frozen=True)
class CorpNerTransport:
    """Resolved transport knobs; turns into a client for a given endpoint.

    TLS verification is never disabled (unlike ``SSL_VERIFY`` for the oracle):
    this call carries RAW user content. Point ``CORP_NER_CA_BUNDLE`` at an
    internal CA instead.
    """

    timeout_s: float
    ca_bundle: str | None
    max_texts: int
    max_input_chars: int

    @property
    def verify(self) -> bool | str:
        return self.ca_bundle or True

    def http_client(self) -> httpx.AsyncClient:
        """An http client the CALLER owns — for sharing with the readiness probe."""
        return httpx.AsyncClient(timeout=self.timeout_s, verify=self.verify)

    def client(self, endpoint: str, *, http: httpx.AsyncClient | None = None) -> CorpNerClient:
        return CorpNerClient(
            endpoint,
            http=http,
            timeout=self.timeout_s,
            verify=self.verify,
            max_texts=self.max_texts,
            max_input_chars=self.max_input_chars,
        )


def resolve_corp_ner_transport(table: Mapping[str, object] | None = None) -> CorpNerTransport:
    """Read CORP_NER_TIMEOUT_S / CA_BUNDLE / MAX_TEXTS / MAX_INPUT_CHARS."""
    tbl = table if table is not None else config.get_table(CORP_NER_TABLE)
    timeout = corp_ner_setting(tbl, "CORP_NER_TIMEOUT_S", "timeout_s", str(DEFAULT_TIMEOUT_S))
    try:
        timeout_s = float(timeout or DEFAULT_TIMEOUT_S)
    except ValueError as exc:
        raise ConfigError([f"CORP_NER_TIMEOUT_S={timeout!r} is not a number"]) from exc
    return CorpNerTransport(
        timeout_s=timeout_s,
        ca_bundle=corp_ner_setting(tbl, "CORP_NER_CA_BUNDLE", "ca_bundle"),
        max_texts=corp_ner_int(tbl, "CORP_NER_MAX_TEXTS", "max_texts", str(MAX_TEXTS)),
        max_input_chars=corp_ner_int(
            tbl, "CORP_NER_MAX_INPUT_CHARS", "max_input_chars", str(MAX_INPUT_CHARS)
        ),
    )
