"""How the route gate turns a request into a verdict: PASSTHROUGH, REWRITTEN or REFUSE.

The verdict comes from the method and the raw path. A websocket or an upgrade request is refused,
an encoded or traversing path is malformed, HEAD takes its GET row and is never rewritten, and an
operator extra can only add a PASSTHROUGH route: it never undoes a refusal or promises a rewrite.
"""

from __future__ import annotations

import re

import pytest

from corp_llm_gateway.route_gate import (
    LITELLM_ROUTE_TABLE,
    ROUTE_GATE_ERROR,
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


def test_an_unknown_scope_type_is_refused_as_a_gate_error() -> None:
    decision = classify("POST", "/v1/messages", b"/v1/messages", scope_type="quic")
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_ERROR


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


def test_a_decoded_traversal_is_malformed_even_without_a_raw_path() -> None:
    decision = classify("POST", "/v1/messages/../key/generate", None)
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_MALFORMED


def test_head_inherits_the_get_entry() -> None:
    decision = classify("HEAD", "/health/liveliness", b"/health/liveliness")
    assert decision.verdict is Verdict.PASSTHROUGH


def test_head_inherits_a_get_entry_from_the_regex_table() -> None:
    decision = classify("HEAD", "/v1/responses/resp_1", b"/v1/responses/resp_1")
    assert decision.verdict is Verdict.PASSTHROUGH


def test_head_on_a_gateway_route_inherits_its_get_entry() -> None:
    decision = classify("HEAD", "/healthz/live", b"/healthz/live")
    assert decision.verdict is Verdict.PASSTHROUGH


def test_head_on_a_route_with_no_get_entry_is_unlisted() -> None:
    decision = classify("HEAD", "/v1/messages", b"/v1/messages")
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_UNLISTED


@pytest.mark.parametrize("seeded_method", ["GET", "HEAD"])
def test_head_is_never_rewritten(monkeypatch: pytest.MonkeyPatch, seeded_method: str) -> None:
    # No shipped entry is HEAD- or GET-REWRITTEN, so the rule is asserted on a
    # synthetic table row — both the inherited and the direct spelling.
    monkeypatch.setitem(
        LITELLM_ROUTE_TABLE,
        (seeded_method, "/synthetic/rewrite"),
        Entry(Verdict.REWRITTEN, "synthetic"),
    )
    decision = classify("HEAD", "/synthetic/rewrite", b"/synthetic/rewrite")
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


@pytest.mark.parametrize("path", ["/v1/messages/count_tokens", "/search/brave"])
def test_an_extra_never_overrides_a_listed_refusal(path: str) -> None:
    # The second path is refused by the regex table, which extras never reach.
    # parse_extras refuses the item at load; a hand-built extra proves the
    # runtime holds on its own.
    with pytest.raises(ValueError, match="refused"):
        parse_extras([f"POST {path}"])
    extras = {("POST", path): Entry(Verdict.PASSTHROUGH, "hand-built extra")}
    decision = classify("POST", path, path.encode(), extras=extras)
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_LISTED


def test_an_extra_can_never_promise_a_rewrite() -> None:
    extras = {("POST", "/internal/ops-webhook"): Entry(Verdict.REWRITTEN, "hand-built")}
    decision = classify("POST", "/internal/ops-webhook", b"/internal/ops-webhook", extras=extras)
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_UNLISTED


def test_issue_token_passes_to_the_gateway_owned_router() -> None:
    # The HealthRouter terminates it (404 locally when issuance is off).
    decision = classify("POST", "/internal/issue-token", b"/internal/issue-token")
    assert decision.verdict is Verdict.PASSTHROUGH


def test_issue_token_is_unlisted_for_any_other_method() -> None:
    decision = classify("GET", "/internal/issue-token", b"/internal/issue-token")
    assert decision.verdict is Verdict.REFUSE
    assert decision.block_reason == ROUTE_GATE_UNLISTED


@pytest.mark.parametrize(
    "raw",
    [
        "/internal/ops",
        "POST",
        "POST /a /b",
        "POST internal/ops",
        "WEBSOCKET /v1/responses",
        "TRACE /internal/ops",
        "POST /internal/../v1/messages",
        "POST //internal/ops",
        "POST /internal/%2fops",
        "POST /internal/ops?x=1",
        "POST /internal/ops#frag",
    ],
)
def test_a_malformed_extra_raises_at_load(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_extras(raw)


SECRET = "sk-live-9f3a2c-extra-canary"


@pytest.mark.parametrize(
    "hostile",
    [
        SECRET,
        f"POST /a {SECRET}",
        f"{SECRET} /internal/ops",
        f"POST {SECRET}",
        f"POST /internal/ops?key={SECRET}",
        f"POST /internal/../{SECRET}",
    ],
)
def test_a_malformed_extra_never_echoes_its_value(hostile: str) -> None:
    # The message reaches stdout at boot; the env value may hold anything.
    with pytest.raises(ValueError) as caught:
        parse_extras(f"GET /internal/ok, {hostile}")
    message = str(caught.value)
    assert SECRET.lower() not in message.lower()
    assert "item 2" in message


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/internal/ops", "item 1: malformed (expected 'METHOD /path')"),
        ("POST /a /b", "item 1: malformed (expected 'METHOD /path')"),
        ("GET /x,,TRACE /internal/ops", "item 3: malformed (expected 'METHOD /path')"),
        ("POST internal/ops", "item 1: malformed (expected 'METHOD /path')"),
        ("POST /internal/ops?x=1", "item 1: malformed (expected 'METHOD /path')"),
    ],
)
def test_a_malformed_extra_names_its_position(raw: str, expected: str) -> None:
    with pytest.raises(ValueError, match=re.escape(expected)):
        parse_extras(raw)


def test_a_path_extra_names_the_offending_token_not_the_path() -> None:
    with pytest.raises(ValueError, match=re.escape("'?'")):
        parse_extras("POST /internal/ops?x=1")


def test_a_refused_extra_names_its_position_and_route() -> None:
    with pytest.raises(ValueError, match=re.escape("item 2 (GET /key/list)")):
        parse_extras("GET /internal/ok, get /key/list")


def test_empty_extras_are_accepted() -> None:
    assert parse_extras(None) == {}
    assert parse_extras("") == {}
    assert parse_extras(" , \n ") == {}
