from __future__ import annotations

import subprocess
import sys

import pytest

from corp_llm_gateway.route_gate import (
    GATEWAY_ROUTE_TABLE,
    HTTP_METHODS,
    LITELLM_REGEX_TABLE,
    LITELLM_ROUTE_TABLE,
    Entry,
    Verdict,
    lookup,
)

# The trees litellm keeps for its own admin surface. A REWRITTEN entry here
# would mean the gate promised sanitization on a route that has no hook.
MANAGEMENT_PREFIXES = (
    "/budget",
    "/config",
    "/credentials",
    "/customer",
    "/global",
    "/key",
    "/lazy",
    "/login",
    "/model",
    "/organization",
    "/spend",
    "/sso",
    "/team",
    "/ui",
    "/user",
    "/v1/access_group",
    "/v1/mcp",
    "/v1/model",
    "/v1/tool",
    "/v1/unified_access_group",
)

BODY_METHODS = ("POST", "PUT", "PATCH")

EXACT_TABLES = (LITELLM_ROUTE_TABLE, GATEWAY_ROUTE_TABLE)

ALL_ENTRIES = [
    *(entry for table in EXACT_TABLES for entry in table.values()),
    *(row.entry for row in LITELLM_REGEX_TABLE),
]


def test_every_exact_key_is_an_uppercase_method_and_an_absolute_path() -> None:
    for table in EXACT_TABLES:
        for method, path in table:
            assert method in HTTP_METHODS, f"{method} {path}"
            assert method == method.upper(), f"{method} {path}"
            assert path.startswith("/"), f"{method} {path}"


def test_every_regex_row_is_an_uppercase_method_and_an_absolute_pattern() -> None:
    for row in LITELLM_REGEX_TABLE:
        assert row.method in HTTP_METHODS, row.template
        assert row.method == row.method.upper(), row.template
        assert row.template.startswith("/"), row.template
        assert row.pattern.pattern.startswith("\\A/"), row.template


def test_no_head_route_is_rewritten() -> None:
    # HEAD carries no body; a REWRITTEN HEAD row would promise a rewrite that
    # cannot happen. classify() downgrades one, but none may exist.
    rows = [(method, entry) for table in EXACT_TABLES for (method, _), entry in table.items()]
    rows += [(row.method, row.entry) for row in LITELLM_REGEX_TABLE]
    assert [
        method for method, entry in rows if method == "HEAD" and entry.verdict is Verdict.REWRITTEN
    ] == []


def test_no_table_lists_a_websocket_route() -> None:
    # Handshakes are refused at scope level; a WEBSOCKET row could only mislead.
    methods = {method for table in EXACT_TABLES for method, _ in table}
    methods |= {row.method for row in LITELLM_REGEX_TABLE}
    assert "WEBSOCKET" not in methods


def test_every_entry_says_why() -> None:
    for entry in ALL_ENTRIES:
        assert entry.why.strip()


def test_no_rewritten_entry_under_a_management_path() -> None:
    rewritten = [
        path
        for table in EXACT_TABLES
        for (_, path), entry in table.items()
        if entry.verdict is Verdict.REWRITTEN
    ]
    for path in rewritten:
        assert not path.startswith(MANAGEMENT_PREFIXES), path


def test_gateway_table_never_rewrites() -> None:
    for entry in GATEWAY_ROUTE_TABLE.values():
        assert entry.verdict is Verdict.PASSTHROUGH


@pytest.mark.parametrize(
    "path",
    [
        "/v1/messages/count_tokens",
        "/v1/responses/input_tokens",
        "/responses/input_tokens",
        "/openai/v1/responses/input_tokens",
        "/utils/token_counter",
        "/queue/chat/completions",
        "/v1/completions",
        "/completions",
        "/v1/embeddings",
        "/v1/moderations",
        "/v1/audio/speech",
        "/mcp-rest/tools/call",
        "/apply_guardrail",
        "/guardrails/apply_guardrail",
        "/health/test_connection",
        "/search/brave",
    ],
)
def test_the_bypass_routes_are_refused(path: str) -> None:
    entry = lookup("POST", path)
    assert entry is not None, path
    assert entry.verdict is Verdict.REFUSE, path


@pytest.mark.parametrize(
    "path",
    ["/v1/messages", "/v1/chat/completions", "/v1/responses", "/v1/responses/compact"],
)
def test_the_hook_backed_routes_are_rewritten(path: str) -> None:
    entry = lookup("POST", path)
    assert entry is not None and entry.verdict is Verdict.REWRITTEN


def test_the_rewritten_set_is_exactly_eight_spellings() -> None:
    # Eight, not eleven: the three `/openai/…` spellings are also matched by
    # litellm's hook-less `/openai/{endpoint:path}` raw passthrough, so their
    # rewrite would rest on include order. They are REFUSE.
    rewritten = [
        f"{method} {path}"
        for table in EXACT_TABLES
        for (method, path), entry in table.items()
        if entry.verdict is Verdict.REWRITTEN
    ]
    rewritten += [
        f"{row.method} {row.template}"
        for row in LITELLM_REGEX_TABLE
        if row.entry.verdict is Verdict.REWRITTEN
    ]
    assert sorted(rewritten) == [
        "POST /chat/completions",
        "POST /engines/{model:path}/chat/completions",
        "POST /responses",
        "POST /responses/compact",
        "POST /v1/chat/completions",
        "POST /v1/messages",
        "POST /v1/responses",
        "POST /v1/responses/compact",
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/openai/v1/responses",
        "/openai/v1/responses/compact",
        "/openai/deployments/gpt-4o/chat/completions",
    ],
)
def test_the_openai_generation_spellings_are_refused(path: str) -> None:
    entry = lookup("POST", path)
    assert entry is not None and entry.verdict is Verdict.REFUSE


def test_every_body_carrying_passthrough_carries_a_justification() -> None:
    # A POST/PUT/PATCH the hook never sees is admitted only with a written
    # no-egress reason; "admin" alone is not one.
    routes = [
        (f"{method} {path}", entry)
        for table in EXACT_TABLES
        for (method, path), entry in table.items()
    ]
    routes += [(f"{row.method} {row.template}", row.entry) for row in LITELLM_REGEX_TABLE]
    offenders = [
        route
        for route, entry in routes
        if route.split()[0] in BODY_METHODS
        and entry.verdict is Verdict.PASSTHROUGH
        and not (entry.justification or "").strip()
    ]
    assert offenders == []


def test_regex_entries_are_anchored() -> None:
    for row in LITELLM_REGEX_TABLE:
        assert row.pattern.pattern.startswith("\\A")
        assert row.pattern.pattern.endswith("\\Z")


@pytest.mark.parametrize(
    ("method", "path", "verdict"),
    [
        ("GET", "/v1/responses/resp_123", Verdict.PASSTHROUGH),
        ("DELETE", "/openai/v1/responses/resp_123", Verdict.PASSTHROUGH),
        ("POST", "/v1/responses/resp_123/cancel", Verdict.PASSTHROUGH),
        ("POST", "/engines/gpt-4o/chat/completions", Verdict.REWRITTEN),
        ("POST", "/openai/deployments/gpt-4o/chat/completions", Verdict.REFUSE),
        ("POST", "/lazy/warm/mcp", Verdict.PASSTHROUGH),
    ],
)
def test_regex_table_resolves_path_parameters(method: str, path: str, verdict: Verdict) -> None:
    entry = lookup(method, path)
    assert entry is not None and entry.verdict is verdict


def test_every_regex_row_is_reachable() -> None:
    # A catch-all row ordered ahead of a specific one would silently swallow it;
    # each row must still answer for the template it was generated from.
    # Identity, not equality: `Entry` is a value object, so two rows that happen
    # to share a verdict and a `why` compare equal and a shadow would pass.
    shadowed = [
        f"{row.method} {row.template}"
        for row in LITELLM_REGEX_TABLE
        if lookup(row.method, row.template) is not row.entry
    ]
    assert shadowed == []


def test_the_regex_rows_are_ordered_specific_first() -> None:
    statics = [len(row.template.split("{", 1)[0]) for row in LITELLM_REGEX_TABLE]
    assert statics == sorted(statics, reverse=True)


def test_the_table_is_the_whole_collected_surface() -> None:
    # Regenerated from litellm 1.101.0; a table this small would mean the
    # generator ran against a partial parse.
    assert len(LITELLM_ROUTE_TABLE) > 500
    assert len(LITELLM_REGEX_TABLE) > 300


def test_the_gateway_owned_routes_are_listed_for_get() -> None:
    # HEAD needs no row of its own — classify() inherits the GET verdict.
    for path in ("/healthz/live", "/healthz/ready", "/metrics"):
        assert lookup("GET", path) is not None, path
    assert lookup("HEAD", "/healthz/live") is None


def test_issue_token_is_a_gateway_owned_passthrough_row() -> None:
    # The HealthRouter answers it locally (404 when issuance is off); nothing is
    # forwarded to litellm, so it is PASSTHROUGH and never REWRITTEN.
    entry = GATEWAY_ROUTE_TABLE[("POST", "/internal/issue-token")]
    assert entry.verdict is Verdict.PASSTHROUGH
    assert entry.why == "gateway-owned issuance; terminates in the gateway, nothing forwarded"
    assert (entry.justification or "").strip()
    assert lookup("POST", "/internal/issue-token") is entry


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_issue_token_is_listed_for_post_only(method: str) -> None:
    assert lookup(method, "/internal/issue-token") is None


def test_the_gateway_table_is_exactly_the_gateway_owned_routes() -> None:
    assert sorted(GATEWAY_ROUTE_TABLE) == [
        ("GET", "/healthz/extensions"),
        ("GET", "/healthz/live"),
        ("GET", "/healthz/ready"),
        ("GET", "/healthz/sanitization"),
        ("GET", "/metrics"),
        ("POST", "/internal/issue-token"),
    ]


def test_the_gateway_table_is_disjoint_from_litellms() -> None:
    assert not set(GATEWAY_ROUTE_TABLE) & set(LITELLM_ROUTE_TABLE)
    for method, path in GATEWAY_ROUTE_TABLE:
        assert not [
            row for row in LITELLM_REGEX_TABLE if row.method == method and row.pattern.match(path)
        ], f"{method} {path}"


def test_lookup_returns_none_for_an_unknown_pair() -> None:
    assert lookup("POST", "/v1/some/future/route") is None
    assert lookup("POST", "/v1/models") is None
    assert lookup("GET", "/v1/messages") is None


def test_an_entry_without_a_why_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty why"):
        Entry(Verdict.REFUSE, "   ")


def test_a_justification_on_a_non_passthrough_entry_is_rejected() -> None:
    with pytest.raises(ValueError, match="PASSTHROUGH entries only"):
        Entry(Verdict.REWRITTEN, "why", justification="no egress")


def test_an_entry_whose_verdict_is_not_a_verdict_is_rejected() -> None:
    with pytest.raises(ValueError, match="needs a Verdict"):
        Entry("passthrough", "why")  # type: ignore[arg-type]


def test_the_table_imports_without_pulling_in_audit_or_metrics() -> None:
    # `config check` resolves the extras key; it must not drag the middleware's
    # audit/metrics stack in through the package __init__.
    source = (
        "import sys\n"
        "from corp_llm_gateway.route_gate.table import parse_extras\n"
        "parse_extras('GET /internal/ops-status')\n"
        "print(','.join(m for m in sys.modules if m.startswith('corp_llm_gateway.')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=True
    ).stdout
    loaded = set(out.strip().split(","))
    assert not {
        m for m in loaded if m.startswith(("corp_llm_gateway.audit", "corp_llm_gateway.metrics"))
    }
