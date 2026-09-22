"""Default-deny route gate: classify every request before litellm's router."""

from typing import TYPE_CHECKING

from corp_llm_gateway.route_gate.classify import (
    BLOCK_REASONS,
    ROUTE_GATE_ERROR,
    ROUTE_GATE_LISTED,
    ROUTE_GATE_MALFORMED,
    ROUTE_GATE_UNARMED,
    ROUTE_GATE_UNLISTED,
    ROUTE_GATE_WEBSOCKET,
    Decision,
    classify,
)
from corp_llm_gateway.route_gate.table import (
    GATEWAY_ROUTE_TABLE,
    HTTP_METHODS,
    LITELLM_REGEX_TABLE,
    LITELLM_ROUTE_TABLE,
    Entry,
    Verdict,
    lookup,
    parse_extras,
)

if TYPE_CHECKING:
    from corp_llm_gateway.route_gate.middleware import RouteGateMiddleware

# `middleware` pulls in audit + metrics; `config check` and settings.validate()
# only need the table, so the import happens on first attribute access.
_LAZY = {"COMPONENT", "RouteGateMiddleware"}


def __getattr__(name: str) -> object:
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from corp_llm_gateway.route_gate import middleware

    return getattr(middleware, name)


__all__ = [
    "BLOCK_REASONS",
    "COMPONENT",
    "GATEWAY_ROUTE_TABLE",
    "HTTP_METHODS",
    "LITELLM_REGEX_TABLE",
    "LITELLM_ROUTE_TABLE",
    "ROUTE_GATE_ERROR",
    "ROUTE_GATE_LISTED",
    "ROUTE_GATE_MALFORMED",
    "ROUTE_GATE_UNARMED",
    "ROUTE_GATE_UNLISTED",
    "ROUTE_GATE_WEBSOCKET",
    "Decision",
    "Entry",
    "RouteGateMiddleware",
    "Verdict",
    "classify",
    "lookup",
    "parse_extras",
]
