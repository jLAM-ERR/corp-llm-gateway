"""The route table: the single place a litellm route is classified.

Every request the gateway serves is looked up here before litellm's router sees
it. A pair the tables do not know is refused (default-deny), so a route litellm
adds in a future bump cannot reach a provider until someone classifies it.

``LITELLM_ROUTE_TABLE`` / ``LITELLM_REGEX_TABLE`` describe routes litellm
registers; ``GATEWAY_ROUTE_TABLE`` describes the routes the gateway itself
mounts (``/healthz/*``, ``/metrics``) and is exempt from the source guard.

The litellm tables are generated from litellm 1.101.0's own source with the
collector in ``tests/route_gate/litellm_routes.py`` and these rules, in order:

1. the eleven generation spellings whose body the hook rewrites are REWRITTEN;
2. a stored-response route addressed by id is PASSTHROUGH — it sends no
   inbound text;
3. a handler that reaches ``pre_call_hook`` or calls a provider any other way
   (``ast``-visible) is REFUSE: the hook does not rewrite that body;
4. named trees that carry user text without the hook — MCP, agents, RAG,
   search, memory, files, token counting, guardrail echo, spend export — are
   REFUSE;
5. everything else is litellm's own admin surface: PASSTHROUGH, and a
   POST/PUT/PATCH among them must carry the written ``justification`` that
   ``tests/route_gate/test_litellm_route_guard.py`` rule (c) re-checks by
   ``ast``.

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
    return RegexEntry(method, template, _anchored(_pattern(template)), entry)


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

_WHY_HOME = "litellm's own index; no user text"
_WHY_HEALTH = "litellm health probe; no user text"
_WHY_MODELS = "model list or metadata; no user text"
_WHY_AUTH = "login / SSO flow; no user text"
_WHY_SPEND = "spend and usage analytics; no user text"
_WHY_PUBLIC = "litellm's unauthenticated public surface; no user text"
_WHY_UI = "admin UI asset or setting; no user text"
_WHY_LAZY = "lazy-router warm-up; no user text"
_WHY_ADMIN = "litellm admin surface, loopback/tunnel-only by design; no user text"

# Rule (c): every PASSTHROUGH route that carries a body says here why it cannot
# put user text on the wire. The guard re-checks each one against the handler's
# own `ast` — a written excuse never overrides a provider call it can see.
_JUST_ADMIN = "management CRUD or config write in litellm's own database; calls no provider"
_JUST_AUTH = "exchanges a login token against the proxy's own session store; calls no provider"
_JUST_SPEND = "reads and refreshes spend rows in litellm's database; calls no provider"
_JUST_PUBLIC = "serves litellm's static public catalogue; calls no provider"
_JUST_UI = "stores an admin UI setting or asset; calls no provider"
_JUST_LAZY = "imports routers only (_lazy_features.py:370-396); calls no provider"
_JUST_STORED_RESPONSE = "cancels a stored response by id; no request body reaches a provider"

LITELLM_ROUTE_TABLE: dict[tuple[str, str], Entry] = {
    ("DELETE", "/cloudzero/delete"): _passthrough(_WHY_ADMIN),
    ("DELETE", "/config/pass_through_endpoint"): _passthrough(_WHY_ADMIN),
    ("DELETE", "/config_overrides/cyberark"): _passthrough(_WHY_ADMIN),
    ("DELETE", "/config_overrides/hashicorp_vault"): _passthrough(_WHY_ADMIN),
    ("DELETE", "/mcp"): _refuse(_WHY_MCP),
    ("DELETE", "/organization/delete"): _passthrough(_WHY_ADMIN),
    ("DELETE", "/organization/member_delete"): _passthrough(_WHY_ADMIN),
    ("DELETE", "/schedule/anthropic_beta_headers_reload"): _passthrough(_WHY_ADMIN),
    ("DELETE", "/schedule/model_cost_map_reload"): _passthrough(_WHY_ADMIN),
    ("DELETE", "/vantage/delete"): _passthrough(_WHY_ADMIN),
    ("GET", "/"): _passthrough(_WHY_HOME),
    ("GET", "/.well-known/jwks.json"): _refuse(_WHY_MCP),
    ("GET", "/.well-known/litellm-cli-auth"): _refuse(_WHY_MCP),
    ("GET", "/.well-known/litellm-ui-config"): _passthrough(_WHY_ADMIN),
    ("GET", "/.well-known/oauth-authorization-server"): _refuse(_WHY_MCP),
    ("GET", "/.well-known/oauth-protected-resource"): _refuse(_WHY_MCP),
    ("GET", "/.well-known/openid-configuration"): _refuse(_WHY_MCP),
    ("GET", "/access_group/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/active/callbacks"): _passthrough(_WHY_ADMIN),
    ("GET", "/adaptive_router/state"): _passthrough(_WHY_ADMIN),
    ("GET", "/agent/daily/activity"): _passthrough(_WHY_ADMIN),
    ("GET", "/alerting/settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/api/plugins"): _passthrough(_WHY_ADMIN),
    ("GET", "/api/plugins/auth-token"): _passthrough(_WHY_ADMIN),
    ("GET", "/assistants"): _refuse(_WHY_PROVIDER),
    ("GET", "/authorize"): _refuse(_WHY_MCP),
    ("GET", "/auto_router/benchmarks"): _passthrough(_WHY_ADMIN),
    ("GET", "/auto_router/classifier/default_prompt"): _passthrough(_WHY_ADMIN),
    ("GET", "/auto_router/shadow_eval"): _passthrough(_WHY_ADMIN),
    ("GET", "/batches"): _refuse(_WHY_PROVIDER),
    ("GET", "/budget/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/budget/settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/cache/ping"): _passthrough(_WHY_ADMIN),
    ("GET", "/cache/redis/info"): _passthrough(_WHY_ADMIN),
    ("GET", "/cache/settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/callback"): _refuse(_WHY_MCP),
    ("GET", "/callbacks/configs"): _passthrough(_WHY_ADMIN),
    ("GET", "/callbacks/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/claude-code/marketplace.json"): _passthrough(_WHY_ADMIN),
    ("GET", "/claude-code/plugins"): _passthrough(_WHY_ADMIN),
    ("GET", "/cloudzero/settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/config/block_requests_for_models_without_pricing"): _passthrough(_WHY_ADMIN),
    ("GET", "/config/cost_discount_config"): _passthrough(_WHY_ADMIN),
    ("GET", "/config/cost_margin_config"): _passthrough(_WHY_ADMIN),
    ("GET", "/config/field/info"): _passthrough(_WHY_ADMIN),
    ("GET", "/config/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/config/pass_through_endpoint"): _passthrough(_WHY_ADMIN),
    ("GET", "/config/pass_through_endpoints/settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/config/yaml"): _passthrough(_WHY_ADMIN),
    ("GET", "/config_overrides/cyberark"): _passthrough(_WHY_ADMIN),
    ("GET", "/config_overrides/hashicorp_vault"): _passthrough(_WHY_ADMIN),
    ("GET", "/containers"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/coordination_redis/settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/credentials"): _passthrough(_WHY_ADMIN),
    ("GET", "/credentials/migrate-encryption/check"): _passthrough(_WHY_ADMIN),
    ("GET", "/cursor/models"): _passthrough(_WHY_MODELS),
    ("GET", "/cursor/v1/models"): _passthrough(_WHY_MODELS),
    ("GET", "/customer/daily/activity"): _passthrough(_WHY_ADMIN),
    ("GET", "/customer/info"): _passthrough(_WHY_ADMIN),
    ("GET", "/customer/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/debug/asyncio-tasks"): _passthrough(_WHY_ADMIN),
    ("GET", "/debug/memory/details"): _passthrough(_WHY_ADMIN),
    ("GET", "/debug/memory/summary"): _passthrough(_WHY_ADMIN),
    ("GET", "/enabled"): _passthrough(_WHY_ADMIN),
    ("GET", "/end_user/daily/activity"): _passthrough(_WHY_ADMIN),
    ("GET", "/end_user/info"): _passthrough(_WHY_ADMIN),
    ("GET", "/end_user/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/fallback/login"): _passthrough(_WHY_ADMIN),
    ("GET", "/files"): _refuse(_WHY_PROVIDER_STORE),
    ("GET", "/fine_tuning/jobs"): _refuse(_WHY_PROVIDER),
    ("GET", "/gateway/daily/activity"): _passthrough(_WHY_ADMIN),
    ("GET", "/get/allowed_ips"): _passthrough(_WHY_ADMIN),
    ("GET", "/get/config/callbacks"): _passthrough(_WHY_ADMIN),
    ("GET", "/get/default_team_settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/get/internal_user_settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/get/mcp_semantic_filter_settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/get/mcp_tool_search_settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/get/sso_settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/get/ui_settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/get/ui_theme_settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/get/user_banner"): _passthrough(_WHY_ADMIN),
    ("GET", "/get_favicon"): _passthrough(_WHY_UI),
    ("GET", "/get_image"): _passthrough(_WHY_UI),
    ("GET", "/get_logo_url"): _passthrough(_WHY_UI),
    ("GET", "/global/activity"): _passthrough(_WHY_SPEND),
    ("GET", "/global/activity/cache_hits"): _passthrough(_WHY_SPEND),
    ("GET", "/global/activity/exceptions"): _passthrough(_WHY_SPEND),
    ("GET", "/global/activity/exceptions/deployment"): _passthrough(_WHY_SPEND),
    ("GET", "/global/activity/model"): _passthrough(_WHY_SPEND),
    ("GET", "/global/all_end_users"): _passthrough(_WHY_SPEND),
    ("GET", "/global/spend"): _passthrough(_WHY_SPEND),
    ("GET", "/global/spend/all_tag_names"): _passthrough(_WHY_SPEND),
    ("GET", "/global/spend/keys"): _passthrough(_WHY_SPEND),
    ("GET", "/global/spend/logs"): _passthrough(_WHY_SPEND),
    ("GET", "/global/spend/models"): _passthrough(_WHY_SPEND),
    ("GET", "/global/spend/provider"): _passthrough(_WHY_SPEND),
    ("GET", "/global/spend/report"): _passthrough(_WHY_SPEND),
    ("GET", "/global/spend/tags"): _passthrough(_WHY_SPEND),
    ("GET", "/global/spend/teams"): _passthrough(_WHY_SPEND),
    ("GET", "/guardrails/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/guardrails/submissions"): _passthrough(_WHY_ADMIN),
    ("GET", "/guardrails/ui/add_guardrail_settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/guardrails/ui/major_airlines"): _passthrough(_WHY_ADMIN),
    ("GET", "/guardrails/ui/provider_specific_params"): _passthrough(_WHY_ADMIN),
    ("GET", "/guardrails/usage/logs"): _passthrough(_WHY_ADMIN),
    ("GET", "/guardrails/usage/overview"): _passthrough(_WHY_ADMIN),
    ("GET", "/health"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/backlog"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/drain"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/history"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/latest"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/license"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/liveliness"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/liveness"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/readiness"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/readiness/details"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/services"): _refuse(_WHY_PROVIDER),
    ("GET", "/health/shared-status"): _passthrough(_WHY_HEALTH),
    ("GET", "/invitation/info"): _passthrough(_WHY_AUTH),
    ("GET", "/jwt/key/mapping/info"): _passthrough(_WHY_ADMIN),
    ("GET", "/jwt/key/mapping/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/key/aliases"): _passthrough(_WHY_ADMIN),
    ("GET", "/key/info"): _passthrough(_WHY_ADMIN),
    ("GET", "/key/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/key/spend/report"): _passthrough(_WHY_ADMIN),
    ("GET", "/litellm/.well-known/litellm-ui-config"): _passthrough(_WHY_ADMIN),
    ("GET", "/management/v1/budgets"): _passthrough(_WHY_ADMIN),
    ("GET", "/management/v1/spend_logs/end_users"): _passthrough(_WHY_ADMIN),
    ("GET", "/management/v1/spend_logs/users"): _passthrough(_WHY_ADMIN),
    ("GET", "/mcp"): _refuse(_WHY_MCP),
    ("GET", "/mcp-rest/tools/list"): _refuse(_WHY_MCP),
    ("GET", "/memory-usage"): _passthrough(_WHY_ADMIN),
    ("GET", "/memory-usage-in-mem-cache"): _passthrough(_WHY_ADMIN),
    ("GET", "/memory-usage-in-mem-cache-items"): _passthrough(_WHY_ADMIN),
    ("GET", "/model/cost_map/source"): _passthrough(_WHY_ADMIN),
    ("GET", "/model/deprecations"): _passthrough(_WHY_ADMIN),
    ("GET", "/model/info"): _passthrough(_WHY_MODELS),
    ("GET", "/model/metrics"): _passthrough(_WHY_ADMIN),
    ("GET", "/model/metrics/exceptions"): _passthrough(_WHY_ADMIN),
    ("GET", "/model/metrics/slow_responses"): _passthrough(_WHY_ADMIN),
    ("GET", "/model/settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/model/streaming_metrics"): _passthrough(_WHY_ADMIN),
    ("GET", "/model_group/info"): _passthrough(_WHY_ADMIN),
    ("GET", "/models"): _passthrough(_WHY_MODELS),
    ("GET", "/onboarding/get_token"): _passthrough(_WHY_AUTH),
    ("GET", "/organization/daily/activity"): _passthrough(_WHY_ADMIN),
    ("GET", "/organization/info"): _passthrough(_WHY_ADMIN),
    ("GET", "/organization/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/organization/spend/report"): _passthrough(_WHY_ADMIN),
    ("GET", "/otel-spans"): _passthrough(_WHY_ADMIN),
    ("GET", "/policies/attachments/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/policies/compare"): _passthrough(_WHY_ADMIN),
    ("GET", "/policies/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/policies/usage/overview"): _passthrough(_WHY_ADMIN),
    ("GET", "/policy/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/policy/templates"): _passthrough(_WHY_ADMIN),
    ("GET", "/prompts/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/provider/budgets"): _passthrough(_WHY_SPEND),
    ("GET", "/public/agent_hub"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/agents/fields"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/autorouter_presets"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/complexity_router/scorer_defaults"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/endpoints"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/litellm_blog_posts"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/litellm_model_cost_map"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/mcp_hub"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/model_hub"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/model_hub/info"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/providers"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/providers/fields"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/skill_hub"): _passthrough(_WHY_PUBLIC),
    ("GET", "/public/v1/model_hub"): _passthrough(_WHY_PUBLIC),
    ("GET", "/router/fields"): _passthrough(_WHY_ADMIN),
    ("GET", "/router/settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/routes"): _passthrough(_WHY_HOME),
    ("GET", "/schedule/anthropic_beta_headers_reload/status"): _passthrough(_WHY_ADMIN),
    ("GET", "/schedule/model_cost_map_reload/status"): _passthrough(_WHY_ADMIN),
    ("GET", "/scim/v2"): _passthrough(_WHY_ADMIN),
    ("GET", "/scim/v2/"): _passthrough(_WHY_ADMIN),
    ("GET", "/scim/v2/Groups"): _passthrough(_WHY_ADMIN),
    ("GET", "/scim/v2/ResourceTypes"): _passthrough(_WHY_ADMIN),
    ("GET", "/scim/v2/Schemas"): _passthrough(_WHY_ADMIN),
    ("GET", "/scim/v2/ServiceProviderConfig"): _passthrough(_WHY_ADMIN),
    ("GET", "/scim/v2/Users"): _passthrough(_WHY_ADMIN),
    ("GET", "/scim/v2/placeholders"): _passthrough(_WHY_ADMIN),
    ("GET", "/search/tools"): _refuse(_WHY_SEARCH),
    ("GET", "/search_tools/list"): _refuse(_WHY_SEARCH),
    ("GET", "/search_tools/ui/available_providers"): _refuse(_WHY_SEARCH),
    ("GET", "/settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/spend/keys"): _passthrough(_WHY_SPEND),
    ("GET", "/spend/logs"): _passthrough(_WHY_SPEND),
    ("GET", "/spend/logs/session/ui"): _passthrough(_WHY_SPEND),
    ("GET", "/spend/logs/ui"): _passthrough(_WHY_SPEND),
    ("GET", "/spend/logs/v2"): _passthrough(_WHY_SPEND),
    ("GET", "/spend/tags"): _passthrough(_WHY_SPEND),
    ("GET", "/spend/users"): _passthrough(_WHY_SPEND),
    ("GET", "/sso/callback"): _passthrough(_WHY_AUTH),
    ("GET", "/sso/debug/callback"): _passthrough(_WHY_AUTH),
    ("GET", "/sso/debug/login"): _passthrough(_WHY_AUTH),
    ("GET", "/sso/get/ui_settings"): _passthrough(_WHY_AUTH),
    ("GET", "/sso/key/generate"): _passthrough(_WHY_AUTH),
    ("GET", "/sso/readiness"): _passthrough(_WHY_AUTH),
    ("GET", "/sso/saml/login"): _passthrough(_WHY_AUTH),
    ("GET", "/sso/saml/metadata"): _passthrough(_WHY_AUTH),
    ("GET", "/tag/daily/activity"): _passthrough(_WHY_ADMIN),
    ("GET", "/tag/dau"): _passthrough(_WHY_ADMIN),
    ("GET", "/tag/distinct"): _passthrough(_WHY_ADMIN),
    ("GET", "/tag/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/tag/mau"): _passthrough(_WHY_ADMIN),
    ("GET", "/tag/summary"): _passthrough(_WHY_ADMIN),
    ("GET", "/tag/user-agent/per-user-analytics"): _passthrough(_WHY_ADMIN),
    ("GET", "/tag/wau"): _passthrough(_WHY_ADMIN),
    ("GET", "/team/available"): _passthrough(_WHY_ADMIN),
    ("GET", "/team/daily/activity"): _passthrough(_WHY_ADMIN),
    ("GET", "/team/daily/activity/aggregated"): _passthrough(_WHY_ADMIN),
    ("GET", "/team/filter/ui"): _passthrough(_WHY_ADMIN),
    ("GET", "/team/info"): _passthrough(_WHY_ADMIN),
    ("GET", "/team/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/team/metadata_schema"): _passthrough(_WHY_ADMIN),
    ("GET", "/team/permissions_list"): _passthrough(_WHY_ADMIN),
    ("GET", "/team/spend/by_user"): _passthrough(_WHY_ADMIN),
    ("GET", "/team/spend/report"): _passthrough(_WHY_ADMIN),
    ("GET", "/test"): _passthrough(_WHY_ADMIN),
    ("GET", "/user/available_roles"): _passthrough(_WHY_ADMIN),
    ("GET", "/user/daily/activity"): _passthrough(_WHY_ADMIN),
    ("GET", "/user/daily/activity/aggregated"): _passthrough(_WHY_ADMIN),
    ("GET", "/user/filter/ui"): _passthrough(_WHY_ADMIN),
    ("GET", "/user/info"): _passthrough(_WHY_ADMIN),
    ("GET", "/user/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/user/spend/report"): _passthrough(_WHY_ADMIN),
    ("GET", "/utils/supported_openai_params"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/access_group"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/agents"): _refuse(_WHY_AGENT),
    ("GET", "/v1/assistants"): _refuse(_WHY_PROVIDER),
    ("GET", "/v1/batches"): _refuse(_WHY_PROVIDER),
    ("GET", "/v1/containers"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/v1/evals"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/v1/files"): _refuse(_WHY_PROVIDER_STORE),
    ("GET", "/v1/fine_tuning/jobs"): _refuse(_WHY_PROVIDER),
    ("GET", "/v1/indexes"): _refuse(_WHY_PROVIDER_STORE),
    ("GET", "/v1/mcp/access_groups"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/mcp/discover"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/mcp/network/client-ip"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/mcp/oauth/authorize"): _refuse(_WHY_MCP),
    ("GET", "/v1/mcp/openapi-registry"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/mcp/registry.json"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/mcp/server"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/mcp/server/health"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/mcp/server/submissions"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/mcp/tools"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/mcp/toolset"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/mcp/user-credentials"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/mcp/user-env-vars/status"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/memory"): _refuse(_WHY_MEMORY),
    ("GET", "/v1/model/deprecations"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/model/info"): _passthrough(_WHY_MODELS),
    ("GET", "/v1/models"): _passthrough(_WHY_MODELS),
    ("GET", "/v1/search/tools"): _refuse(_WHY_SEARCH),
    ("GET", "/v1/skills"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/v1/tool/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/tool/policy/options"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/tool/spend"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/unified_access_group"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/vector_store/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/v1/vector_stores"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/v1/videos"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/v1/workflows/runs"): _refuse(_WHY_AGENT),
    ("GET", "/v1beta/agents"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/v2/guardrails/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/v2/model/info"): _passthrough(_WHY_ADMIN),
    ("GET", "/v2/team/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/v2/user/info"): _passthrough(_WHY_ADMIN),
    ("GET", "/vantage/settings"): _passthrough(_WHY_ADMIN),
    ("GET", "/vector_store/list"): _passthrough(_WHY_ADMIN),
    ("GET", "/vector_stores"): _refuse(_WHY_NOT_REWRITTEN),
    ("GET", "/videos"): _refuse(_WHY_NOT_REWRITTEN),
    ("OPTIONS", "/health/liveliness"): _passthrough(_WHY_HEALTH),
    ("OPTIONS", "/health/liveness"): _passthrough(_WHY_HEALTH),
    ("OPTIONS", "/health/readiness"): _passthrough(_WHY_HEALTH),
    ("OPTIONS", "/mcp"): _refuse(_WHY_MCP),
    ("PATCH", "/config/block_requests_for_models_without_pricing"): _passthrough(
        _WHY_ADMIN, _JUST_ADMIN
    ),
    ("PATCH", "/config/cost_discount_config"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PATCH", "/config/cost_margin_config"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PATCH", "/mcp"): _refuse(_WHY_MCP),
    ("PATCH", "/organization/member_update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PATCH", "/organization/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PATCH", "/update/default_team_settings"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PATCH", "/update/internal_user_settings"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PATCH", "/update/mcp_semantic_filter_settings"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PATCH", "/update/mcp_tool_search_settings"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PATCH", "/update/sso_settings"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PATCH", "/update/ui_settings"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PATCH", "/update/ui_theme_settings"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PATCH", "/update/user_banner"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/access_group/new"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/add/allowed_ip"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/api/event_logging/batch"): _refuse(_WHY_NO_HOOK),
    ("POST", "/apply_guardrail"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/assistants"): _refuse(_WHY_PROVIDER),
    ("POST", "/audio/speech"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/audio/transcriptions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/authorize/complete"): _refuse(_WHY_MCP),
    ("POST", "/auto_router/classifier/default_prompt"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/auto_router/shadow_eval/start"): _refuse(_WHY_NO_HOOK),
    ("POST", "/auto_router/test_routing"): _refuse(_WHY_NO_HOOK),
    ("POST", "/auto_router/validate_complexity_router_config"): _passthrough(
        _WHY_ADMIN, _JUST_ADMIN
    ),
    ("POST", "/batches"): _refuse(_WHY_PROVIDER),
    ("POST", "/budget/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/budget/info"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/budget/new"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/budget/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/cache/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/cache/flushall"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/cache/settings"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/cache/settings/test"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/chat/completions"): _rewritten(_WHY_CHAT),
    ("POST", "/claude-code/plugins"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/cloudzero/dry-run"): _refuse(_WHY_SPEND_EXPORT),
    ("POST", "/cloudzero/export"): _refuse(_WHY_SPEND_EXPORT),
    ("POST", "/cloudzero/init"): _refuse(_WHY_SPEND_EXPORT),
    ("POST", "/completions"): _refuse(_WHY_PROMPT),
    ("POST", "/compliance/eu-ai-act"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/compliance/gdpr"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/comprehendmedical"): _refuse(_WHY_PROVIDER),
    ("POST", "/config/callback/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/config/field/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/config/field/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/config/pass_through_endpoint"): _refuse(_WHY_PROVIDER),
    ("POST", "/config/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/config_overrides/cyberark"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/config_overrides/cyberark/test_connection"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/config_overrides/hashicorp_vault"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/config_overrides/hashicorp_vault/test_connection"): _passthrough(
        _WHY_ADMIN, _JUST_ADMIN
    ),
    ("POST", "/containers"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/coordination_redis/settings"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/coordination_redis/settings/test"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/cost/estimate"): _refuse(_WHY_NO_HOOK),
    ("POST", "/credentials"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/credentials/migrate-encryption"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/cursor/chat/completions"): _refuse(_WHY_CURSOR),
    ("POST", "/customer/block"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/customer/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/customer/new"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/customer/unblock"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/customer/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/debug/memory/gc/configure"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/delete/allowed_ip"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/embeddings"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/end_user/block"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/end_user/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/end_user/new"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/end_user/unblock"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/end_user/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/fallback"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/files"): _refuse(_WHY_PROVIDER_STORE),
    ("POST", "/fine_tuning/jobs"): _refuse(_WHY_PROVIDER),
    ("POST", "/global/spend/end_users"): _passthrough(_WHY_SPEND, _JUST_SPEND),
    ("POST", "/global/spend/refresh"): _passthrough(_WHY_SPEND, _JUST_SPEND),
    ("POST", "/global/spend/reset"): _passthrough(_WHY_SPEND, _JUST_SPEND),
    ("POST", "/guardrails"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/guardrails/apply_guardrail"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/guardrails/register"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/guardrails/test_custom_code"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/guardrails/validate_blocked_words_file"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/health/test_connection"): _refuse(_WHY_NO_HOOK),
    ("POST", "/images/edits"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/images/generations"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/interactions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/introspect"): _refuse(_WHY_MCP),
    ("POST", "/invitation/delete"): _passthrough(_WHY_AUTH, _JUST_AUTH),
    ("POST", "/invitation/new"): _passthrough(_WHY_AUTH, _JUST_AUTH),
    ("POST", "/invitation/update"): _passthrough(_WHY_AUTH, _JUST_AUTH),
    ("POST", "/jwt/key/mapping/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/jwt/key/mapping/new"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/jwt/key/mapping/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/key/block"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/key/bulk_update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/key/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/key/generate"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/key/health"): _refuse(_WHY_PROVIDER),
    ("POST", "/key/regenerate"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/key/service-account/generate"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/key/unblock"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/key/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/login"): _passthrough(_WHY_AUTH, _JUST_AUTH),
    ("POST", "/mcp"): _refuse(_WHY_MCP),
    ("POST", "/mcp-rest/test/connection"): _refuse(_WHY_MCP),
    ("POST", "/mcp-rest/test/tools/list"): _refuse(_WHY_MCP),
    ("POST", "/mcp-rest/tools/call"): _refuse(_WHY_MCP),
    ("POST", "/model/block"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/model/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/model/new"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/model/unblock"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/model/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/model_group/make_public"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/model_hub/update_useful_links"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/moderations"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/ocr"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/onboarding/claim_token"): _passthrough(_WHY_AUTH, _JUST_AUTH),
    ("POST", "/openai/v1/realtime/calls"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/openai/v1/realtime/client_secrets"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/openai/v1/realtime/transcription_sessions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/openai/v1/responses"): _rewritten(_WHY_RESPONSES),
    ("POST", "/openai/v1/responses/compact"): _rewritten(_WHY_COMPACT),
    ("POST", "/openai/v1/responses/input_tokens"): _refuse(_WHY_COUNT),
    ("POST", "/organization/info"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/organization/member_add"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/organization/new"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/policies"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/policies/attachments"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/policies/attachments/estimate-impact"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/policies/resolve"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/policies/test-pipeline"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/policy/templates/enrich"): _refuse(_WHY_PROVIDER),
    ("POST", "/policy/templates/enrich/stream"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/policy/templates/suggest"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/policy/templates/test"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/policy/test"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/policy/validate"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/prompts"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/prompts/test"): _refuse(_WHY_NO_HOOK),
    ("POST", "/queue/chat/completions"): _refuse(_WHY_NO_HOOK),
    ("POST", "/rag/ingest"): _refuse(_WHY_RAG),
    ("POST", "/rag/query"): _refuse(_WHY_RAG),
    ("POST", "/realtime/calls"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/realtime/client_secrets"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/realtime/transcription_sessions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/register"): _refuse(_WHY_MCP),
    ("POST", "/reload/anthropic_beta_headers"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/reload/model_cost_map"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/rerank"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/responses"): _rewritten(_WHY_RESPONSES),
    ("POST", "/responses/compact"): _rewritten(_WHY_COMPACT),
    ("POST", "/responses/input_tokens"): _refuse(_WHY_COUNT),
    ("POST", "/revoke"): _refuse(_WHY_MCP),
    ("POST", "/schedule/anthropic_beta_headers_reload"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/schedule/model_cost_map_reload"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/scim/v2/Groups"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/scim/v2/Users"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/search"): _refuse(_WHY_SEARCH),
    ("POST", "/search_tools"): _refuse(_WHY_SEARCH),
    ("POST", "/search_tools/test_connection"): _refuse(_WHY_NO_HOOK),
    ("POST", "/spend/calculate"): _refuse(_WHY_NO_HOOK),
    ("POST", "/sso/cli/start"): _passthrough(_WHY_AUTH, _JUST_AUTH),
    ("POST", "/sso/saml/callback"): _passthrough(_WHY_AUTH, _JUST_AUTH),
    ("POST", "/tag/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/tag/info"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/tag/new"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/tag/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/block"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/bulk_member_add"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/key/bulk_update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/member_add"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/member_delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/member_update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/model/add"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/model/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/new"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/permissions_bulk_update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/permissions_update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/unblock"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/team/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/threads"): _refuse(_WHY_PROVIDER),
    ("POST", "/token"): _refuse(_WHY_MCP),
    ("POST", "/upload/logo"): _passthrough(_WHY_UI, _JUST_UI),
    ("POST", "/usage/ai/chat"): _refuse(_WHY_NO_HOOK),
    ("POST", "/user/bulk_update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/user/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/user/new"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/user/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/utils/dotprompt_json_converter"): _refuse(_WHY_NO_HOOK),
    ("POST", "/utils/test_policies_and_guardrails"): _refuse(_WHY_GUARDRAIL_ECHO),
    ("POST", "/utils/token_counter"): _refuse(_WHY_COUNT),
    ("POST", "/utils/transform_request"): _refuse(_WHY_NO_HOOK),
    ("POST", "/v1/a2a/discover"): _refuse(_WHY_AGENT),
    ("POST", "/v1/access_group"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
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
    ("POST", "/v1/mcp/make_public"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/v1/mcp/oauth/authorize"): _refuse(_WHY_MCP),
    ("POST", "/v1/mcp/oauth/token"): _refuse(_WHY_MCP),
    ("POST", "/v1/mcp/server"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/v1/mcp/server/import"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/v1/mcp/server/oauth/session"): _refuse(_WHY_MCP),
    ("POST", "/v1/mcp/server/register"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/v1/mcp/toolset"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
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
    ("POST", "/v1/tool/policy"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/v1/unified_access_group"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/v1/vector_stores"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/videos"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/videos/characters"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/videos/edits"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/videos/extensions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1/workflows/runs"): _refuse(_WHY_AGENT),
    ("POST", "/v1beta/agents"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v1beta/interactions"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v2/key/info"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/v2/login"): _passthrough(_WHY_AUTH, _JUST_AUTH),
    ("POST", "/v2/rerank"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/v3/login"): _passthrough(_WHY_AUTH, _JUST_AUTH),
    ("POST", "/v3/login/exchange"): _passthrough(_WHY_AUTH, _JUST_AUTH),
    ("POST", "/vantage/dry-run"): _refuse(_WHY_SPEND_EXPORT),
    ("POST", "/vantage/export"): _refuse(_WHY_SPEND_EXPORT),
    ("POST", "/vantage/init"): _refuse(_WHY_SPEND_EXPORT),
    ("POST", "/vector_store/delete"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/vector_store/info"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/vector_store/new"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/vector_store/update"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("POST", "/vector_stores"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/videos"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/videos/characters"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/videos/edits"): _refuse(_WHY_NOT_REWRITTEN),
    ("POST", "/videos/extensions"): _refuse(_WHY_NOT_REWRITTEN),
    ("PUT", "/cloudzero/settings"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PUT", "/mcp"): _refuse(_WHY_MCP),
    ("PUT", "/v1/mcp/server"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PUT", "/v1/mcp/toolset"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ("PUT", "/vantage/settings"): _passthrough(_WHY_ADMIN, _JUST_ADMIN),
}

LITELLM_REGEX_TABLE: tuple[RegexEntry, ...] = (
    _regex(
        "GET", "/.well-known/oauth-authorization-server/{mcp_server_name}/mcp", _refuse(_WHY_MCP)
    ),
    _regex("GET", "/config/pass_through_endpoint/team/{team_id}", _passthrough(_WHY_ADMIN)),
    _regex("POST", "/config/pass_through_endpoint/{endpoint_id}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/guardrails/ui/category_yaml/{category_name}", _passthrough(_WHY_ADMIN)),
    _regex("DELETE", "/v1/unified_access_group/{access_group_id}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/v1/unified_access_group/{access_group_id}", _passthrough(_WHY_ADMIN)),
    _regex(
        "PUT", "/v1/unified_access_group/{access_group_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)
    ),
    _regex("GET", "/guardrails/usage/detail/{guardrail_id}", _passthrough(_WHY_ADMIN)),
    _regex("POST", "/auto_router/shadow_eval/{job_id}/stop", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("GET", "/auto_router/shadow_eval/{job_id}", _passthrough(_WHY_ADMIN)),
    _regex(
        "POST",
        "/guardrails/submissions/{guardrail_id}/approve",
        _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ),
    _regex(
        "POST",
        "/guardrails/submissions/{guardrail_id}/reject",
        _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ),
    _regex("GET", "/guardrails/submissions/{guardrail_id}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/scim/v2/ResourceTypes/{resource_type_id}", _passthrough(_WHY_ADMIN)),
    _regex("DELETE", "/policies/attachments/{attachment_id}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/policies/attachments/{attachment_id}", _passthrough(_WHY_ADMIN)),
    _regex("POST", "/scim/v2/placeholders/{user_id}/merge", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("GET", "/v1/videos/characters/{character_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/credentials/by_model/{model_id}", _passthrough(_WHY_ADMIN)),
    _regex("POST", "/v1/fine_tuning/jobs/{fine_tuning_job_id:path}/cancel", _refuse(_WHY_PROVIDER)),
    _regex(
        "GET", "/openai/v1/responses/{response_id}/input_items", _passthrough(_WHY_STORED_RESPONSE)
    ),
    _regex("GET", "/v1/fine_tuning/jobs/{fine_tuning_job_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/v1beta/interactions/{interaction_id}/cancel", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/credentials/by_name/{credential_name:path}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/v1/mcp/server/oauth/{server_id}/authorize", _refuse(_WHY_MCP)),
    _regex(
        "POST", "/claude-code/plugins/{plugin_name}/disable", _passthrough(_WHY_ADMIN, _JUST_ADMIN)
    ),
    _regex(
        "POST", "/claude-code/plugins/{plugin_name}/enable", _passthrough(_WHY_ADMIN, _JUST_ADMIN)
    ),
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
    _regex("DELETE", "/claude-code/plugins/{plugin_name}", _passthrough(_WHY_ADMIN)),
    _regex("DELETE", "/openai/v1/responses/{response_id}", _passthrough(_WHY_STORED_RESPONSE)),
    _regex("GET", "/claude-code/plugins/{plugin_name}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/openai/v1/responses/{response_id}", _passthrough(_WHY_STORED_RESPONSE)),
    _regex("PUT", "/claude-code/plugins/{plugin_name}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("GET", "/public/v1/model_hub/{facet}", _passthrough(_WHY_PUBLIC)),
    _regex(
        "POST", "/openai/deployments/{model:path}/images/generations", _refuse(_WHY_NOT_REWRITTEN)
    ),
    _regex("POST", "/openai/deployments/{model:path}/chat/completions", _rewritten(_WHY_CHAT)),
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
    _regex("POST", "/sso/cli/complete/{login_id}", _passthrough(_WHY_AUTH, _JUST_AUTH)),
    _regex("DELETE", "/v1/access_group/{access_group_id}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/v1/access_group/{access_group_id}", _passthrough(_WHY_ADMIN)),
    _regex("PATCH", "/v2/organization/{organization_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("PUT", "/v1/access_group/{access_group_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("GET", "/scim/v2/Schemas/{schema_id:path}", _passthrough(_WHY_ADMIN)),
    _regex("DELETE", "/v1/mcp/toolset/{toolset_id}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/v1/mcp/toolset/{toolset_id}", _passthrough(_WHY_ADMIN)),
    _regex("DELETE", "/scim/v2/Groups/{group_id}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/scim/v2/Groups/{group_id}", _passthrough(_WHY_ADMIN)),
    _regex("PATCH", "/scim/v2/Groups/{group_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("PUT", "/scim/v2/Groups/{group_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex(
        "GET",
        "/vector_stores/{vector_store_id}/files/{file_id}/content",
        _refuse(_WHY_NOT_REWRITTEN),
    ),
    _regex(
        "GET", "/v1/mcp/server/{server_id}/oauth-user-credential/status", _passthrough(_WHY_ADMIN)
    ),
    _regex(
        "POST",
        "/v1beta/models/{model_name:path}:streamGenerateContent",
        _refuse(_WHY_NOT_REWRITTEN),
    ),
    _regex("DELETE", "/v1/mcp/server/{server_id}/oauth-user-credential", _passthrough(_WHY_ADMIN)),
    _regex(
        "DELETE", "/vector_stores/{vector_store_id}/files/{file_id}", _refuse(_WHY_NOT_REWRITTEN)
    ),
    _regex("GET", "/vector_stores/{vector_store_id}/files/{file_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex(
        "POST",
        "/v1/mcp/server/{server_id}/oauth-user-credential",
        _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ),
    _regex("POST", "/v1beta/models/{model_name:path}:generateContent", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/vector_stores/{vector_store_id}/files/{file_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/v1beta/models/{model_name:path}:countTokens", _refuse(_WHY_COUNT)),
    _regex("POST", "/vector_stores/{vector_store_id:path}/search", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/v1/mcp/server/{server_id}/user-credential", _passthrough(_WHY_ADMIN)),
    _regex(
        "POST", "/v1/mcp/server/{server_id}/user-credential", _passthrough(_WHY_ADMIN, _JUST_ADMIN)
    ),
    _regex("DELETE", "/policies/name/{policy_name}/all-versions", _passthrough(_WHY_ADMIN)),
    _regex("DELETE", "/v1/mcp/server/{server_id}/user-env-vars", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/v1/mcp/server/{server_id}/user-env-vars", _passthrough(_WHY_ADMIN)),
    _regex(
        "POST", "/v1/mcp/server/{server_id}/user-env-vars", _passthrough(_WHY_ADMIN, _JUST_ADMIN)
    ),
    _regex("GET", "/vector_stores/{vector_store_id}/files", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/vector_stores/{vector_store_id}/files", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/policies/name/{policy_name}/versions", _passthrough(_WHY_ADMIN)),
    _regex("POST", "/policies/name/{policy_name}/versions", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("DELETE", "/v1/assistants/{assistant_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/v1/mcp/server/{server_id}/approve", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("PUT", "/v1/mcp/server/{server_id}/reject", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
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
    _regex("GET", "/spend/logs/ui/{request_id}", _passthrough(_WHY_SPEND)),
    _regex("DELETE", "/v1/mcp/server/{server_id}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/v1/mcp/server/{server_id}", _passthrough(_WHY_ADMIN)),
    _regex("DELETE", "/scim/v2/Users/{user_id}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/scim/v2/Users/{user_id}", _passthrough(_WHY_ADMIN)),
    _regex("PATCH", "/scim/v2/Users/{user_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("PUT", "/scim/v2/Users/{user_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
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
    _regex("DELETE", "/access_group/{access_group}/budget", _passthrough(_WHY_ADMIN)),
    _regex("DELETE", "/access_group/{access_group}/delete", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/access_group/{access_group}/budget", _passthrough(_WHY_ADMIN)),
    _regex("PUT", "/access_group/{access_group}/budget", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("PUT", "/access_group/{access_group}/update", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex(
        "POST",
        "/v1/responses/{response_id}/cancel",
        _passthrough(_WHY_STORED_RESPONSE, _JUST_STORED_RESPONSE),
    ),
    _regex("GET", "/access_group/{access_group}/info", _passthrough(_WHY_ADMIN)),
    _regex("DELETE", "/interactions/{interaction_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/search_tools/{search_tool_id}", _refuse(_WHY_SEARCH)),
    _regex("GET", "/interactions/{interaction_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/search_tools/{search_tool_id}", _refuse(_WHY_SEARCH)),
    _regex("PUT", "/search_tools/{search_tool_id}", _refuse(_WHY_SEARCH)),
    _regex("DELETE", "/v1/responses/{response_id}", _passthrough(_WHY_STORED_RESPONSE)),
    _regex("GET", "/v1/responses/{response_id}", _passthrough(_WHY_STORED_RESPONSE)),
    _regex("GET", "/sso/cli/poll/{key_id}", _passthrough(_WHY_AUTH)),
    _regex("DELETE", "/credentials/{credential_name:path}", _passthrough(_WHY_ADMIN)),
    _regex("PATCH", "/credentials/{credential_name:path}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("GET", "/policy/info/{policy_name}", _passthrough(_WHY_ADMIN)),
    _regex("POST", "/v1/batches/{batch_id:path}/cancel", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/v1/threads/{thread_id}/messages", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/v1/threads/{thread_id}/messages", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/assistants/{assistant_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/guardrails/{guardrail_id}/info", _passthrough(_WHY_ADMIN)),
    _regex("POST", "/v1/threads/{thread_id}/runs", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/v1/batches/{batch_id:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/assemblyai/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/containers/{container_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/guardrails/{guardrail_id}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/containers/{container_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/guardrails/{guardrail_id}", _passthrough(_WHY_ADMIN)),
    _regex("PATCH", "/guardrails/{guardrail_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("PUT", "/guardrails/{guardrail_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
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
    _regex("POST", "/lazy/warm/{name}", _passthrough(_WHY_LAZY, _JUST_LAZY)),
    _regex("GET", "/policies/{policy_id}/resolved-guardrails", _passthrough(_WHY_ADMIN)),
    _regex("DELETE", "/v1/evals/{eval_id}/runs/{run_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1/evals/{eval_id}/runs/{run_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/v1/evals/{eval_id}/runs/{run_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1/files/{file_id:path}/content", _refuse(_WHY_PROVIDER_STORE)),
    _regex("PUT", "/policies/{policy_id}/status", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
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
    _regex("DELETE", "/policies/{policy_id}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/policies/{policy_id}", _passthrough(_WHY_ADMIN)),
    _regex("PUT", "/policies/{policy_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("DELETE", "/v1/evals/{eval_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("GET", "/v1/evals/{eval_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("POST", "/v1/evals/{eval_id}", _refuse(_WHY_NOT_REWRITTEN)),
    _regex("DELETE", "/fallback/{model}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/fallback/{model}", _passthrough(_WHY_ADMIN)),
    _regex("POST", "/engines/{model:path}/chat/completions", _rewritten(_WHY_CHAT)),
    _regex("DELETE", "/v1/tool/{tool_name:path}/overrides", _passthrough(_WHY_ADMIN)),
    _regex("POST", "/engines/{model:path}/completions", _refuse(_WHY_PROMPT)),
    _regex("GET", "/v1/tool/{tool_name:path}/detail", _passthrough(_WHY_ADMIN)),
    _regex("POST", "/engines/{model:path}/embeddings", _refuse(_WHY_NO_REWRITE)),
    _regex("POST", "/batches/{batch_id:path}/cancel", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/v1/tool/{tool_name:path}/logs", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/prompts/{prompt_id}/versions", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/threads/{thread_id}/messages", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/threads/{thread_id}/messages", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/toolset/{toolset_name}/mcp", _refuse(_WHY_MCP)),
    _regex("GET", "/toolset/{toolset_name}/mcp", _refuse(_WHY_MCP)),
    _regex("OPTIONS", "/toolset/{toolset_name}/mcp", _refuse(_WHY_MCP)),
    _regex("PATCH", "/toolset/{toolset_name}/mcp", _refuse(_WHY_MCP)),
    _regex("POST", "/toolset/{toolset_name}/mcp", _refuse(_WHY_MCP)),
    _regex("PUT", "/toolset/{toolset_name}/mcp", _refuse(_WHY_MCP)),
    _regex("GET", "/prompts/{prompt_id}/info", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/v1/tool/{tool_name:path}", _passthrough(_WHY_ADMIN)),
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
    _regex("DELETE", "/prompts/{prompt_id}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/prompts/{prompt_id}", _passthrough(_WHY_ADMIN)),
    _regex("GET", "/threads/{thread_id}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/prompts/{prompt_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("PUT", "/prompts/{prompt_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
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
    _regex("PATCH", "/model/{model_id}/update", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("DELETE", "/azure/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/azure/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/azure/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/azure/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/azure/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("DELETE", "/files/{file_id:path}", _refuse(_WHY_PROVIDER_STORE)),
    _regex("GET", "/files/{file_id:path}", _refuse(_WHY_PROVIDER_STORE)),
    _regex("DELETE", "/team/{team_id:path}/callback/{callback_name}", _passthrough(_WHY_ADMIN)),
    _regex(
        "POST",
        "/team/{team_id}/member/{user_id}/reset_spend",
        _passthrough(_WHY_ADMIN, _JUST_ADMIN),
    ),
    _regex("POST", "/team/{team_id}/disable_logging", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("GET", "/team/{team_id:path}/callback", _passthrough(_WHY_ADMIN)),
    _regex("POST", "/team/{team_id:path}/callback", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("GET", "/team/{team_id}/members/me", _passthrough(_WHY_ADMIN)),
    _regex("DELETE", "/vllm/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("GET", "/vllm/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/vllm/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("POST", "/vllm/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PUT", "/vllm/{endpoint:path}", _refuse(_WHY_PROVIDER)),
    _regex("PATCH", "/team/{team_id}", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("GET", "/a2a/{agent_id}/.well-known/agent-card.json", _refuse(_WHY_AGENT)),
    _regex("GET", "/a2a/{agent_id}/.well-known/agent.json", _refuse(_WHY_AGENT)),
    _regex("POST", "/a2a/{agent_id}/message/send", _refuse(_WHY_AGENT)),
    _regex("POST", "/key/{key:path}/reset_spend", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
    _regex("POST", "/key/{key:path}/regenerate", _passthrough(_WHY_ADMIN, _JUST_ADMIN)),
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

# Only what Task 4 mounts on litellm's app: `build_health_router()` at
# /healthz/* and the metrics exposition at /metrics. HEAD needs no row — it
# inherits the GET verdict in classify.py, and the router answers 405 to it.
# POST /internal/issue-token is deliberately absent: the router is not mounted
# for it, so the gate refuses it as unlisted.
GATEWAY_ROUTE_TABLE: dict[tuple[str, str], Entry] = {
    ("GET", "/healthz/extensions"): _passthrough("gateway-owned health check; no user text"),
    ("GET", "/healthz/live"): _passthrough("gateway-owned liveness probe; Helm probes it"),
    ("GET", "/healthz/ready"): _passthrough("gateway-owned readiness probe; Helm probes it"),
    ("GET", "/healthz/sanitization"): _passthrough("gateway-owned health check; no user text"),
    ("GET", "/metrics"): _passthrough("gateway-owned Prometheus exposition; no user text"),
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
    newline separated) into PASSTHROUGH entries. Raises on anything malformed —
    an operator typo must fail at load, not widen the gate by accident."""
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
        extras[(method, path)] = _passthrough(
            f"operator extra: CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH lists {method} {path}"
        )
    return extras
