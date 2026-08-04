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
from corp_llm_gateway.corp_ner import (
    CORP_NER_TABLE,
    corp_ner_setting,
    resolve_corp_ner_transport,
)
from corp_llm_gateway.detectors import DualNerDetector, RegexChecksumDetector
from corp_llm_gateway.detectors.base import PIIDetector
from corp_llm_gateway.detectors.corp_ner import CorpNerDetector
from corp_llm_gateway.extensions import EXTENSION_API_VERSION, REGISTRY
from corp_llm_gateway.extensions.corp_ner import CorpNerExtension, register_corp_ner
from corp_llm_gateway.litellm_hook import CorpLlmGuardrail
from corp_llm_gateway.metrics import get_exporter
from corp_llm_gateway.profiles import FileProfileLoader, ProfileBundle, ProfileResolver
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
from corp_llm_gateway.sanitizer.profile_orchestrator import (
    ProfileAwareOrchestrator,
    build_inner_orchestrator,
)
from corp_llm_gateway.settings import (
    NO_OP_SANITIZER_MESSAGE,
    ConfigError,
    forward_auth_conflict,
    master_key_conflict,
    parse_flag,
)
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
# Shipped profile bundles (core / ru-152fz / division-x); CORP_PROFILE_ROOT overrides.
_DEFAULT_PROFILE_ROOT = Path(__file__).parent / "profiles" / "defaults"

_log = logging.getLogger(__name__)


def _flag(name: str, default: str = "1") -> bool:
    # Delegates to settings.parse_flag so bootstrap and settings.validate() can
    # never disagree on what counts as falsy (off/no/0/false/"" — case-insensitive).
    return parse_flag(config.get(name, default))


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


def build_corp_ner() -> tuple[CorpNerDetector, CorpNerExtension] | None:
    """Corp NER detector + its registry extension, or None when disabled.

    Off by default (``CORP_NER_ENABLED=0``) so existing deploys are untouched.
    When on, a missing endpoint is a boot-time refusal — `settings.validate()`
    covers `config check` only, and the compose/demo boots skip it.

    No metrics exporter is threaded in: ``gateway_failure{component="corp_ner"}``
    is emitted once per request by ``litellm_hook._record_failure``.

    Transport settings (CA bundle, timeout, batch limits) resolve through
    ``corp_ner.resolve_corp_ner_transport`` — the same reader the profile
    registry uses, so the two paths cannot drift. Construction performs no I/O.
    """
    table = config.get_table(CORP_NER_TABLE)
    if not parse_flag(corp_ner_setting(table, "CORP_NER_ENABLED", "enabled", "0")):
        return None
    endpoint = corp_ner_setting(table, "CORP_NER_ENDPOINT", "endpoint")
    if not endpoint:
        raise ConfigError(
            [
                "CORP_NER_ENDPOINT: required when CORP_NER_ENABLED=1 — set the env var, "
                f"the config-file scalar, or endpoint under [{CORP_NER_TABLE}]"
            ]
        )
    transport = resolve_corp_ner_transport(table)
    http = transport.http_client()
    # The extension shares the client so readiness and the request path can never
    # disagree about how the service is reached.
    return CorpNerDetector(transport.client(endpoint, http=http)), CorpNerExtension(
        endpoint, http=http
    )


def _code_safe_detectors(
    local_detectors: list[PIIDetector], network_backed: PIIDetector | None
) -> list[PIIDetector]:
    """Detectors allowed on raw CODE segments — every LOCAL detector.

    The rule is network-backed vs local, NOT "NER vs regex". Local dual-NER
    stays in: PERSON / ORG / LOCATION inside fenced JSON, SQL values, config
    examples and test fixtures is caught there and nowhere else, so narrowing
    this list to regex/checksum only was a leak. ``network_backed`` (corp NER
    today) is the one exclusion, for two reasons specific to it: its regex half
    fires on code tokens, and calling it would ship the developer's source code
    to an external service. Derived by exclusion so a new LOCAL detector is
    code-safe by default; a new network-backed one has to be named here.
    """
    return [d for d in local_detectors if d is not network_backed]


def _deliver_teams() -> frozenset[str]:
    """Teams allowed the oversize deliver-flag (shared by core + profile inners)."""
    raw_teams = config.get("CORP_LLM_OVERSIZE_DELIVER_TEAMS", "") or ""
    return frozenset(t.strip() for t in raw_teams.split(",") if t.strip())


def _build_orchestrator(
    corp_llm: CorpLlmClient | None,
    mapping_store: MappingStore,
    *,
    oracle_enabled: bool,
    corp_ner: PIIDetector | None = None,
) -> SanitizationOrchestrator:
    local_detectors: list[PIIDetector] = []
    if _flag("CORP_LLM_LOCAL_FIRST"):
        local_detectors += [RegexChecksumDetector(), DualNerDetector()]
    if corp_ner is not None:
        local_detectors.append(corp_ner)
    gazetteer = Gazetteer.from_defaults() if _flag("CORP_LLM_GAZETTEER") else None
    rules_dir = config.get("CORP_LLM_RULES_DIR", _DEFAULT_RULES_DIR) or _DEFAULT_RULES_DIR
    return SanitizationOrchestrator(
        corp_llm,
        mapping_store,
        _RulesLoader(rules_dir),
        oversize_policy=config.oversize_policy(),
        oversize_deliver_teams=_deliver_teams(),
        local_detectors=local_detectors or None,
        code_safe_detectors=_code_safe_detectors(local_detectors, corp_ner),
        gazetteer=gazetteer,
        allowlist=Allowlist.from_config(),
        oracle_trigger=config.oracle_trigger(),
        oracle_enabled=oracle_enabled,
    )


def _build_profile_wrapper(
    core: SanitizationOrchestrator,
    *,
    corp_llm: CorpLlmClient | None,
    mapping_store: MappingStore,
    team_store: TeamConfigStore,
    oracle_enabled: bool,
) -> ProfileAwareOrchestrator:
    """Wrap the core orchestrator so a team's selected profile bundle is activated.

    A team with NO ``profile_ids`` resolves to passthrough — the core orchestrator
    with no fingerprint — so no-profile traffic is byte-identical to today (D4). A
    team that selects a profile gets one inner orchestrator per resolved layer-key,
    built by ``build_inner`` over the SHARED corp-LLM client + mapping store (Cache A)
    + a base ``replace.md`` loader. Construction does no I/O (the loader just holds
    a path; resolution/reads happen lazily on the first profiled request).
    """
    root = config.get("CORP_PROFILE_ROOT") or str(_DEFAULT_PROFILE_ROOT)
    resolver = ProfileResolver(FileProfileLoader(Path(root)))
    rules_dir = config.get("CORP_LLM_RULES_DIR", _DEFAULT_RULES_DIR) or _DEFAULT_RULES_DIR
    base_rules_loader = _RulesLoader(rules_dir)
    oversize_policy = config.oversize_policy()
    deliver_teams = _deliver_teams()
    oracle_trigger = config.oracle_trigger()

    def build_inner(bundle: ProfileBundle) -> SanitizationOrchestrator:
        return build_inner_orchestrator(
            bundle,
            corp_llm=corp_llm,
            mapping_store=mapping_store,
            base_rules_loader=base_rules_loader,
            oversize_policy=oversize_policy,
            oversize_deliver_teams=deliver_teams,
            oracle_enabled=oracle_enabled,
            oracle_trigger=oracle_trigger,
        )

    return ProfileAwareOrchestrator(
        core, team_store=team_store, resolver=resolver, build_inner=build_inner
    )


def _build_dlp_guard() -> DlpEgressGuard:
    raw = config.get("CORP_LLM_DLP_CANARIES", "") or ""
    canaries = [c.strip() for c in raw.split(",") if c.strip()]
    return DlpEgressGuard(canary_patterns=canaries or None, secret_rescan=True)


def _warn_on_disarmed_forward_auth_conflict(
    *,
    configured_chatgpt: bool,
    configured_anthropic: bool,
    resolved_chatgpt: bool,
    resolved_anthropic: bool,
) -> None:
    # An explicit kwarg legally resolves a both-flags-on config down to one live
    # bridge, but the deployment config stays broken and `config check` still
    # refuses it — say so instead of diverging silently.
    if forward_auth_conflict(chatgpt=configured_chatgpt, anthropic=configured_anthropic) is None:
        return
    disarmed = " and ".join(
        name
        for name, live in (
            ("CORP_LLM_FORWARD_CHATGPT_AUTH", resolved_chatgpt),
            ("CORP_LLM_FORWARD_ANTHROPIC_AUTH", resolved_anthropic),
        )
        if not live
    )
    _log.warning(
        "CORP_LLM_FORWARD_CHATGPT_AUTH and CORP_LLM_FORWARD_ANTHROPIC_AUTH are both enabled in "
        "config, which is mutually exclusive; an explicit build_guardrail() kwarg disarmed %s for "
        "this process, so the running gateway disagrees with its own config. "
        "`gateway-admin config check` still rejects this config — turn %s off in the deployment "
        "config.",
        disarmed,
        disarmed,
    )


def build_guardrail(
    *,
    auth_middleware: AuthMiddleware | None = None,
    mapping_store: MappingStore | None = None,
    corp_llm: CorpLlmClient | None = None,
    team_config_store: TeamConfigStore | None = None,
    dlp_guard: DlpEgressGuard | None = None,
    sink: Sink | None = None,
    max_output_tokens_cap: int | None = None,
    strip_inbound_headers_to_upstream: bool | None = None,
    forward_chatgpt_auth: bool | None = None,
    forward_anthropic_auth: bool | None = None,
) -> CorpLlmGuardrail:
    """Assemble a `CorpLlmGuardrail` from config, with optional dep overrides.

    Overrides let the demo inject in-memory backends; when omitted, each backend
    is selected from config (Postgres/Redis when configured, else in-memory) and
    the audit sink from `CORP_AUDIT_SINK` via `get_sink()`.

    The core orchestrator is wrapped in a `ProfileAwareOrchestrator` (D4): a team
    with no `profile_ids` passes through to the core unchanged; a team that selects
    a bundle gets its merged profile applied. The metrics exporter comes from
    `get_exporter()` (B4) — default noop, so the metrics surface is opt-in.

    The active sink is registered in the extension REGISTRY (cached live
    instance) and `validate_api_version` runs here — inside the lazy build, never
    at import — so a version-incompatible extension is refused before the
    guardrail serves any traffic.
    """
    # Unlike max_output_tokens_cap (a call-site-only policy toggle with no
    # corresponding env var), these three are documented as operator-settable
    # cluster config (CORP_LLM_FORWARD_CHATGPT_AUTH / CORP_LLM_FORWARD_ANTHROPIC_AUTH
    # / CORP_LLM_STRIP_INBOUND_HEADERS) — a plain `bool = False` default would
    # silently override the env var in every real deploy, so only resolve from
    # config when the caller left the kwarg unset.
    configured_forward_chatgpt_auth = _flag("CORP_LLM_FORWARD_CHATGPT_AUTH", "0")
    configured_forward_anthropic_auth = _flag("CORP_LLM_FORWARD_ANTHROPIC_AUTH", "0")
    resolved_forward_chatgpt_auth = (
        forward_chatgpt_auth
        if forward_chatgpt_auth is not None
        else configured_forward_chatgpt_auth
    )
    resolved_forward_anthropic_auth = (
        forward_anthropic_auth
        if forward_anthropic_auth is not None
        else configured_forward_anthropic_auth
    )
    # Independent of the chatgpt/anthropic pair: not part of their mutual exclusivity.
    resolved_strip_inbound_headers_to_upstream = (
        strip_inbound_headers_to_upstream
        if strip_inbound_headers_to_upstream is not None
        else _flag("CORP_LLM_STRIP_INBOUND_HEADERS", "0")
    )
    # Same reason as the no-op-sanitizer floor below: settings.validate() covers
    # `config check` only, and the compose/demo boots these bridges ship on skip it.
    # Checked before anything is constructed so a config conflict is what the
    # operator sees, not a downstream failure of a component we should not have
    # started building.
    conflict = forward_auth_conflict(
        chatgpt=resolved_forward_chatgpt_auth, anthropic=resolved_forward_anthropic_auth
    )
    if conflict is not None:
        raise ConfigError([conflict])
    master_key_problem = master_key_conflict(
        master_key=config.get("LITELLM_MASTER_KEY"),
        chatgpt=resolved_forward_chatgpt_auth,
        anthropic=resolved_forward_anthropic_auth,
    )
    if master_key_problem is not None:
        raise ConfigError([master_key_problem])
    _warn_on_disarmed_forward_auth_conflict(
        configured_chatgpt=configured_forward_chatgpt_auth,
        configured_anthropic=configured_forward_anthropic_auth,
        resolved_chatgpt=resolved_forward_chatgpt_auth,
        resolved_anthropic=resolved_forward_anthropic_auth,
    )
    auth = auth_middleware if auth_middleware is not None else make_auth_middleware()
    store = mapping_store if mapping_store is not None else build_mapping_store()
    oracle_enabled = _flag("CORP_LLM_ORACLE_ENABLED")
    # settings.validate() is not called on this path — the Helm initContainer
    # runs full `config check` before pods start, but compose/demo/bare-litellm
    # boots skip it — so the no-op-sanitizer floor is re-checked here. Fires
    # regardless of an explicit `corp_llm` override: a caller-supplied client
    # does not restore the missing local-first floor.
    if not oracle_enabled and not _flag("CORP_LLM_LOCAL_FIRST"):
        raise ConfigError([NO_OP_SANITIZER_MESSAGE])
    if corp_llm is not None:
        client = corp_llm
    elif oracle_enabled:
        client = build_corp_llm_client()
    else:
        client = None
        _log.info("bootstrap oracle_enabled=false — local-first only")
    team_store = team_config_store if team_config_store is not None else build_team_config_store()
    # One exporter instance for the hook: get_exporter() builds a new one per
    # call, and a second instance would emit to a registry nobody scrapes.
    metrics = get_exporter()
    corp_ner = build_corp_ner()
    core = _build_orchestrator(
        client,
        store,
        oracle_enabled=oracle_enabled,
        corp_ner=corp_ner[0] if corp_ner is not None else None,
    )
    orchestrator = _build_profile_wrapper(
        core,
        corp_llm=client,
        mapping_store=store,
        team_store=team_store,
        oracle_enabled=oracle_enabled,
    )
    active_sink = sink if sink is not None else get_sink()
    register_sink(REGISTRY, active_sink, sink_name_for(active_sink))
    if corp_ner is not None:
        register_corp_ner(REGISTRY, corp_ner[1])
    REGISTRY.validate_api_version(EXTENSION_API_VERSION)
    audit_logger = AuditLogger(active_sink, gateway_version=gateway_version())
    return CorpLlmGuardrail(
        orchestrator,
        auth,
        audit_logger,
        max_output_tokens_cap=max_output_tokens_cap,
        strip_inbound_headers_to_upstream=resolved_strip_inbound_headers_to_upstream,
        forward_chatgpt_auth=resolved_forward_chatgpt_auth,
        forward_anthropic_auth=resolved_forward_anthropic_auth,
        dlp_guard=dlp_guard if dlp_guard is not None else _build_dlp_guard(),
        metrics=metrics,
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
