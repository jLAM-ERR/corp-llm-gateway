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
    *(entry for _, _, entry in LITELLM_REGEX_TABLE),
]


def test_every_exact_key_is_an_uppercase_method_and_an_absolute_path() -> None:
    for table in EXACT_TABLES:
        for method, path in table:
            assert method in HTTP_METHODS, f"{method} {path}"
            assert method == method.upper(), f"{method} {path}"
            assert path.startswith("/"), f"{method} {path}"


def test_every_regex_row_is_an_uppercase_method_and_an_absolute_pattern() -> None:
    for method, pattern, _ in LITELLM_REGEX_TABLE:
        assert method in HTTP_METHODS, f"{method} {pattern.pattern}"
        assert method == method.upper(), f"{method} {pattern.pattern}"
        assert pattern.pattern.startswith("\\A/"), f"{method} {pattern.pattern}"


def test_no_head_route_is_rewritten() -> None:
    # HEAD carries no body; a REWRITTEN HEAD row would promise a rewrite that
    # cannot happen. classify() downgrades one, but none may exist.
    rows = [(method, entry) for table in EXACT_TABLES for (method, _), entry in table.items()]
    rows += [(method, entry) for method, _, entry in LITELLM_REGEX_TABLE]
    assert [
        method for method, entry in rows if method == "HEAD" and entry.verdict is Verdict.REWRITTEN
    ] == []


def test_no_table_lists_a_websocket_route() -> None:
    # Handshakes are refused at scope level; a WEBSOCKET row could only mislead.
    methods = {method for table in EXACT_TABLES for method, _ in table}
    methods |= {method for method, _, _ in LITELLM_REGEX_TABLE}
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


def test_every_body_carrying_passthrough_carries_a_justification() -> None:
    # A POST/PUT/PATCH the hook never sees is admitted only with a written
    # no-egress reason; "admin" alone is not one.
    routes = [
        (f"{method} {path}", entry)
        for table in EXACT_TABLES
        for (method, path), entry in table.items()
    ]
    routes += [
        (f"{method} {pattern.pattern}", entry) for method, pattern, entry in LITELLM_REGEX_TABLE
    ]
    offenders = [
        route
        for route, entry in routes
        if route.split()[0] in BODY_METHODS
        and entry.verdict is Verdict.PASSTHROUGH
        and not (entry.justification or "").strip()
    ]
    assert offenders == []


def test_regex_entries_are_anchored() -> None:
    for _, pattern, _ in LITELLM_REGEX_TABLE:
        assert pattern.pattern.startswith("\\A")
        assert pattern.pattern.endswith("\\Z")


@pytest.mark.parametrize(
    ("method", "path", "verdict"),
    [
        ("GET", "/v1/responses/resp_123", Verdict.PASSTHROUGH),
        ("DELETE", "/openai/v1/responses/resp_123", Verdict.PASSTHROUGH),
        ("POST", "/v1/responses/resp_123/cancel", Verdict.PASSTHROUGH),
        ("POST", "/engines/gpt-4o/chat/completions", Verdict.REWRITTEN),
        ("POST", "/openai/deployments/gpt-4o/chat/completions", Verdict.REWRITTEN),
        ("POST", "/lazy/warm/mcp", Verdict.PASSTHROUGH),
    ],
)
def test_regex_table_resolves_path_parameters(method: str, path: str, verdict: Verdict) -> None:
    entry = lookup(method, path)
    assert entry is not None and entry.verdict is verdict


def test_the_gateway_owned_routes_are_listed_for_get() -> None:
    # HEAD needs no row of its own — classify() inherits the GET verdict.
    for path in ("/healthz/live", "/healthz/ready", "/metrics"):
        assert lookup("GET", path) is not None, path
    assert lookup("HEAD", "/healthz/live") is None


def test_issue_token_is_not_in_the_gateway_table() -> None:
    # Task 4 mounts /healthz/* and /metrics only; issuance stays unlisted.
    assert lookup("POST", "/internal/issue-token") is None


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
