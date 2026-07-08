"""Production composition root — assemble a `CorpLlmGuardrail` from config.

Every setting resolves through `corp_llm_gateway.config` (env → config.toml →
default); there are no direct `os.environ` reads here (CLAUDE.md config-resolution
contract). Backend selection is config-driven:

- token store   → Postgres when ``CORP_LLM_PG_DSN`` is set, else in-memory
- team config   → Postgres when ``CORP_LLM_PG_DSN`` is set, else in-memory
- mapping store → Redis when ``REDIS_URL`` is set, else in-memory
- corp-LLM auth → `auth.factory.get_auth_provider` (never inline auth)

The ``guardrail`` attribute is the instance LiteLLM's ``callbacks:`` imports as
``corp_llm_gateway.bootstrap.guardrail``. It is built lazily on first access
(PEP 562 ``__getattr__``) so importing this module — or any module that imports
it — stays side-effect-free; prod still fails fast on the first callback
resolution. Construction performs NO network I/O — stores and clients connect
lazily on first await.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import httpx

from corp_llm_gateway import config
from corp_llm_gateway.audit import AuditLogger, Sink, get_sink, register_sink, sink_name_for
from corp_llm_gateway.auth import get_auth_provider
from corp_llm_gateway.corp_llm import CorpLlmClient
from corp_llm_gateway.detectors import DualNerDetector, RegexChecksumDetector
from corp_llm_gateway.detectors.base import PIIDetector
from corp_llm_gateway.extensions import EXTENSION_API_VERSION, REGISTRY
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
from corp_llm_gateway.rules import (
    CachedRulesLoader,
    FileRulesLoader,
    Gazetteer,
    Rules,
    RulesLoader,
    RulesNotFoundError,
)
from corp_llm_gateway.sanitizer import SanitizationOrchestrator
from corp_llm_gateway.sanitizer.allowlist import Allowlist
from corp_llm_gateway.sanitizer.dlp_guard import DlpEgressGuard
from corp_llm_gateway.storage import InMemoryMappingStore, MappingStore
from corp_llm_gateway.team_config import (
    InMemoryTeamConfigStore,
    PostgresTeamConfigStore,
    TeamConfigStore,
)
from corp_llm_gateway.tokens import AuthMiddleware
from corp_llm_gateway.tokens.middleware import make_auth_middleware

_DIST_NAME = "corp-llm-gateway"
_DEFAULT_ENDPOINT = "https://corp-llm.example/v1"
_DEFAULT_MODEL = "GLM-5.1-AWQ"
_DEFAULT_RULES_DIR = "/etc/corp-llm-gateway/rules"
_FALSEY = frozenset({"0", "false", "False", "FALSE"})

_log = logging.getLogger(__name__)


def _flag(name: str, default: str = "1") -> bool:
    return (config.get(name, default) or default) not in _FALSEY


def gateway_version() -> str:
    """Installed distribution version; a stable sentinel when running from source."""
    try:
        return version(_DIST_NAME)
    except PackageNotFoundError:
        return "0.0.0+unknown"


class _RulesLoader(RulesLoader):
    """Load per-team replace.md rules from disk; empty rules on a missing file."""

    def __init__(self, directory: str) -> None:
        self._inner = CachedRulesLoader(FileRulesLoader(Path(directory)))

    async def load(self, team_id: str) -> Rules:
        try:
            return await self._inner.load(team_id)
        except RulesNotFoundError:
            return Rules(rules=())


def build_team_config_store() -> TeamConfigStore:
    """Postgres team-config store when a DSN is configured, else in-memory."""
    dsn = config.get("CORP_LLM_PG_DSN")
    if dsn:
        return PostgresTeamConfigStore(dsn)
    return InMemoryTeamConfigStore()


def build_mapping_store() -> MappingStore:
    """Redis mapping store when ``REDIS_URL`` is configured, else in-memory.

    ``redis.asyncio.from_url`` builds a client without connecting; the pool is
    established lazily on the first command.
    """
    url = config.get("REDIS_URL")
    if not url:
        return InMemoryMappingStore()
    from redis.asyncio import from_url

    from corp_llm_gateway.storage import RedisMappingStore

    return RedisMappingStore(from_url(url, decode_responses=True))


def build_corp_llm_client() -> CorpLlmClient:
    """corp-LLM (vLLM oracle) client; auth comes from `get_auth_provider`."""
    endpoint = config.get("CORP_LLM_ENDPOINT") or _DEFAULT_ENDPOINT
    if endpoint == _DEFAULT_ENDPOINT:
        _log.warning(
            "CORP_LLM_ENDPOINT is unset; using placeholder %s. Set it before production egress.",
            _DEFAULT_ENDPOINT,
        )
    # CorpLlmClient appends /v1/chat/completions; strip a trailing /v1 that
    # the litellm hosted_vllm api_base needs but this client must not double.
    base_url = endpoint.rstrip("/").removesuffix("/v1")
    model = config.get("CORP_LLM_MODEL", _DEFAULT_MODEL) or _DEFAULT_MODEL
    http = httpx.AsyncClient(timeout=30.0, verify=config.corp_llm_verify())
    return CorpLlmClient(
        base_url=base_url,
        model=model,
        http=http,
        auth_provider=get_auth_provider(),
    )


def _build_orchestrator(
    corp_llm: CorpLlmClient, mapping_store: MappingStore
) -> SanitizationOrchestrator:
    local_detectors: list[PIIDetector] | None = (
        [RegexChecksumDetector(), DualNerDetector()] if _flag("CORP_LLM_LOCAL_FIRST") else None
    )
    gazetteer = Gazetteer.from_defaults() if _flag("CORP_LLM_GAZETTEER") else None
    rules_dir = config.get("CORP_LLM_RULES_DIR", _DEFAULT_RULES_DIR) or _DEFAULT_RULES_DIR
    raw_teams = config.get("CORP_LLM_OVERSIZE_DELIVER_TEAMS", "") or ""
    deliver_teams = frozenset(t.strip() for t in raw_teams.split(",") if t.strip())
    return SanitizationOrchestrator(
        corp_llm,
        mapping_store,
        _RulesLoader(rules_dir),
        oversize_policy=config.oversize_policy(),
        oversize_deliver_teams=deliver_teams,
        local_detectors=local_detectors,
        gazetteer=gazetteer,
        allowlist=Allowlist.from_config(),
    )


def _build_dlp_guard() -> DlpEgressGuard:
    raw = config.get("CORP_LLM_DLP_CANARIES", "") or ""
    canaries = [c.strip() for c in raw.split(",") if c.strip()]
    return DlpEgressGuard(canary_patterns=canaries or None, secret_rescan=True)


def build_guardrail(
    *,
    auth_middleware: AuthMiddleware | None = None,
    mapping_store: MappingStore | None = None,
    corp_llm: CorpLlmClient | None = None,
    dlp_guard: DlpEgressGuard | None = None,
    sink: Sink | None = None,
    max_output_tokens_cap: int | None = None,
    strip_inbound_headers_to_upstream: bool = False,
) -> CorpLlmGuardrail:
    """Assemble a `CorpLlmGuardrail` from config, with optional dep overrides.

    Overrides let the demo inject in-memory backends; when omitted, each backend
    is selected from config (Postgres/Redis when configured, else in-memory) and
    the audit sink from `CORP_AUDIT_SINK` via `get_sink()`.

    The active sink is registered in the extension REGISTRY (cached live
    instance) and `validate_api_version` runs here — inside the lazy build, never
    at import — so a version-incompatible extension is refused before the
    guardrail serves any traffic.
    """
    auth = auth_middleware if auth_middleware is not None else make_auth_middleware()
    store = mapping_store if mapping_store is not None else build_mapping_store()
    client = corp_llm if corp_llm is not None else build_corp_llm_client()
    orchestrator = _build_orchestrator(client, store)
    active_sink = sink if sink is not None else get_sink()
    register_sink(REGISTRY, active_sink, sink_name_for(active_sink))
    REGISTRY.validate_api_version(EXTENSION_API_VERSION)
    audit_logger = AuditLogger(active_sink, gateway_version=gateway_version())
    return CorpLlmGuardrail(
        orchestrator,
        auth,
        audit_logger,
        max_output_tokens_cap=max_output_tokens_cap,
        strip_inbound_headers_to_upstream=strip_inbound_headers_to_upstream,
        dlp_guard=dlp_guard if dlp_guard is not None else _build_dlp_guard(),
    )


_guardrail: CorpLlmGuardrail | None = None


def __getattr__(name: str) -> CorpLlmGuardrail:
    # PEP 562 lazy attribute: `corp_llm_gateway.bootstrap.guardrail` builds on
    # first access and caches, keeping plain imports side-effect-free.
    if name != "guardrail":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    global _guardrail
    if _guardrail is None:
        _guardrail = build_guardrail()
    return _guardrail
