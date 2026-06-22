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
