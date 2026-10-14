"""The route table's verdicts on the litellm spellings that matter most: the
rewritten set, the OpenAI spellings a raw passthrough also matches, websockets
and the token counters."""

from __future__ import annotations

import re
from collections.abc import Iterator
from importlib.util import find_spec

import pytest

from corp_llm_gateway.route_gate import (
    LITELLM_REGEX_TABLE,
    LITELLM_ROUTE_TABLE,
    ROUTE_GATE_WEBSOCKET,
    Verdict,
    classify,
    lookup,
)

pytestmark = pytest.mark.skipif(
    find_spec("litellm") is None, reason="litellm is not installed in this interpreter"
)


def _table_rows() -> Iterator[tuple[str, str, object]]:
    for (method, path), entry in LITELLM_ROUTE_TABLE.items():
        yield method, path, entry
    for row in LITELLM_REGEX_TABLE:
        yield row.method, row.template, row.entry


def _rewritten_rows() -> list[tuple[str, str]]:
    return sorted(
        (method, path)
        for method, path, entry in _table_rows()
        if entry.verdict is Verdict.REWRITTEN  # type: ignore[attr-defined]
    )


def test_the_rewritten_set_is_the_eight_generation_spellings() -> None:
    # Three more spellings reach the hook (`/openai/v1/responses`,
    # `/openai/v1/responses/compact`, `/openai/deployments/{model:path}/chat/completions`)
    # and are REFUSE anyway: litellm's raw `/openai/{endpoint:path}` passthrough
    # also matches them (next test).
    assert _rewritten_rows() == [
        ("POST", "/chat/completions"),
        ("POST", "/engines/{model:path}/chat/completions"),
        ("POST", "/responses"),
        ("POST", "/responses/compact"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/messages"),
        ("POST", "/v1/responses"),
        ("POST", "/v1/responses/compact"),
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/openai/v1/responses",
        "/openai/v1/responses/compact",
        "/openai/deployments/{model:path}/chat/completions",
    ],
)
def test_the_openai_spellings_are_refused_because_a_raw_passthrough_matches_them(
    path: str,
) -> None:
    # These three DO reach the hook; the only thing keeping litellm's raw
    # `/openai/{endpoint:path}` passthrough off them is that its router is
    # included later. They are REFUSE for that reason.
    sample = re.sub(r"\{[^{}]+\}", "sample", path)
    assert lookup("POST", sample).verdict is Verdict.REFUSE  # type: ignore[union-attr]


# The websocket paths litellm 1.101.0 registers.
WEBSOCKET_PATHS = (
    "/openai/v1/realtime",
    "/openai/{endpoint:path}",
    "/openai_passthrough/{endpoint:path}",
    "/realtime",
    "/responses",
    "/v1/realtime",
    "/v1/responses",
    "/vertex_ai/live",
)


def test_no_websocket_route_is_listed() -> None:
    # A websocket path may also exist as an HTTP route (POST /v1/responses does);
    # what matters is that a websocket scope is refused before any lookup.
    for path in WEBSOCKET_PATHS:
        decision = classify("GET", path, path.encode(), scope_type="websocket")
        assert decision.verdict is Verdict.REFUSE
        assert decision.block_reason == ROUTE_GATE_WEBSOCKET


@pytest.mark.parametrize(
    "path",
    [
        "/v1/messages/count_tokens",
        "/v1/responses/input_tokens",
        "/openai/v1/responses/input_tokens",
    ],
)
def test_the_token_counters_still_bypass_the_hook(path: str) -> None:
    assert lookup("POST", path).verdict is Verdict.REFUSE  # type: ignore[union-attr]
