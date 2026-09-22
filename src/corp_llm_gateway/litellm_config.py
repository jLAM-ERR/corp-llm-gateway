"""Checks on litellm's own proxy config — the YAML the gateway serves.

Two callers, one rule set: ``asgi.py`` runs these before it imports litellm's
app and exits 78 on any problem (the check litellm's CLI did through
``ProxyConfig.get_config`` and its lifespan does NOT — ``proxy_server.py:1088-1095``
silently skips a missing or non-YAML file, which starts the proxy with no
guardrail callback at all), and ``settings.validate()`` runs the content half so
``gateway-admin config check`` reports the same problems before a pod starts.

Two blocks are refused outright:

* ``pass_through_endpoints`` — litellm's ``SafeRouteAdder``
  (``pass_through_endpoints/pass_through_endpoints.py:2781``) registers routes from
  that block at runtime, with paths no static read of litellm's source can know.
  The route gate default-denies whatever they would add, so configuring them
  produces routes that can only 404 — refusing the config says so at boot instead.
* ``general_settings.database_url`` — litellm's CLI reads a DSN from there and
  from the ``DATABASE_HOST``/``DATABASE_USERNAME``/``DATABASE_NAME`` composition
  (``proxy_cli.py:1183-1190``) before it runs the Prisma schema sequence.
  ``asgi.py`` replicates the sequence but reads ``DATABASE_URL``/``DIRECT_URL``
  only, so a config-only DSN would boot a proxy that connects to a database whose
  schema was never set up. Refuse it; ``DATABASE_URL`` is the supported source.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from corp_llm_gateway import config

# Where litellm's config is mounted in the image (Dockerfile.gateway, the Helm
# configmap and compose all use this path).
DEFAULT_CONFIG_PATH = "/etc/litellm/config.yaml"

CONFIG_PATH_KEY = "CORP_LLM_LITELLM_CONFIG"

# sysexits.h EX_CONFIG. Both entrypoint modules exit with it — `asgi.py` when the
# litellm config is unusable, `serve.py` when a gateway setting is. The runbook
# and the container tests key off the number, so it has one definition.
EXIT_CONFIG = 78

_YAML_SUFFIXES = (".yaml", ".yml")

PASS_THROUGH_KEY = "pass_through_endpoints"

DATABASE_URL_KEY = "database_url"


def config_path() -> Path:
    """The litellm config path, resolved through the documented config chain."""
    raw = config.get(CONFIG_PATH_KEY, DEFAULT_CONFIG_PATH) or DEFAULT_CONFIG_PATH
    return Path(raw.strip())


def path_problems(path: Path) -> list[str]:
    """Problems with the file itself: absent, not a file, or not YAML."""
    problems: list[str] = []
    if not path.exists():
        problems.append(
            f"{CONFIG_PATH_KEY}={path}: no such file. litellm's own lifespan skips a "
            "missing config silently, which starts the proxy with no guardrail callback"
        )
        return problems
    if not path.is_file():
        problems.append(f"{CONFIG_PATH_KEY}={path}: not a file")
        return problems
    if path.suffix.lower() not in _YAML_SUFFIXES:
        problems.append(
            f"{CONFIG_PATH_KEY}={path}: litellm reads this as YAML; "
            f"name it {' or '.join(_YAML_SUFFIXES)}"
        )
    return problems


def content_problems(path: Path) -> list[str]:
    """Problems inside the file: unreadable, not YAML, or a refused block."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{CONFIG_PATH_KEY}={path}: unreadable ({type(exc).__name__})"]
    try:
        import yaml
    except ImportError:  # pragma: no cover - PyYAML ships with litellm and dev
        return [f"{CONFIG_PATH_KEY}={path}: cannot be checked, PyYAML is not installed"]
    try:
        document: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return [f"{CONFIG_PATH_KEY}={path}: not valid YAML ({type(exc).__name__})"]
    if document is None:
        return [f"{CONFIG_PATH_KEY}={path}: empty; litellm would load no callbacks"]
    if not isinstance(document, dict):
        return [f"{CONFIG_PATH_KEY}={path}: top level must be a mapping"]
    general = document.get("general_settings")
    if general is not None and not isinstance(general, dict):
        # litellm's own loader would take it: `load_config` does
        # `general_settings.get("search_tools")` (proxy_server.py:5118) on
        # anything truthy, so a scalar is an AttributeError mid-startup.
        return [
            f"{CONFIG_PATH_KEY}={path}: general_settings must be a mapping, "
            f"not {type(general).__name__}; litellm reads keys off it at startup"
        ]
    general = general or {}
    if PASS_THROUGH_KEY in general or PASS_THROUGH_KEY in document:
        return [
            f"{CONFIG_PATH_KEY}={path}: general_settings.{PASS_THROUGH_KEY} is refused. "
            "litellm registers those routes at runtime from config, so the route gate "
            "cannot classify them and default-deny refuses every one; remove the block"
        ]
    if DATABASE_URL_KEY in general:
        return [
            f"{CONFIG_PATH_KEY}={path}: general_settings.{DATABASE_URL_KEY} is refused. "
            "The entrypoint's Prisma schema setup reads the DATABASE_URL / DIRECT_URL "
            "environment only, so a DSN that lives only in this file would let litellm "
            "connect to a database whose schema was never set up; set DATABASE_URL instead"
        ]
    return []


def problems(path: Path, *, require_file: bool) -> list[str]:
    """Every problem with ``path``.

    ``require_file=False`` (``config check`` on a host where litellm's config is
    not mounted) reports content problems only when the file is there; the
    entrypoint passes True and treats absence as fatal.
    """
    found = path_problems(path)
    if found:
        return found if require_file else []
    return content_problems(path)


def general_settings(path: Path) -> dict[str, Any]:
    """``general_settings`` from the config, as litellm's CLI reads it
    (``proxy_cli.py:1167-1170``). Empty when absent or malformed."""
    document = _document(path)
    settings = document.get("general_settings") or {}
    return settings if isinstance(settings, dict) else {}


def json_logs(path: Path) -> bool:
    """``litellm_settings.json_logs``, or litellm's ``JSON_LOGS`` env var.

    ``serve.py`` needs it to pick uvicorn's log config and ``asgi.py`` to pick
    the boot handler's formatter; both run before litellm is imported, so it
    cannot be read off ``litellm.json_logs``.

    The env var is litellm's own (``litellm/_logging.py:374``, strict ``"true"``
    — read here, not through the gateway config chain, for the same reason
    ``DATABASE_URL`` is): litellm's logging goes JSON at import whenever it is
    set, whatever the YAML says, so reading only the YAML would put two record
    shapes on one stdout that Vector parses.
    """
    if (os.getenv("JSON_LOGS") or "").lower() == "true":
        return True
    settings = _document(path).get("litellm_settings") or {}
    return isinstance(settings, dict) and settings.get("json_logs") is True


def _document(path: Path) -> dict[str, Any]:
    try:
        import yaml

        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        # Any read or parse failure reads as "nothing set": `problems()` is what
        # refuses a bad file, and it has already run by the time these are called.
        return {}
    return document if isinstance(document, dict) else {}
