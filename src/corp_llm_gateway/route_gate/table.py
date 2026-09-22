"""The route table: the single place a litellm route is classified.

Every request the gateway serves is looked up here before litellm's router sees
it. A pair the tables do not know is refused (default-deny), so a route litellm
adds in a future bump cannot reach a provider until someone classifies it.

``LITELLM_ROUTE_TABLE`` / ``LITELLM_REGEX_TABLE`` describe routes litellm
registers; ``GATEWAY_ROUTE_TABLE`` describes the routes the gateway itself
mounts (``/healthz/*``, ``/metrics``) and is exempt from the source guard.

# PROVISIONAL until Task 3 regenerates it from litellm source
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


def _rewritten(why: str) -> Entry:
    return Entry(Verdict.REWRITTEN, why)


def _passthrough(why: str, justification: str | None = None) -> Entry:
    return Entry(Verdict.PASSTHROUGH, why, justification)


def _refuse(why: str) -> Entry:
    return Entry(Verdict.REFUSE, why)


def _anchored(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"\A{pattern}\Z")


_WHY_MESSAGES = "Anthropic Messages; the hook rewrites messages/system"
_WHY_CHAT = "chat completions; the hook rewrites messages"
_WHY_RESPONSES = "Responses API; the hook rewrites input"
_WHY_COMPACT = "Responses compact; the hook rewrites input"
_WHY_NO_REWRITE = "hook no-rewrite set (_NON_CHAT_INPUT_CALL_TYPES); a DLP scan is not sanitization"
_WHY_PROMPT = "prompt is never read by the hook, so nothing is rewritten"
_WHY_COUNT = "handler never calls pre_call_hook; clients use usage.input_tokens from the real turn"
_WHY_SEARCH = "query text goes to a search backend without the hook"
_WHY_HEALTH = "litellm health probe; no user text"
_WHY_MODELS = "model list; no user text"
_WHY_STORED_RESPONSE = "stored response addressed by id; no inbound user text"

LITELLM_ROUTE_TABLE: dict[tuple[str, str], Entry] = {
    ("GET", "/"): _passthrough("litellm home; no user text"),
    ("GET", "/cursor/models"): _passthrough(_WHY_MODELS),
    ("GET", "/cursor/v1/models"): _passthrough(_WHY_MODELS),
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
    ("GET", "/health/services"): _passthrough(_WHY_HEALTH),
    ("GET", "/health/shared-status"): _passthrough(_WHY_HEALTH),
    ("GET", "/key/info"): _passthrough("admin read of a virtual key; no user text"),
    ("GET", "/model/info"): _passthrough("model metadata; no user text"),
    ("GET", "/models"): _passthrough(_WHY_MODELS),
    ("GET", "/routes"): _passthrough("litellm's own route list; no user text"),
    ("GET", "/v1/model/info"): _passthrough("model metadata; no user text"),
    ("GET", "/v1/models"): _passthrough(_WHY_MODELS),
    ("OPTIONS", "/health/liveliness"): _passthrough("CORS preflight for a health probe"),
    ("OPTIONS", "/health/liveness"): _passthrough("CORS preflight for a health probe"),
    ("OPTIONS", "/health/readiness"): _passthrough("CORS preflight for a health probe"),
    ("POST", "/apply_guardrail"): _refuse("runs caller text through a guardrail without the hook"),
    ("POST", "/audio/speech"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/chat/completions"): _rewritten(_WHY_CHAT),
    ("POST", "/completions"): _refuse(_WHY_PROMPT),
    ("POST", "/cursor/chat/completions"): _refuse(
        "cursor-compat chat spelling; REWRITTEN only once the guard proves it reaches the hook"
    ),
    ("POST", "/embeddings"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/guardrails/apply_guardrail"): _refuse(
        "runs caller text through a guardrail without the hook"
    ),
    ("POST", "/health/test_connection"): _refuse(
        "sends a caller-supplied probe to a model without the hook"
    ),
    ("POST", "/mcp-rest/tools/call"): _refuse("tool arguments reach an MCP server, no hook"),
    ("POST", "/moderations"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/openai/v1/responses"): _rewritten(_WHY_RESPONSES),
    ("POST", "/openai/v1/responses/compact"): _rewritten(_WHY_COMPACT),
    ("POST", "/openai/v1/responses/input_tokens"): _refuse(_WHY_COUNT),
    ("POST", "/queue/chat/completions"): _refuse("queued generation; never reaches the hook"),
    ("POST", "/rag/ingest"): _refuse("ingested text the hook never reads"),
    ("POST", "/rag/query"): _refuse("query text the hook never reads"),
    ("POST", "/responses"): _rewritten(_WHY_RESPONSES),
    ("POST", "/responses/compact"): _rewritten(_WHY_COMPACT),
    ("POST", "/responses/input_tokens"): _refuse(_WHY_COUNT),
    ("POST", "/search"): _refuse(_WHY_SEARCH),
    ("POST", "/utils/token_counter"): _refuse(
        "?call_endpoint=true posts the raw body to the provider's counter; never reaches the hook"
    ),
    ("POST", "/v1/audio/speech"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/v1/chat/completions"): _rewritten(_WHY_CHAT),
    ("POST", "/v1/completions"): _refuse(_WHY_PROMPT),
    ("POST", "/v1/embeddings"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/v1/messages"): _rewritten(_WHY_MESSAGES),
    ("POST", "/v1/messages/count_tokens"): _refuse(_WHY_COUNT),
    ("POST", "/v1/moderations"): _refuse(_WHY_NO_REWRITE),
    ("POST", "/v1/rag/ingest"): _refuse("ingested text the hook never reads"),
    ("POST", "/v1/rag/query"): _refuse("query text the hook never reads"),
    ("POST", "/v1/responses"): _rewritten(_WHY_RESPONSES),
    ("POST", "/v1/responses/compact"): _rewritten(_WHY_COMPACT),
    ("POST", "/v1/responses/input_tokens"): _refuse(_WHY_COUNT),
    ("POST", "/v1/search"): _refuse(_WHY_SEARCH),
    ("POST", "/v1/tool/policy"): _passthrough(
        "tool-policy write; litellm admin surface",
        justification="writes a policy row in the management DB; the handler calls no provider",
    ),
    ("POST", "/v1/unified_access_group"): _passthrough(
        "access-group write; litellm admin surface",
        justification="access-group CRUD in the management DB; the handler calls no provider",
    ),
}

LITELLM_REGEX_TABLE: list[tuple[str, re.Pattern[str], Entry]] = [
    (
        "DELETE",
        _anchored(r"/(?:v1/|openai/v1/)?responses/[^/]+"),
        _passthrough(_WHY_STORED_RESPONSE),
    ),
    (
        "GET",
        _anchored(r"/(?:v1/|openai/v1/)?responses/[^/]+"),
        _passthrough(_WHY_STORED_RESPONSE),
    ),
    (
        "GET",
        _anchored(r"/(?:v1/|openai/v1/)?responses/[^/]+/input_items"),
        _passthrough(_WHY_STORED_RESPONSE),
    ),
    (
        "POST",
        _anchored(r"/(?:engines|openai/deployments)/.+/chat/completions"),
        _rewritten(_WHY_CHAT),
    ),
    (
        "POST",
        _anchored(r"/(?:engines|openai/deployments)/.+/embeddings"),
        _refuse(_WHY_NO_REWRITE),
    ),
    (
        "POST",
        _anchored(r"/(?:v1/|openai/v1/)?responses/[^/]+/cancel"),
        _passthrough(
            _WHY_STORED_RESPONSE,
            justification="cancels a stored response by id; no request body reaches a provider",
        ),
    ),
    (
        "POST",
        _anchored(r"/(?:v1/)?search/[^/]+"),
        _refuse(_WHY_SEARCH),
    ),
    (
        "POST",
        _anchored(r"/lazy/warm/[^/]+"),
        _passthrough(
            "lazy-router warm-up; litellm admin surface",
            justification="imports routers only (_lazy_features.py:370); calls no provider",
        ),
    ),
]

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
    for regex_method, pattern, regex_entry in LITELLM_REGEX_TABLE:
        if regex_method == verb and pattern.match(path):
            return regex_entry
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
