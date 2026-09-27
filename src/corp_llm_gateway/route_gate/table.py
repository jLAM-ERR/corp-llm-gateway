"""The route table: the single place a litellm route is classified.

Every request the gateway serves is looked up here before litellm's router sees
it. A pair the tables do not know is refused (default-deny), so a route litellm
adds in a future bump cannot reach a provider until someone classifies it.

``LITELLM_ROUTE_TABLE`` / ``LITELLM_REGEX_TABLE`` describe routes litellm
registers; ``GATEWAY_ROUTE_TABLE`` describes the routes the gateway itself
serves (``/healthz/*``, ``/metrics``, ``POST /internal/issue-token``) and is exempt
from the source guard.

The litellm tables are generated from litellm 1.101.0's own source with the
collector in ``tests/route_gate/litellm_routes.py`` and these rules, in order:

1. the eight generation spellings whose body the hook rewrites are REWRITTEN;
2. a stored-response route addressed by id is PASSTHROUGH — it sends no
   inbound text;
3. a handler that reaches ``pre_call_hook`` or calls a provider any other way
   (``ast``-visible) is REFUSE: the hook does not rewrite that body;
4. named trees that carry user text without the hook — MCP, agents, RAG,
   search, memory, files, token counting, guardrail echo, spend export — are
   REFUSE;
5. model listing and the three probe paths (``/health/liveliness``,
   ``/health/liveness``, ``/health/readiness``) are PASSTHROUGH — no user text,
   no provider call;
6. everything else is litellm's own management surface — admin, spend,
   login/SSO, public catalogue, UI, index, lazy warm-up and the other
   ``/health/*`` routes — and is REFUSE by design (``docs/security.md`` §14).

A body-carrying PASSTHROUGH row must carry the written ``justification`` that
``tests/route_gate/test_litellm_route_guard.py`` rule (c) re-checks by ``ast``.

A path with a ``{parameter}`` becomes a ``LITELLM_REGEX_TABLE`` row: the table
is matched against the path as received, never against the template. Rows are
ordered longest-static-prefix first so a specific spelling wins over a
catch-all; ``test_table.py`` pins that every row is reachable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

# WEBSOCKET is deliberately absent: handshakes are refused at scope level and
# the table never lists one.
HTTP_METHODS: frozenset[str] = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)

# An extra is matched against the decoded path exactly as received, so a path
# carrying any of these never matches anything — it is an operator typo.
_BAD_EXTRA_PATH_TOKENS: tuple[str, ...] = ("..", "//", "%", "?", "#", "\x00")


class Verdict(Enum):
    PASSTHROUGH = "passthrough"
    REWRITTEN = "rewritten"
    REFUSE = "refuse"


@dataclass(frozen=True)
class Entry:
    """One classified route. ``why`` is printed by ``config check --routes`` and
    by guard failures; ``justification`` is the written no-egress reason a
    body-carrying PASSTHROUGH route must give."""

    verdict: Verdict
    why: str
    justification: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, Verdict):
            raise ValueError(f"route table entry needs a Verdict, got {self.verdict!r}")
        if not self.why.strip():
            raise ValueError("route table entry needs a non-empty why")
        if self.justification is not None and not self.justification.strip():
            raise ValueError("justification must be non-empty when present")
        if self.justification and self.verdict is not Verdict.PASSTHROUGH:
            raise ValueError("justification belongs on PASSTHROUGH entries only")


@dataclass(frozen=True)
class RegexEntry:
    """A route with a path parameter. ``template`` is litellm's own spelling —
    it documents the row and lets the tests prove the row is reachable."""

    method: str
    template: str
    pattern: re.Pattern[str]
    entry: Entry


def _rewritten(why: str) -> Entry:
    return Entry(Verdict.REWRITTEN, why)


def _passthrough(why: str, justification: str | None = None) -> Entry:
    return Entry(Verdict.PASSTHROUGH, why, justification)


def _refuse(why: str) -> Entry:
    return Entry(Verdict.REFUSE, why)


def _anchored(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"\A{pattern}\Z")


_PARAMETER = re.compile(r"\{[^{}]+\}")


def _regex(method: str, template: str, entry: Entry) -> RegexEntry:
    return RegexEntry(method, template, template_matcher(template), entry)


def template_matcher(template: str) -> re.Pattern[str]:
    """Anchored matcher for a FastAPI path template. Public so the litellm guard
    can ask whether one registered template also matches another's path."""
    return _anchored(_pattern(template))


def _pattern(template: str) -> str:
    """FastAPI path template -> anchored pattern body. ``{x:path}`` may span
    segments (that is what the ``:path`` converter means); ``{x}`` may not."""
    out: list[str] = []
    position = 0
    for match in _PARAMETER.finditer(template):
        out.append(re.escape(template[position : match.start()]))
        out.append(".+" if match.group().endswith(":path}") else "[^/]+")
        position = match.end()
    out.append(re.escape(template[position:]))
    return "".join(out)


_WHY_MESSAGES = "Anthropic Messages; the hook rewrites messages/system"
_WHY_CHAT = "chat completions; the hook rewrites messages"
_WHY_RESPONSES = "Responses API; the hook rewrites input"
_WHY_COMPACT = "Responses compact; the hook rewrites input"
_WHY_STORED_RESPONSE = "stored response addressed by id; no inbound user text"

_WHY_NOT_REWRITTEN = "reaches a provider with a body the hook does not rewrite"
_WHY_PROVIDER = "provider-native passthrough; the body leaves as the caller sent it"
_WHY_NO_REWRITE = "hook no-rewrite set (_NON_CHAT_INPUT_CALL_TYPES); a DLP scan is not sanitization"
_WHY_PROMPT = "prompt is never read by the hook, so nothing is rewritten"
_WHY_COUNT = "handler never calls pre_call_hook; clients use usage.input_tokens from the real turn"
_WHY_NO_HOOK = "carries user text to a provider or a store without reaching the hook"
_WHY_OPENAI_SHADOWED = (
    "the hook-less /openai/{endpoint:path} raw passthrough matches this path too; it loses only "
    "because litellm includes its router later, so the rewrite rests on include order no bump "
    "guarantees. No client here uses the /openai/ spelling"
)
_WHY_CURSOR = (
    "cursor-compat chat spelling; it does reach the hook, but no client here uses it and "
    "default-deny keeps the admitted set minimal"
)
_WHY_GUARDRAIL_ECHO = "runs caller text through a guardrail or policy engine without the hook"
_WHY_SEARCH = "query text goes to a search backend without the hook"
_WHY_RAG = "ingested or queried text the hook never reads"
_WHY_MEMORY = "stores caller text for later replay; the hook never reads it"
_WHY_AGENT = "agent / workflow turn text without the hook"
_WHY_MCP = "MCP tree: tool arguments and OAuth credential flows, no hook"
_WHY_PROVIDER_STORE = "file or index content held at the provider; the hook never reads it"
_WHY_SPEND_EXPORT = "ships spend rows, which can carry stored prompts, to a third-party SaaS"

_WHY_MODELS = "model list or metadata; no user text"

# Kept: GET|OPTIONS /health/liveliness, /health/liveness, /health/readiness. In
# litellm 1.101.0 readiness (`_health_endpoints.py:1740-1760`) reads the shutdown
# flag and, when litellm has a database, a Prisma ping cached for 15 s and bounded
# at 4 s (:1410-1458, :1720-1737). It calls no provider; neither does the opt-in
# `allow_public_health_readiness_details` branch (:1585-1667). Liveness
# (:1847-1865) and the OPTIONS handlers (:1868-1901) touch nothing.
_WHY_HEALTH = "litellm probe; calls no provider, bounded cost, no user text"

# The management surface is refused by design, not gated by network: identity is
# X-Corp-Auth in the gateway's own store and observability is Langfuse, so nothing
# litellm manages is needed at runtime.
_WHY_ADMIN_OFF = (
    "litellm admin surface refused by design: identity is X-Corp-Auth in the gateway's "
    "own store, observability is Langfuse; see docs/security.md §14"
)
_WHY_SPEND_OFF = (
    "litellm spend analytics refused by design: observability is Langfuse; see docs/security.md §14"
)
_WHY_AUTH_OFF = (
    "litellm login / SSO / invitation flow refused by design: identity is X-Corp-Auth, "
    "issued by the gateway from Keycloak; see docs/security.md §14"
)
_WHY_PUBLIC_OFF = (
    "litellm's unauthenticated public catalogue refused by design; see docs/security.md §14"
)
_WHY_UI_OFF = "litellm admin UI asset or setting refused by design; see docs/security.md §14"
_WHY_HOME_OFF = "litellm's own index and route list refused by design; see docs/security.md §14"
_WHY_LAZY_OFF = (
    "litellm lazy-router warm-up refused by design: it loads admin routers; "
    "see docs/security.md §14"
)
_WHY_HEALTH_CHECK_OFF = (
    "runs model health checks against providers when background checks are off "
    "(_health_endpoints.py:1036-1048); see docs/security.md §14"
)
_WHY_HEALTH_DRAIN_OFF = (
    "flips litellm's process-wide shutdown state (_health_endpoints.py:1799-1843); "
    "see docs/security.md §14"
)
_WHY_HEALTH_OPS_OFF = "litellm operator diagnostics, not a probe; see docs/security.md §14"

# Rule (c): every PASSTHROUGH route that carries a body says here why it cannot
# put user text on the wire. The guard re-checks each one against the handler's
# own `ast` — a written excuse never overrides a provider call it can see.
_JUST_STORED_RESPONSE = "cancels a stored response by id; no request body reaches a provider"

LITELLM_ROUTE_TABLE: dict[tuple[str, str], Entry] = {
    ("DELETE", "/cloudzero/delete"): _refuse(_WHY_ADMIN_OFF),
    ("DELETE", "/config/pass_through_endpoint"): _refuse(_WHY_ADMIN_OFF),
    ("DELETE", "/config_overrides/cyberark"): _refuse(_WHY_ADMIN_OFF),
    ("DELETE", "/config_overrides/hashicorp_vault"): _refuse(_WHY_ADMIN_OFF),
    ("DELETE", "/mcp"): _refuse(_WHY_MCP),
    ("DELETE", "/organization/delete"): _refuse(_WHY_ADMIN_OFF),
    ("DELETE", "/organization/member_delete"): _refuse(_WHY_ADMIN_OFF),
    ("DELETE", "/schedule/anthropic_beta_headers_reload"): _refuse(_WHY_ADMIN_OFF),
    ("DELETE", "/schedule/model_cost_map_reload"): _refuse(_WHY_ADMIN_OFF),
    ("DELETE", "/vantage/delete"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/"): _refuse(_WHY_HOME_OFF),
    ("GET", "/.well-known/jwks.json"): _refuse(_WHY_MCP),
    ("GET", "/.well-known/litellm-cli-auth"): _refuse(_WHY_MCP),
    ("GET", "/.well-known/litellm-ui-config"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/.well-known/oauth-authorization-server"): _refuse(_WHY_MCP),
    ("GET", "/.well-known/oauth-protected-resource"): _refuse(_WHY_MCP),
    ("GET", "/.well-known/openid-configuration"): _refuse(_WHY_MCP),
    ("GET", "/access_group/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/active/callbacks"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/adaptive_router/state"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/agent/daily/activity"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/alerting/settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/api/plugins"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/api/plugins/auth-token"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/assistants"): _refuse(_WHY_PROVIDER),
    ("GET", "/authorize"): _refuse(_WHY_MCP),
    ("GET", "/auto_router/benchmarks"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/auto_router/classifier/default_prompt"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/auto_router/shadow_eval"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/batches"): _refuse(_WHY_PROVIDER),
    ("GET", "/budget/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/budget/settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/cache/ping"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/cache/redis/info"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/cache/settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/callback"): _refuse(_WHY_MCP),
    ("GET", "/callbacks/configs"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/callbacks/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/claude-code/marketplace.json"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/claude-code/plugins"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/cloudzero/settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/config/block_requests_for_models_without_pricing"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/config/cost_discount_config"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/config/cost_margin_config"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/config/field/info"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/config/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/config/pass_through_endpoint"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/config/pass_through_endpoints/settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/config/yaml"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/config_overrides/cyberark"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/config_overrides/hashicorp_vault"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/containers"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/coordination_redis/settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/credentials"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/credentials/migrate-encryption/check"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/cursor/models"): _passthrough(_WHY_MODELS),
    ("GET", "/cursor/v1/models"): _passthrough(_WHY_MODELS),
    ("GET", "/customer/daily/activity"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/customer/info"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/customer/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/debug/asyncio-tasks"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/debug/memory/details"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/debug/memory/summary"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/enabled"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/end_user/daily/activity"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/end_user/info"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/end_user/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/fallback/login"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/files"): _refuse(_WHY_PROVIDER_STORE),
    ("GET", "/fine_tuning/jobs"): _refuse(_WHY_PROVIDER),
    ("GET", "/gateway/daily/activity"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/get/allowed_ips"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/get/config/callbacks"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/get/default_team_settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/get/internal_user_settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/get/mcp_semantic_filter_settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/get/mcp_tool_search_settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/get/sso_settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/get/ui_settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/get/ui_theme_settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/get/user_banner"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/get_favicon"): _refuse(_WHY_UI_OFF),
    ("GET", "/get_image"): _refuse(_WHY_UI_OFF),
    ("GET", "/get_logo_url"): _refuse(_WHY_UI_OFF),
    ("GET", "/global/activity"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/activity/cache_hits"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/activity/exceptions"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/activity/exceptions/deployment"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/activity/model"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/all_end_users"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/spend"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/spend/all_tag_names"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/spend/keys"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/spend/logs"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/spend/models"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/spend/provider"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/spend/report"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/spend/tags"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/global/spend/teams"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/guardrails/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/guardrails/submissions"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/guardrails/ui/add_guardrail_settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/guardrails/ui/major_airlines"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/guardrails/ui/provider_specific_params"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/guardrails/usage/logs"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/guardrails/usage/overview"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/health"): _refuse(_WHY_HEALTH_CHECK_OFF),
    ("GET", "/health/backlog"): _refuse(_WHY_HEALTH_OPS_OFF),
    ("GET", "/health/drain"): _refuse(_WHY_HEALTH_DRAIN_OFF),
    ("GET", "/health/history"): _refuse(_WHY_HEALTH_OPS_OFF),
    ("GET", "/health/latest"): _refuse(_WHY_HEALTH_OPS_OFF),
    ("GET", "/health/license"): _refuse(_WHY_HEALTH_OPS_OFF),
    ("GET", "/health/liveliness"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/liveness"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/readiness"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/readiness/details"): _refuse(_WHY_HEALTH_OPS_OFF),
    ("GET", "/health/services"): _refuse(_WHY_PROVIDER),
    ("GET", "/health/shared-status"): _refuse(_WHY_HEALTH_OPS_OFF),
    ("GET", "/invitation/info"): _refuse(_WHY_AUTH_OFF),
    ("GET", "/jwt/key/mapping/info"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/jwt/key/mapping/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/key/aliases"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/key/info"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/key/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/key/spend/report"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/litellm/.well-known/litellm-ui-config"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/management/v1/budgets"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/management/v1/spend_logs/end_users"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/management/v1/spend_logs/users"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/mcp"): _refuse(_WHY_MCP),
    ("GET", "/mcp-rest/tools/list"): _refuse(_WHY_MCP),
    ("GET", "/memory-usage"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/memory-usage-in-mem-cache"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/memory-usage-in-mem-cache-items"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/model/cost_map/source"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/model/deprecations"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/model/info"): _passthrough(_WHY_MODELS),
    ("GET", "/model/metrics"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/model/metrics/exceptions"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/model/metrics/slow_responses"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/model/settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/model/streaming_metrics"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/model_group/info"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/models"): _passthrough(_WHY_MODELS),
    ("GET", "/onboarding/get_token"): _refuse(_WHY_AUTH_OFF),
    ("GET", "/organization/daily/activity"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/organization/info"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/organization/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/organization/spend/report"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/otel-spans"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/policies/attachments/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/policies/compare"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/policies/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/policies/usage/overview"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/policy/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/policy/templates"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/prompts/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/provider/budgets"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/public/agent_hub"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/agents/fields"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/autorouter_presets"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/complexity_router/scorer_defaults"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/endpoints"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/litellm_blog_posts"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/litellm_model_cost_map"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/mcp_hub"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/model_hub"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/model_hub/info"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/providers"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/providers/fields"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/skill_hub"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/public/v1/model_hub"): _refuse(_WHY_PUBLIC_OFF),
    ("GET", "/router/fields"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/router/settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/routes"): _refuse(_WHY_HOME_OFF),
    ("GET", "/schedule/anthropic_beta_headers_reload/status"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/schedule/model_cost_map_reload/status"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/scim/v2"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/scim/v2/"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/scim/v2/Groups"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/scim/v2/ResourceTypes"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/scim/v2/Schemas"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/scim/v2/ServiceProviderConfig"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/scim/v2/Users"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/scim/v2/placeholders"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/search/tools"): _refuse(_WHY_SEARCH),
    ("GET", "/search_tools/list"): _refuse(_WHY_SEARCH),
    ("GET", "/search_tools/ui/available_providers"): _refuse(_WHY_SEARCH),
    ("GET", "/settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/spend/keys"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/spend/logs"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/spend/logs/session/ui"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/spend/logs/ui"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/spend/logs/v2"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/spend/tags"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/spend/users"): _refuse(_WHY_SPEND_OFF),
    ("GET", "/sso/callback"): _refuse(_WHY_AUTH_OFF),
    ("GET", "/sso/debug/callback"): _refuse(_WHY_AUTH_OFF),
    ("GET", "/sso/debug/login"): _refuse(_WHY_AUTH_OFF),
    ("GET", "/sso/get/ui_settings"): _refuse(_WHY_AUTH_OFF),
    ("GET", "/sso/key/generate"): _refuse(_WHY_AUTH_OFF),
    ("GET", "/sso/readiness"): _refuse(_WHY_AUTH_OFF),
    ("GET", "/sso/saml/login"): _refuse(_WHY_AUTH_OFF),
    ("GET", "/sso/saml/metadata"): _refuse(_WHY_AUTH_OFF),
    ("GET", "/tag/daily/activity"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/tag/dau"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/tag/distinct"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/tag/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/tag/mau"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/tag/summary"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/tag/user-agent/per-user-analytics"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/tag/wau"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/team/available"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/team/daily/activity"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/team/daily/activity/aggregated"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/team/filter/ui"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/team/info"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/team/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/team/metadata_schema"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/team/permissions_list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/team/spend/by_user"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/team/spend/report"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/test"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/user/available_roles"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/user/daily/activity"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/user/daily/activity/aggregated"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/user/filter/ui"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/user/info"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/user/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/user/spend/report"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/utils/supported_openai_params"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/access_group"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/agents"): _refuse(_WHY_AGENT),
    ("GET", "/v1/assistants"): _refuse(_WHY_PROVIDER),
    ("GET", "/v1/batches"): _refuse(_WHY_PROVIDER),
    ("GET", "/v1/containers"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/v1/evals"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/v1/files"): _refuse(_WHY_PROVIDER_STORE),
    ("GET", "/v1/fine_tuning/jobs"): _refuse(_WHY_PROVIDER),
    ("GET", "/v1/indexes"): _refuse(_WHY_PROVIDER_STORE),
    ("GET", "/v1/mcp/access_groups"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/mcp/discover"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/mcp/network/client-ip"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/mcp/oauth/authorize"): _refuse(_WHY_MCP),
    ("GET", "/v1/mcp/openapi-registry"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/mcp/registry.json"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/mcp/server"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/mcp/server/health"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/mcp/server/submissions"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/mcp/tools"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/mcp/toolset"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/mcp/user-credentials"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/mcp/user-env-vars/status"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/memory"): _refuse(_WHY_MEMORY),
    ("GET", "/v1/model/deprecations"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/model/info"): _passthrough(_WHY_MODELS),
    ("GET", "/v1/models"): _passthrough(_WHY_MODELS),
    ("GET", "/v1/search/tools"): _refuse(_WHY_SEARCH),
    ("GET", "/v1/skills"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/v1/tool/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/tool/policy/options"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/tool/spend"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/unified_access_group"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/vector_store/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v1/vector_stores"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/v1/videos"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/v1/workflows/runs"): _refuse(_WHY_AGENT),
    ("GET", "/v1beta/agents"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/v2/guardrails/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v2/model/info"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v2/team/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/v2/user/info"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/vantage/settings"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/vector_store/list"): _refuse(_WHY_ADMIN_OFF),
    ("GET", "/vector_stores"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/videos"): _refuse(_WHY_NOT_REWRITTEN),
    ("OPTIONS", "/health/liveliness"): _passthrough(_WHY_HEALTH),
    ("OPTIONS", "/health/liveness"): _passthrough(_WHY_HEALTH),
    ("OPTIONS", "/health/readiness"): _passthrough(_WHY_HEALTH),
    ("OPTIONS", "/mcp"): _refuse(_WHY_MCP),
    ("PATCH", "/config/block_requests_for_models_without_pricing"): _refuse(_WHY_ADMIN_OFF),
    ("PATCH", "/config/cost_discount_config"): _refuse(_WHY_ADMIN_OFF),
    ("PATCH", "/config/cost_margin_config"): _refuse(_WHY_ADMIN_OFF),
    ("PATCH", "/mcp"): _refuse(_WHY_MCP),
    ("PATCH", "/organization/member_update"): _refuse(_WHY_ADMIN_OFF),
    ("PATCH", "/organization/update"): _refuse(_WHY_ADMIN_OFF),
    ("PATCH", "/update/default_team_settings"): _refuse(_WHY_ADMIN_OFF),
    ("PATCH", "/update/internal_user_settings"): _refuse(_WHY_ADMIN_OFF),
    ("PATCH", "/update/mcp_semantic_filter_settings"): _refuse(_WHY_ADMIN_OFF),
    ("PATCH", "/update/mcp_tool_search_settings"): _refuse(_WHY_ADMIN_OFF),
    ("PATCH", "/update/sso_settings"): _refuse(_WHY_ADMIN_OFF),
    ("PATCH", "/update/ui_settings"): _refuse(_WHY_ADMIN_OFF),
    ("PATCH", "/update/ui_theme_settings"): _refuse(_WHY_ADMIN_OFF),
    ("PATCH", "/update/user_banner"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/access_group/new"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/add/allowed_ip"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/api/event_logging/batch"): _refuse(_WHY_NO_HOOK),
    ("POST", "/apply_guardrail"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/assistants"): _refuse(_WHY_PROVIDER),
    ("POST", "/audio/speech"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/audio/transcriptions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/authorize/complete"): _refuse(_WHY_MCP),
    ("POST", "/auto_router/classifier/default_prompt"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/auto_router/shadow_eval/start"): _refuse(_WHY_NO_HOOK),
    ("POST", "/auto_router/test_routing"): _refuse(_WHY_NO_HOOK),
    ("POST", "/auto_router/validate_complexity_router_config"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/batches"): _refuse(_WHY_PROVIDER),
    ("POST", "/budget/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/budget/info"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/budget/new"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/budget/update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/cache/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/cache/flushall"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/cache/settings"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/cache/settings/test"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/chat/completions"): _rewritten(_WHY_CHAT),
    ("POST", "/claude-code/plugins"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/cloudzero/dry-run"): _refuse(_WHY_SPEND_EXPORT),
    ("POST", "/cloudzero/export"): _refuse(_WHY_SPEND_EXPORT),
    ("POST", "/cloudzero/init"): _refuse(_WHY_SPEND_EXPORT),
    ("POST", "/completions"): _refuse(_WHY_PROMPT),
    ("POST", "/compliance/eu-ai-act"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/compliance/gdpr"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/comprehendmedical"): _refuse(_WHY_PROVIDER),
    ("POST", "/config/callback/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/config/field/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/config/field/update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/config/pass_through_endpoint"): _refuse(_WHY_PROVIDER),
    ("POST", "/config/update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/config_overrides/cyberark"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/config_overrides/cyberark/test_connection"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/config_overrides/hashicorp_vault"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/config_overrides/hashicorp_vault/test_connection"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/containers"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/coordination_redis/settings"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/coordination_redis/settings/test"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/cost/estimate"): _refuse(_WHY_NO_HOOK),
    ("POST", "/credentials"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/credentials/migrate-encryption"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/cursor/chat/completions"): _refuse(_WHY_CURSOR),
    ("POST", "/customer/block"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/customer/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/customer/new"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/customer/unblock"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/customer/update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/debug/memory/gc/configure"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/delete/allowed_ip"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/embeddings"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/end_user/block"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/end_user/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/end_user/new"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/end_user/unblock"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/end_user/update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/fallback"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/files"): _refuse(_WHY_PROVIDER_STORE),
    ("POST", "/fine_tuning/jobs"): _refuse(_WHY_PROVIDER),
    ("POST", "/global/spend/end_users"): _refuse(_WHY_SPEND_OFF),
    ("POST", "/global/spend/refresh"): _refuse(_WHY_SPEND_OFF),
    ("POST", "/global/spend/reset"): _refuse(_WHY_SPEND_OFF),
    ("POST", "/guardrails"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/guardrails/apply_guardrail"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/guardrails/register"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/guardrails/test_custom_code"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/guardrails/validate_blocked_words_file"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/health/test_connection"): _refuse(_WHY_NO_HOOK),
    ("POST", "/images/edits"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/images/generations"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/interactions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/introspect"): _refuse(_WHY_MCP),
    ("POST", "/invitation/delete"): _refuse(_WHY_AUTH_OFF),
    ("POST", "/invitation/new"): _refuse(_WHY_AUTH_OFF),
    ("POST", "/invitation/update"): _refuse(_WHY_AUTH_OFF),
    ("POST", "/jwt/key/mapping/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/jwt/key/mapping/new"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/jwt/key/mapping/update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/key/block"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/key/bulk_update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/key/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/key/generate"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/key/health"): _refuse(_WHY_PROVIDER),
    ("POST", "/key/regenerate"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/key/service-account/generate"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/key/unblock"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/key/update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/login"): _refuse(_WHY_AUTH_OFF),
    ("POST", "/mcp"): _refuse(_WHY_MCP),
    ("POST", "/mcp-rest/test/connection"): _refuse(_WHY_MCP),
    ("POST", "/mcp-rest/test/tools/list"): _refuse(_WHY_MCP),
    ("POST", "/mcp-rest/tools/call"): _refuse(_WHY_MCP),
    ("POST", "/model/block"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/model/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/model/new"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/model/unblock"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/model/update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/model_group/make_public"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/model_hub/update_useful_links"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/moderations"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/ocr"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/onboarding/claim_token"): _refuse(_WHY_AUTH_OFF),
    ("POST", "/openai/v1/realtime/calls"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/openai/v1/realtime/client_secrets"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/openai/v1/realtime/transcription_sessions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/openai/v1/responses"): _refuse(_WHY_OPENAI_SHADOWED),
    ("POST", "/openai/v1/responses/compact"): _refuse(_WHY_OPENAI_SHADOWED),
    ("POST", "/openai/v1/responses/input_tokens"): _refuse(_WHY_COUNT),
    ("POST", "/organization/info"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/organization/member_add"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/organization/new"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/policies"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/policies/attachments"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/policies/attachments/estimate-impact"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/policies/resolve"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/policies/test-pipeline"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/policy/templates/enrich"): _refuse(_WHY_PROVIDER),
    ("POST", "/policy/templates/enrich/stream"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/policy/templates/suggest"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/policy/templates/test"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/policy/test"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/policy/validate"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/prompts"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/prompts/test"): _refuse(_WHY_NO_HOOK),
    ("POST", "/queue/chat/completions"): _refuse(_WHY_NO_HOOK),
    ("POST", "/rag/ingest"): _refuse(_WHY_RAG),
    ("POST", "/rag/query"): _refuse(_WHY_RAG),
    ("POST", "/realtime/calls"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/realtime/client_secrets"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/realtime/transcription_sessions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/register"): _refuse(_WHY_MCP),
    ("POST", "/reload/anthropic_beta_headers"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/reload/model_cost_map"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/rerank"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/responses"): _rewritten(_WHY_RESPONSES),
    ("POST", "/responses/compact"): _rewritten(_WHY_COMPACT),
    ("POST", "/responses/input_tokens"): _refuse(_WHY_COUNT),
    ("POST", "/revoke"): _refuse(_WHY_MCP),
    ("POST", "/schedule/anthropic_beta_headers_reload"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/schedule/model_cost_map_reload"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/scim/v2/Groups"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/scim/v2/Users"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/search"): _refuse(_WHY_SEARCH),
    ("POST", "/search_tools"): _refuse(_WHY_SEARCH),
    ("POST", "/search_tools/test_connection"): _refuse(_WHY_NO_HOOK),
    ("POST", "/spend/calculate"): _refuse(_WHY_NO_HOOK),
    ("POST", "/sso/cli/start"): _refuse(_WHY_AUTH_OFF),
    ("POST", "/sso/saml/callback"): _refuse(_WHY_AUTH_OFF),
    ("POST", "/tag/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/tag/info"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/tag/new"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/tag/update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/block"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/bulk_member_add"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/key/bulk_update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/member_add"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/member_delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/member_update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/model/add"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/model/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/new"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/permissions_bulk_update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/permissions_update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/unblock"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/team/update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/threads"): _refuse(_WHY_PROVIDER),
    ("POST", "/token"): _refuse(_WHY_MCP),
    ("POST", "/upload/logo"): _refuse(_WHY_UI_OFF),
    ("POST", "/usage/ai/chat"): _refuse(_WHY_NO_HOOK),
    ("POST", "/user/bulk_update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/user/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/user/new"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/user/update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/utils/dotprompt_json_converter"): _refuse(_WHY_NO_HOOK),
    ("POST", "/utils/test_policies_and_guardrails"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/utils/token_counter"): _refuse(_WHY_COUNT),
    ("POST", "/utils/transform_request"): _refuse(_WHY_NO_HOOK),
    ("POST", "/v1/a2a/discover"): _refuse(_WHY_AGENT),
    ("POST", "/v1/access_group"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/v1/agents"): _refuse(_WHY_AGENT),
    ("POST", "/v1/agents/make_public"): _refuse(_WHY_AGENT),
    ("POST", "/v1/assistants"): _refuse(_WHY_PROVIDER),
    ("POST", "/v1/audio/speech"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/v1/audio/transcriptions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/batches"): _refuse(_WHY_PROVIDER),
    ("POST", "/v1/chat/completions"): _rewritten(_WHY_CHAT),
    ("POST", "/v1/completions"): _refuse(_WHY_PROMPT),
    ("POST", "/v1/containers"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/embeddings"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/v1/evals"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/files"): _refuse(_WHY_PROVIDER_STORE),
    ("POST", "/v1/fine_tuning/jobs"): _refuse(_WHY_PROVIDER),
    ("POST", "/v1/images/edits"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/images/generations"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/indexes"): _refuse(_WHY_PROVIDER_STORE),
    ("POST", "/v1/mcp/make_public"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/v1/mcp/oauth/authorize"): _refuse(_WHY_MCP),
    ("POST", "/v1/mcp/oauth/token"): _refuse(_WHY_MCP),
    ("POST", "/v1/mcp/server"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/v1/mcp/server/import"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/v1/mcp/server/oauth/session"): _refuse(_WHY_MCP),
    ("POST", "/v1/mcp/server/register"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/v1/mcp/toolset"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/v1/memory"): _refuse(_WHY_MEMORY),
    ("POST", "/v1/messages"): _rewritten(_WHY_MESSAGES),
    ("POST", "/v1/messages/count_tokens"): _refuse(_WHY_COUNT),
    ("POST", "/v1/moderations"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/v1/ocr"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/rag/ingest"): _refuse(_WHY_RAG),
    ("POST", "/v1/rag/query"): _refuse(_WHY_RAG),
    ("POST", "/v1/realtime/calls"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/realtime/client_secrets"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/realtime/transcription_sessions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/rerank"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/responses"): _rewritten(_WHY_RESPONSES),
    ("POST", "/v1/responses/compact"): _rewritten(_WHY_COMPACT),
    ("POST", "/v1/responses/input_tokens"): _refuse(_WHY_COUNT),
    ("POST", "/v1/rust_control_plane/logs"): _refuse(_WHY_NO_HOOK),
    ("POST", "/v1/search"): _refuse(_WHY_SEARCH),
    ("POST", "/v1/skills"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/threads"): _refuse(_WHY_PROVIDER),
    ("POST", "/v1/tool/policy"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/v1/unified_access_group"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/v1/vector_stores"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/videos"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/videos/characters"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/videos/edits"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/videos/extensions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/workflows/runs"): _refuse(_WHY_AGENT),
    ("POST", "/v1beta/agents"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1beta/interactions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v2/key/info"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/v2/login"): _refuse(_WHY_AUTH_OFF),
    ("POST", "/v2/rerank"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v3/login"): _refuse(_WHY_AUTH_OFF),
    ("POST", "/v3/login/exchange"): _refuse(_WHY_AUTH_OFF),
    ("POST", "/vantage/dry-run"): _refuse(_WHY_SPEND_EXPORT),
    ("POST", "/vantage/export"): _refuse(_WHY_SPEND_EXPORT),
    ("POST", "/vantage/init"): _refuse(_WHY_SPEND_EXPORT),
    ("POST", "/vector_store/delete"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/vector_store/info"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/vector_store/new"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/vector_store/update"): _refuse(_WHY_ADMIN_OFF),
    ("POST", "/vector_stores"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/videos"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/videos/characters"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/videos/edits"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/videos/extensions"): _refuse(_WHY_NOT_REWRITTEN),
    ("PUT", "/cloudzero/settings"): _refuse(_WHY_ADMIN_OFF),
    ("PUT", "/mcp"): _refuse(_WHY_MCP),
    ("PUT", "/v1/mcp/server"): _refuse(_WHY_ADMIN_OFF),
    ("PUT", "/v1/mcp/toolset"): _refuse(_WHY_ADMIN_OFF),
    ("PUT", "/vantage/settings"): _refuse(_WHY_ADMIN_OFF),
}

LITELLM_REGEX_TABLE: tuple[RegexEntry, ...] = (
    _regex(
        "GET", "/.well-known/oauth-authorization-server/{mcp_server_name}/mcp", _refuse(_WHY_MCP)
    ),
    _regex("GET", "/config/pass_through_endpoint/team/{team_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/config/pass_through_endpoint/{endpoint_id}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/guardrails/ui/category_yaml/{category_name}", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/v1/unified_access_group/{access_group_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/v1/unified_access_group/{access_group_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("PUT", "/v1/unified_access_group/{access_group_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/guardrails/usage/detail/{guardrail_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/auto_router/shadow_eval/{job_id}/stop", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/auto_router/shadow_eval/{job_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex(
        "POST",
        "/guardrails/submissions/{guardrail_id}/approve",
        _refuse(_WHY_ADMIN_OFF),
    ),
    _regex(
        "POST",
        "/guardrails/submissions/{guardrail_id}/reject",
        _refuse(_WHY_ADMIN_OFF),
    ),
    _regex("GET", "/guardrails/submissions/{guardrail_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/scim/v2/ResourceTypes/{resource_type_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/policies/attachments/{attachment_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/policies/attachments/{attachment_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/scim/v2/placeholders/{user_id}/merge", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/v1/videos/characters/{character_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/credentials/by_model/{model_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/v1/fine_tuning/jobs/{fine_tuning_job_id:path}/cancel", _refuse(_WHY_PROVIDER)),
    _regex(
        "GET", "/openai/v1/responses/{response_id}/input_items", _passthrough(_WHY_STORED_RESPONSE)
    ),
    _regex("GET", "/v1/fine_tuning/jobs/{fine_tuning_job_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/v1beta/interactions/{interaction_id}/cancel", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/credentials/by_name/{credential_name:path}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/v1/mcp/server/oauth/{server_id}/authorize", _refuse(_WHY_MCP)),
    _regex("POST", "/claude-code/plugins/{plugin_name}/disable", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/claude-code/plugins/{plugin_name}/enable", _refuse(_WHY_ADMIN_OFF)),
    _regex(
        "POST",
        "/openai/v1/responses/{response_id}/cancel",
        _passthrough(_WHY_STORED_RESPONSE, _JUST_STORED_RESPONSE),
    ),
    _regex("POST", "/v1/mcp/server/oauth/{server_id}/register", _refuse(_WHY_MCP)),
    _regex("POST", "/v1/mcp/server/oauth/{server_id}/token", _refuse(_WHY_MCP)),
    _regex("DELETE", "/v1beta/interactions/{interaction_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1beta/interactions/{interaction_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/vertex_ai/discovery/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/vertex_ai/discovery/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/vertex_ai/discovery/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/vertex_ai/discovery/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/vertex_ai/discovery/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/claude-code/plugins/{plugin_name}", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/openai/v1/responses/{response_id}", _passthrough(_WHY_STORED_RESPONSE)),
    _regex("GET", "/claude-code/plugins/{plugin_name}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/openai/v1/responses/{response_id}", _passthrough(_WHY_STORED_RESPONSE)),
    _regex("PUT", "/claude-code/plugins/{plugin_name}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/public/v1/model_hub/{facet}", _refuse(_WHY_PUBLIC_OFF)),
    _regex(
        "POST", "/openai/deployments/{model:path}/images/generations", _refuse(_WHY_NOT_REWRITTEN)
    ),
    _regex(
        "POST",
        "/openai/deployments/{model:path}/chat/completions",
        _refuse(_WHY_OPENAI_SHADOWED),
    ),
    _regex("POST", "/openai/deployments/{model:path}/images/edits", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/openai/deployments/{model:path}/completions", _refuse(_WHY_PROMPT)),
    _regex("POST", "/openai/deployments/{model:path}/embeddings", _refuse(_WHY_NO_REWRITE)),
    _regex("DELETE", "/openai_passthrough/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/openai_passthrough/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/openai_passthrough/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/openai_passthrough/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/openai_passthrough/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/v1/workflows/runs/{run_id}/messages", _refuse(_WHY_AGENT)),
    _regex("POST", "/v1/workflows/runs/{run_id}/messages", _refuse(_WHY_AGENT)),
    _regex("GET", "/v1/workflows/runs/{run_id}/events", _refuse(_WHY_AGENT)),
    _regex("POST", "/v1/workflows/runs/{run_id}/events", _refuse(_WHY_AGENT)),
    _regex("GET", "/videos/characters/{character_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/comprehendmedical/{operation}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/v1/workflows/runs/{run_id}", _refuse(_WHY_AGENT)),
    _regex("PATCH", "/v1/workflows/runs/{run_id}", _refuse(_WHY_AGENT)),
    _regex(
        "GET",
        "/v1/vector_stores/{vector_store_id}/files/{file_id}/content",
        _refuse(_WHY_NOT_REWRITTEN),
    ),
    _regex(
        "DELETE", "/v1/vector_stores/{vector_store_id}/files/{file_id}", _refuse(_WHY_NOT_REWRITTEN)
    ),
    _regex(
        "GET", "/v1/vector_stores/{vector_store_id}/files/{file_id}", _refuse(_WHY_NOT_REWRITTEN)
    ),
    _regex(
        "POST", "/v1/vector_stores/{vector_store_id}/files/{file_id}", _refuse(_WHY_NOT_REWRITTEN)
    ),
    _regex("POST", "/fine_tuning/jobs/{fine_tuning_job_id:path}/cancel", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/v1/vector_stores/{vector_store_id:path}/search", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/fine_tuning/jobs/{fine_tuning_job_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/v1/vector_stores/{vector_store_id}/files", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/v1/vector_stores/{vector_store_id}/files", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/v1/vector_stores/{vector_store_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1/vector_stores/{vector_store_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/v1/vector_stores/{vector_store_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/sso/cli/complete/{login_id}", _refuse(_WHY_AUTH_OFF)),
    _regex("DELETE", "/v1/access_group/{access_group_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/v1/access_group/{access_group_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("PATCH", "/v2/organization/{organization_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("PUT", "/v1/access_group/{access_group_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/scim/v2/Schemas/{schema_id:path}", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/v1/mcp/toolset/{toolset_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/v1/mcp/toolset/{toolset_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/scim/v2/Groups/{group_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/scim/v2/Groups/{group_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("PATCH", "/scim/v2/Groups/{group_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("PUT", "/scim/v2/Groups/{group_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex(
        "GET",
        "/vector_stores/{vector_store_id}/files/{file_id}/content",
        _refuse(_WHY_NOT_REWRITTEN),
    ),
    _regex(
        "GET", "/v1/mcp/server/{server_id}/oauth-user-credential/status", _refuse(_WHY_ADMIN_OFF)
    ),
    _regex(
        "POST",
        "/v1beta/models/{model_name:path}:streamGenerateContent",
        _refuse(_WHY_NOT_REWRITTEN),
    ),
    _regex("DELETE", "/v1/mcp/server/{server_id}/oauth-user-credential", _refuse(_WHY_ADMIN_OFF)),
    _regex(
        "DELETE", "/vector_stores/{vector_store_id}/files/{file_id}", _refuse(_WHY_NOT_REWRITTEN)
    ),
    _regex("GET", "/vector_stores/{vector_store_id}/files/{file_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex(
        "POST",
        "/v1/mcp/server/{server_id}/oauth-user-credential",
        _refuse(_WHY_ADMIN_OFF),
    ),
    _regex("POST", "/v1beta/models/{model_name:path}:generateContent", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/vector_stores/{vector_store_id}/files/{file_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/v1beta/models/{model_name:path}:countTokens", _refuse(_WHY_COUNT)),
    _regex("POST", "/vector_stores/{vector_store_id:path}/search", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/v1/mcp/server/{server_id}/user-credential", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/v1/mcp/server/{server_id}/user-credential", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/policies/name/{policy_name}/all-versions", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/v1/mcp/server/{server_id}/user-env-vars", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/v1/mcp/server/{server_id}/user-env-vars", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/v1/mcp/server/{server_id}/user-env-vars", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/vector_stores/{vector_store_id}/files", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/vector_stores/{vector_store_id}/files", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/policies/name/{policy_name}/versions", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/policies/name/{policy_name}/versions", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/v1/assistants/{assistant_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/v1/mcp/server/{server_id}/approve", _refuse(_WHY_ADMIN_OFF)),
    _regex("PUT", "/v1/mcp/server/{server_id}/reject", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/vector_stores/{vector_store_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/vector_stores/{vector_store_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/vector_stores/{vector_store_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/eu.assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/eu.assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/v1beta/agents/{name}/versions", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("PATCH", "/eu.assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/eu.assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/eu.assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/v1/containers/{container_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1/containers/{container_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/spend/logs/ui/{request_id}", _refuse(_WHY_SPEND_OFF)),
    _regex("DELETE", "/v1/mcp/server/{server_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/v1/mcp/server/{server_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/scim/v2/Users/{user_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/scim/v2/Users/{user_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("PATCH", "/scim/v2/Users/{user_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("PUT", "/scim/v2/Users/{user_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/v1beta/agents/{name}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1beta/agents/{name}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/plugin-proxy/{plugin_name}/{path:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/plugin-proxy/{plugin_name}/{path:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/v1/responses/{response_id}/input_items", _passthrough(_WHY_STORED_RESPONSE)),
    _regex("OPTIONS", "/plugin-proxy/{plugin_name}/{path:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/plugin-proxy/{plugin_name}/{path:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/plugin-proxy/{plugin_name}/{path:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/plugin-proxy/{plugin_name}/{path:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/interactions/{interaction_id}/cancel", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/access_group/{access_group}/budget", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/access_group/{access_group}/delete", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/access_group/{access_group}/budget", _refuse(_WHY_ADMIN_OFF)),
    _regex("PUT", "/access_group/{access_group}/budget", _refuse(_WHY_ADMIN_OFF)),
    _regex("PUT", "/access_group/{access_group}/update", _refuse(_WHY_ADMIN_OFF)),
    _regex(
        "POST",
        "/v1/responses/{response_id}/cancel",
        _passthrough(_WHY_STORED_RESPONSE, _JUST_STORED_RESPONSE),
    ),
    _regex("GET", "/access_group/{access_group}/info", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/interactions/{interaction_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/search_tools/{search_tool_id}", _refuse(_WHY_SEARCH)),
    _regex("GET", "/interactions/{interaction_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/search_tools/{search_tool_id}", _refuse(_WHY_SEARCH)),
    _regex("PUT", "/search_tools/{search_tool_id}", _refuse(_WHY_SEARCH)),
    _regex("DELETE", "/v1/responses/{response_id}", _passthrough(_WHY_STORED_RESPONSE)),
    _regex("GET", "/v1/responses/{response_id}", _passthrough(_WHY_STORED_RESPONSE)),
    _regex("GET", "/sso/cli/poll/{key_id}", _refuse(_WHY_AUTH_OFF)),
    _regex("DELETE", "/credentials/{credential_name:path}", _refuse(_WHY_ADMIN_OFF)),
    _regex("PATCH", "/credentials/{credential_name:path}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/policy/info/{policy_name}", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/v1/batches/{batch_id:path}/cancel", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/v1/threads/{thread_id}/messages", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/v1/threads/{thread_id}/messages", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/assistants/{assistant_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/guardrails/{guardrail_id}/info", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/v1/threads/{thread_id}/runs", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/v1/batches/{batch_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/containers/{container_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/guardrails/{guardrail_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/containers/{container_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/guardrails/{guardrail_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("PATCH", "/guardrails/{guardrail_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("PUT", "/guardrails/{guardrail_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/v1/threads/{thread_id}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/responses/{response_id}/input_items", _passthrough(_WHY_STORED_RESPONSE)),
    _regex("POST", "/v1/agents/{agent_id}/make_public", _refuse(_WHY_AGENT)),
    _regex(
        "POST",
        "/responses/{response_id}/cancel",
        _passthrough(_WHY_STORED_RESPONSE, _JUST_STORED_RESPONSE),
    ),
    _regex("GET", "/v1/videos/{video_id}/content", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/v1/search/{search_tool_name}", _refuse(_WHY_SEARCH)),
    _regex("POST", "/v1/videos/{video_id}/remix", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/anthropic/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/vertex-ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/vertex_ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/anthropic/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/vertex-ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/vertex_ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/anthropic/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/vertex-ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/vertex_ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/anthropic/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/vertex-ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/vertex_ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/anthropic/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/vertex-ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/vertex_ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/responses/{response_id}", _passthrough(_WHY_STORED_RESPONSE)),
    _regex("GET", "/responses/{response_id}", _passthrough(_WHY_STORED_RESPONSE)),
    _regex("DELETE", "/v1/agents/{agent_id}", _refuse(_WHY_AGENT)),
    _regex("DELETE", "/v1/memory/{key:path}", _refuse(_WHY_MEMORY)),
    _regex("DELETE", "/v1/skills/{skill_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1/agents/{agent_id}", _refuse(_WHY_AGENT)),
    _regex("GET", "/v1/memory/{key:path}", _refuse(_WHY_MEMORY)),
    _regex("GET", "/v1/models/{model_id}", _passthrough(_WHY_MODELS)),
    _regex("GET", "/v1/skills/{skill_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1/videos/{video_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("PATCH", "/v1/agents/{agent_id}", _refuse(_WHY_AGENT)),
    _regex("PUT", "/v1/agents/{agent_id}", _refuse(_WHY_AGENT)),
    _regex("PUT", "/v1/memory/{key:path}", _refuse(_WHY_MEMORY)),
    _regex("POST", "/lazy/warm/{name}", _refuse(_WHY_LAZY_OFF)),
    _regex("GET", "/policies/{policy_id}/resolved-guardrails", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/v1/evals/{eval_id}/runs/{run_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1/evals/{eval_id}/runs/{run_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/v1/evals/{eval_id}/runs/{run_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1/files/{file_id:path}/content", _refuse(_WHY_PROVIDER_STORE)),
    _regex("PUT", "/policies/{policy_id}/status", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/v1/evals/{eval_id}/cancel", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/azure_ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/gigachat/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/langfuse/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/azure_ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/gigachat/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/langfuse/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/azure_ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/gigachat/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/langfuse/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/azure_ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/gigachat/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/langfuse/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/azure_ai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/gigachat/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/langfuse/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/v1/files/{file_id:path}", _refuse(_WHY_PROVIDER_STORE)),
    _regex("GET", "/v1/evals/{eval_id}/runs", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1/files/{file_id:path}", _refuse(_WHY_PROVIDER_STORE)),
    _regex("POST", "/v1/evals/{eval_id}/runs", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/policies/{policy_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/policies/{policy_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("PUT", "/policies/{policy_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/v1/evals/{eval_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1/evals/{eval_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/v1/evals/{eval_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/fallback/{model}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/fallback/{model}", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/engines/{model:path}/chat/completions", _rewritten(_WHY_CHAT)),
    _regex("DELETE", "/v1/tool/{tool_name:path}/overrides", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/engines/{model:path}/completions", _refuse(_WHY_PROMPT)),
    _regex("GET", "/v1/tool/{tool_name:path}/detail", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/engines/{model:path}/embeddings", _refuse(_WHY_NO_REWRITE)),
    _regex("POST", "/batches/{batch_id:path}/cancel", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/v1/tool/{tool_name:path}/logs", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/prompts/{prompt_id}/versions", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/threads/{thread_id}/messages", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/threads/{thread_id}/messages", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/toolset/{toolset_name}/mcp", _refuse(_WHY_MCP)),
    _regex("GET", "/toolset/{toolset_name}/mcp", _refuse(_WHY_MCP)),
    _regex("OPTIONS", "/toolset/{toolset_name}/mcp", _refuse(_WHY_MCP)),
    _regex("PATCH", "/toolset/{toolset_name}/mcp", _refuse(_WHY_MCP)),
    _regex("POST", "/toolset/{toolset_name}/mcp", _refuse(_WHY_MCP)),
    _regex("PUT", "/toolset/{toolset_name}/mcp", _refuse(_WHY_MCP)),
    _regex("GET", "/prompts/{prompt_id}/info", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/v1/tool/{tool_name:path}", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/threads/{thread_id}/runs", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/bedrock/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/mistral/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/watsonx/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/batches/{batch_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/bedrock/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/mistral/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/watsonx/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/bedrock/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/mistral/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/watsonx/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/bedrock/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/mistral/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/watsonx/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/bedrock/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/mistral/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/watsonx/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/prompts/{prompt_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/prompts/{prompt_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/threads/{thread_id}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/prompts/{prompt_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("PUT", "/prompts/{prompt_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/models/{model_name:path}:streamGenerateContent", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/models/{model_name:path}:generateContent", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/models/{model_name:path}:countTokens", _refuse(_WHY_COUNT)),
    _regex("POST", "/v1/a2a/{agent_id}/message/send", _refuse(_WHY_AGENT)),
    _regex("GET", "/videos/{video_id}/content", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/search/{search_tool_name}", _refuse(_WHY_SEARCH)),
    _regex("POST", "/videos/{video_id}/remix", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/cohere/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/cursor/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/gemini/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/milvus/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/openai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/cohere/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/cursor/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/gemini/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/milvus/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/openai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/cohere/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/cursor/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/gemini/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/milvus/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/openai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/cohere/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/cursor/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/gemini/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/milvus/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/openai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/cohere/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/cursor/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/gemini/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/milvus/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/openai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/models/{model_id}", _passthrough(_WHY_MODELS)),
    _regex("GET", "/videos/{video_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/files/{file_id:path}/content", _refuse(_WHY_PROVIDER_STORE)),
    _regex("PATCH", "/model/{model_id}/update", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/azure/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/azure/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/azure/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/azure/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/azure/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/files/{file_id:path}", _refuse(_WHY_PROVIDER_STORE)),
    _regex("GET", "/files/{file_id:path}", _refuse(_WHY_PROVIDER_STORE)),
    _regex("DELETE", "/team/{team_id:path}/callback/{callback_name}", _refuse(_WHY_ADMIN_OFF)),
    _regex(
        "POST",
        "/team/{team_id}/member/{user_id}/reset_spend",
        _refuse(_WHY_ADMIN_OFF),
    ),
    _regex("POST", "/team/{team_id}/disable_logging", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/team/{team_id:path}/callback", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/team/{team_id:path}/callback", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/team/{team_id}/members/me", _refuse(_WHY_ADMIN_OFF)),
    _regex("DELETE", "/vllm/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/vllm/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/vllm/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/vllm/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/vllm/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/team/{team_id}", _refuse(_WHY_ADMIN_OFF)),
    _regex("GET", "/a2a/{agent_id}/.well-known/agent-card.json", _refuse(_WHY_AGENT)),
    _regex("GET", "/a2a/{agent_id}/.well-known/agent.json", _refuse(_WHY_AGENT)),
    _regex("POST", "/a2a/{agent_id}/message/send", _refuse(_WHY_AGENT)),
    _regex("POST", "/key/{key:path}/reset_spend", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/key/{key:path}/regenerate", _refuse(_WHY_ADMIN_OFF)),
    _regex("POST", "/a2a/{agent_id}", _refuse(_WHY_AGENT)),
    _regex("POST", "/{provider}/v1/batches/{batch_id:path}/cancel", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/{provider}/v1/files/{file_id:path}/content", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/{provider}/v1/batches/{batch_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/{provider}/v1/files/{file_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/{provider}/v1/files/{file_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/{mcp_server_name}/authorize", _refuse(_WHY_MCP)),
    _regex("POST", "/{mcp_server_name}/register", _refuse(_WHY_MCP)),
    _regex("POST", "/{mcp_server_name}/token", _refuse(_WHY_MCP)),
    _regex("DELETE", "/{mcp_server_name}/mcp", _refuse(_WHY_MCP)),
    _regex("GET", "/{mcp_server_name}/mcp", _refuse(_WHY_MCP)),
    _regex("GET", "/{provider}/v1/batches", _refuse(_WHY_PROVIDER)),
    _regex("OPTIONS", "/{mcp_server_name}/mcp", _refuse(_WHY_MCP)),
    _regex("PATCH", "/{mcp_server_name}/mcp", _refuse(_WHY_MCP)),
    _regex("POST", "/{mcp_server_name}/mcp", _refuse(_WHY_MCP)),
    _regex("POST", "/{provider}/v1/batches", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/{mcp_server_name}/mcp", _refuse(_WHY_MCP)),
    _regex("GET", "/{provider}/v1/files", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/{provider}/v1/files", _refuse(_WHY_PROVIDER)),
)

# Only what the entrypoint serves ahead of litellm's app: `build_health_router()`
# (/healthz/* and the issuance route) and the metrics exposition at /metrics.
# HEAD needs no row — it inherits the GET verdict in classify.py. Issuance is
# POST only; the HealthRouter answers it (404 when issuance is off) and never
# hands it to litellm.
GATEWAY_ROUTE_TABLE: dict[tuple[str, str], Entry] = {
    ("GET", "/healthz/extensions"): _passthrough("gateway-owned health check; no user text"),
    ("GET", "/healthz/live"): _passthrough("gateway-owned liveness probe; Helm probes it"),
    ("GET", "/healthz/ready"): _passthrough("gateway-owned readiness probe; Helm probes it"),
    ("GET", "/healthz/sanitization"): _passthrough("gateway-owned health check; no user text"),
    ("GET", "/metrics"): _passthrough("gateway-owned Prometheus exposition; no user text"),
    ("POST", "/internal/issue-token"): _passthrough(
        "gateway-owned issuance; terminates in the gateway, nothing forwarded",
        "answered by the HealthRouter, never litellm; body refused, bearer header only; "
        "the only outbound call is the configured Keycloak JWKS fetch, carrying no request byte",
    ),
}


def lookup(method: str, path: str) -> Entry | None:
    """Exact tables (litellm, then gateway), then the anchored regex table — the
    order Technical Details fixes. The exact tables are disjoint, so which of
    the two answers first is not observable. Returns ``None`` when no table
    knows the pair — the caller must refuse."""
    verb = method.upper()
    entry = LITELLM_ROUTE_TABLE.get((verb, path)) or GATEWAY_ROUTE_TABLE.get((verb, path))
    if entry is not None:
        return entry
    for row in LITELLM_REGEX_TABLE:
        if row.method == verb and row.pattern.match(path):
            return row.entry
    return None


def parse_extras(raw: str | Iterable[str] | None) -> dict[tuple[str, str], Entry]:
    """Parse ``CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH`` ("METHOD /path", comma or
    newline separated) into PASSTHROUGH entries. Raises on anything malformed or
    naming a refused route — an operator typo must fail at load, not widen the
    gate by accident."""
    if raw is None:
        return {}
    items = re.split(r"[,\n]", raw) if isinstance(raw, str) else list(raw)
    extras: dict[tuple[str, str], Entry] = {}
    for item in items:
        text = item.strip()
        if not text:
            continue
        fields = text.split()
        if len(fields) != 2:
            raise ValueError(f"route gate extra must be 'METHOD /path', got {item!r}")
        method, path = fields[0].upper(), fields[1]
        if method not in HTTP_METHODS:
            raise ValueError(f"route gate extra has unknown method {fields[0]!r} in {item!r}")
        if not path.startswith("/"):
            raise ValueError(f"route gate extra path must start with '/', got {item!r}")
        bad = [token for token in _BAD_EXTRA_PATH_TOKENS if token in path]
        if bad or any(char.isspace() for char in path):
            offending = ", ".join(bad) or "whitespace"
            raise ValueError(
                f"route gate extra path must be a plain absolute path — {offending} "
                f"in {item!r} matches nothing; the gate matches the decoded path as received"
            )
        if _refused(method, path):
            raise ValueError(
                f"route gate extra {method} {path} names a route the table refused; the key "
                "adds PASSTHROUGH rows only and can never re-open a refusal (docs/security.md §14)"
            )
        extras[(method, path)] = _passthrough(
            f"operator extra: CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH lists {method} {path}"
        )
    return extras


def _refused(method: str, path: str) -> bool:
    # HEAD inherits its path's GET verdict, so a HEAD extra is judged by both.
    methods = (method, "GET") if method == "HEAD" else (method,)
    for verb in methods:
        entry = lookup(verb, path)
        if entry is not None and entry.verdict is Verdict.REFUSE:
            return True
    return False
