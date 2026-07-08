"""Optional TOML config file — fallback when env vars aren't set.

Lookup precedence for any setting NAME:
  1. ``os.environ[NAME]`` (existing deployments keep working)
  2. TOML file at the first existing path of:
     - ``$CORP_LLM_GATEWAY_CONFIG_FILE``
     - ``~/.corp-llm-gateway/config.toml`` (laptop default)
     - ``/etc/corp-llm-gateway/config.toml`` (server default)
  3. caller-provided default

Keys in the TOML file are the same names as the env vars (flat, no
sections) so the file is a drop-in fallback for any code path that
previously read ``os.environ.get(NAME, default)``.

Example file::

    CORP_GATEWAY_URL = "https://gateway.corp.lan"
    CORP_GATEWAY_TOKEN_FILE = "~/.corp-llm-gateway/token"
    CORP_LLM_AUTH_PROVIDER = "bearer"
    CORP_LLM_BEARER_TOKEN = "ct_..."
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

_DEFAULT_PATHS: tuple[str, ...] = (
    "~/.corp-llm-gateway/config.toml",
    "/etc/corp-llm-gateway/config.toml",
)


@lru_cache(maxsize=1)
def _load_file() -> dict[str, Any]:
    explicit = os.environ.get("CORP_LLM_GATEWAY_CONFIG_FILE")
    candidates: tuple[str, ...] = (explicit, *_DEFAULT_PATHS) if explicit else _DEFAULT_PATHS
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path.is_file():
            with path.open("rb") as fh:
                return tomllib.load(fh)
    return {}


def reset_cache() -> None:
    """Drop the cached file contents. Test hook only."""
    _load_file.cache_clear()


def get(name: str, default: str | None = None) -> str | None:
    """Resolve ``name`` as a string. Env wins; file fallback; then ``default``."""
    env_value = os.environ.get(name)
    if env_value is not None:
        return env_value
    file_value = _load_file().get(name)
    if file_value is not None:
        return str(file_value)
    return default


def get_required(name: str) -> str:
    """Like :func:`get` but raises ``RuntimeError`` if neither source supplies a value."""
    value = get(name)
    if not value:
        raise RuntimeError(
            f"setting {name!r} is required: set the env var or add it to "
            f"{_DEFAULT_PATHS[0]} (or $CORP_LLM_GATEWAY_CONFIG_FILE)"
        )
    return value


def get_table(prefix: str) -> dict[str, Any]:
    """Resolve a nested TOML table by dotted ``prefix`` (file-only; missing → ``{}``).

    Env vars carry flat scalars only, so tables have no env override — unlike
    :func:`get`, this never reads ``os.environ``. A scalar at the path (or a
    missing key) yields an empty dict.
    """
    node: Any = _load_file()
    for part in prefix.split("."):
        if not isinstance(node, dict):
            return {}
        node = node.get(part)
    return node if isinstance(node, dict) else {}


def oversize_policy() -> str:
    """Resolve ``CORP_LLM_OVERSIZE_POLICY`` (fail-closed | chunk | deliver-flag).

    Unset/empty resolves to ``fail-closed``; an unknown value raises ``ValueError``.
    """
    from corp_llm_gateway.payload.size_threshold import normalize_oversize_policy

    return normalize_oversize_policy(get("CORP_LLM_OVERSIZE_POLICY"))


_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def require_ner() -> bool:
    """Whether a self-disabled NER engine must fail the request CLOSED (M4/F2).

    ``CORP_LLM_REQUIRE_NER`` truthy → production fail-closed: a configured NER
    engine whose model/deps are absent raises ``NerUnavailableError`` (mapped to
    503 ``E_NER_UNAVAILABLE`` at the hook) instead of silently returning no
    findings and letting a PERSON/ORG egress. Unset/false → the dev /
    Python-3.14 graceful-degradation path (no NER, returns ``[]``).

    Default **off** so the local 3.14 suite keeps the graceful path; prod
    profiles / Helm set it on. The knob — not silent absence — distinguishes
    "expected-absent" (fail-closed) from "genuinely not configured" (dev).
    """
    value = get("CORP_LLM_REQUIRE_NER", "0")
    return value is not None and value.strip().lower() in _TRUTHY


def corp_llm_verify() -> bool | str:
    """``verify`` value for the corp-LLM httpx client (TLS to the corp LLM).

    Precedence:
      1. ``CORP_LLM_CA_BUNDLE`` — path to a PEM CA bundle. When set, the corp
         LLM cert is verified AGAINST that bundle (verification stays ON). Use
         this when the corp LLM presents a cert signed by an internal CA (e.g.
         the Corp Root + Issuing CA chain).
      2. ``SSL_VERIFY`` — ``"false"`` disables verification; anything else (or
         unset) leaves it ON against the system/certifi trust store.
    """
    ca_bundle = get("CORP_LLM_CA_BUNDLE", "")
    if ca_bundle:
        return ca_bundle
    return get("SSL_VERIFY", "true").lower() != "false"
