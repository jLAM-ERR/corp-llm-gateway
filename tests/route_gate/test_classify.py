from __future__ import annotations

import pytest

from corp_llm_gateway.route_gate import (
    ROUTE_GATE_LISTED,
    ROUTE_GATE_MALFORMED,
    ROUTE_GATE_UNLISTED,
    ROUTE_GATE_WEBSOCKET,
    Entry,
    Verdict,
    classify,
    parse_extras,
)


@pytest.mark.parametrize(
    ("method", "path", "verdict", "block_reason"),
    [
        ("POST", "/v1/messages", Verdict.REWRITTEN, None),
        ("POST", "/v1/chat/completions", Verdict.REWRITTEN, None),
        ("GET", "/v1/models", Verdict.PASSTHROUGH, None),
        ("GET", "/healthz/live", Verdict.PASSTHROUGH, None),
        ("POST", "/v1/messages/count_tokens", Verdict.REFUSE, ROUTE_GATE_LISTED),
        ("POST", "/utils/token_counter", Verdict.REFUSE, ROUTE_GATE_LISTED),
        ("POST", "/v1/some/future/route", Verdict.REFUSE, ROUTE_GATE_UNLISTED),
        ("GET", "/v1/some/future/route", Verdict.REFUSE, ROUTE_GATE_UNLISTED),
    ],
)
def test_one_case_per_verdict(
    method: str, path: str, verdict: Verdict, block_reason: str | None
) -> None:
    decision = classify(method, path, path.encode())
    assert decision.verdict is verdict
    assert decision.block_reason == block_reason
    assert decision.why


def test_the_method_is_upper_cased_but_the_path_is_taken_as_received() -> None:
    assert classify("post", "/v1/messages", b"/v1/messages").verdict is Verdict.REWRITTEN


def test_lifespan_scopes_are_forwarded() -> None:
    decision = classify("", "", None, scope_type="lifespan")
    assert decision.verdict is Verdict.PASSTHROUGH
    assert decision.block_reason is None


def test_a_websocket_scope_is_refused() -> None:
    decision = classify("GET", "/v1/responses", b"/v1/responses", scope_type="websocket")
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_WEBSOCKET


def test_an_upgrade_header_on_an_admitted_path_is_refused() -> None:
    decision = classify("POST", "/v1/messages", b"/v1/messages", upgrade_header="WebSocket")
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_WEBSOCKET


def test_an_unknown_scope_type_is_refused() -> None:
    decision = classify("POST", "/v1/messages", b"/v1/messages", scope_type="quic")
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_UNLISTED


@pytest.mark.parametrize(
    "path", ["/v1/messages/", "/V1/Messages", "//v1//messages", "/v1/messages/../v1/messages"]
)
def test_near_misses_are_never_rewritten(path: str) -> None:
    decision = classify("POST", path, path.encode())
    assert decision.verdict is Verdict.REFUSE


@pytest.mark.parametrize(
    ("path", "raw_path"),
    [
        ("/v1/messages/../../key/generate", b"/v1/messages/..%2f..%2fkey/generate"),
        ("/v1/messages/../../key/generate", b"/v1/messages/..%2F..%2Fkey/generate"),
        ("/v1/messages\x00", b"/v1/messages%00"),
        ("/v1/messages", b"/v1/%2E%2E%2Fmessages"),
        ("//v1//messages", b"//v1//messages"),
        ("/v1/messäges", "/v1/messäges".encode()),
    ],
)
def test_encoded_and_traversing_paths_are_refused_as_malformed(path: str, raw_path: bytes) -> None:
    decision = classify("POST", path, raw_path)
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_MALFORMED


def test_head_inherits_the_get_entry() -> None:
    decision = classify("HEAD", "/health/liveliness", b"/health/liveliness")
    assert decision.verdict is Verdict.PASSTHROUGH


def test_head_on_a_route_with_no_get_entry_is_unlisted() -> None:
    decision = classify("HEAD", "/v1/messages", b"/v1/messages")
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_UNLISTED


def test_head_never_inherits_a_rewritten_verdict() -> None:
    # No shipped GET entry is REWRITTEN, so the rule is asserted on a synthetic one.
    extras = {("GET", "/synthetic/rewrite"): Entry(Verdict.REWRITTEN, "synthetic")}
    decision = classify("HEAD", "/synthetic/rewrite", b"/synthetic/rewrite", extras=extras)
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_LISTED


def test_extras_add_passthrough_routes() -> None:
    extras = parse_extras("POST /internal/ops-webhook, GET /internal/ops-status")
    decision = classify("POST", "/internal/ops-webhook", b"/internal/ops-webhook", extras=extras)
    assert decision.verdict is Verdict.PASSTHROUGH
    assert classify("GET", "/internal/ops-status", extras=extras).verdict is Verdict.PASSTHROUGH


def test_extras_can_only_be_passthrough() -> None:
    for entry in parse_extras(["POST /internal/ops-webhook", "GET /internal/x"]).values():
        assert entry.verdict is Verdict.PASSTHROUGH


def test_an_extra_never_overrides_a_listed_refusal() -> None:
    extras = parse_extras(["POST /v1/messages/count_tokens"])
    decision = classify(
        "POST", "/v1/messages/count_tokens", b"/v1/messages/count_tokens", extras=extras
    )
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_LISTED


@pytest.mark.parametrize(
    "raw",
    [
        "/internal/ops",
        "POST",
        "POST /a /b",
        "POST internal/ops",
        "WEBSOCKET /v1/responses",
        "TRACE /internal/ops",
    ],
)
def test_a_malformed_extra_raises_at_load(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_extras(raw)


def test_empty_extras_are_accepted() -> None:
    assert parse_extras(None) == {}
    assert parse_extras("") == {}
    assert parse_extras(" , \n ") == {}
