"""Scope dispatch and route classification — pure, fail-closed, dependency-free.

Imports nothing from litellm, starlette or fastapi, so it stays importable on
Python 3.14 where litellm is absent. Anything the table does not know is
refused; a caller that cannot classify must refuse too (``ROUTE_GATE_ERROR``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from corp_llm_gateway.route_gate.table import Entry, Verdict, lookup

# The audit/metric ``block_reason`` values this gate can emit.
ROUTE_GATE_UNLISTED = "route_gate_unlisted"
ROUTE_GATE_LISTED = "route_gate_listed"
ROUTE_GATE_WEBSOCKET = "route_gate_websocket"
ROUTE_GATE_MALFORMED = "route_gate_malformed"
ROUTE_GATE_UNARMED = "route_gate_unarmed"
ROUTE_GATE_ERROR = "route_gate_error"

BLOCK_REASONS: frozenset[str] = frozenset(
    {
        ROUTE_GATE_UNLISTED,
        ROUTE_GATE_LISTED,
        ROUTE_GATE_WEBSOCKET,
        ROUTE_GATE_MALFORMED,
        ROUTE_GATE_UNARMED,
        ROUTE_GATE_ERROR,
    }
)

_ENCODED_MALFORMED = (b"%2f", b"%00", b"%2e%2e")
_DECODED_MALFORMED = ("..", "//", "\x00")


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    block_reason: str | None
    why: str


_LIFESPAN = Decision(Verdict.PASSTHROUGH, None, "lifespan scope; forwarded untouched")


def classify(
    method: str,
    path: str,
    raw_path: bytes | None = None,
    *,
    scope_type: str = "http",
    upgrade_header: str | None = None,
    extras: Mapping[tuple[str, str], Entry] | None = None,
) -> Decision:
    if scope_type == "lifespan":
        return _LIFESPAN
    if scope_type == "websocket" or _wants_websocket(upgrade_header):
        return Decision(
            Verdict.REFUSE,
            ROUTE_GATE_WEBSOCKET,
            "websocket frames never reach the hook, so nothing is rewritten",
        )
    if scope_type != "http":
        return Decision(Verdict.REFUSE, ROUTE_GATE_UNLISTED, f"unknown scope type {scope_type!r}")
    if _malformed(path, raw_path):
        return Decision(
            Verdict.REFUSE,
            ROUTE_GATE_MALFORMED,
            "path is encoded or traversing; it cannot be matched against the table",
        )

    verb = method.upper()
    entry = _resolve(verb, path, extras)
    if entry is None:
        return Decision(Verdict.REFUSE, ROUTE_GATE_UNLISTED, f"no table entry for {verb} {path}")
    if entry.verdict is Verdict.REFUSE:
        return Decision(Verdict.REFUSE, ROUTE_GATE_LISTED, entry.why)
    return Decision(entry.verdict, None, entry.why)


def _wants_websocket(upgrade_header: str | None) -> bool:
    return bool(upgrade_header) and "websocket" in upgrade_header.lower()


def _malformed(path: str, raw_path: bytes | None) -> bool:
    # uvicorn percent-decodes `path`; the encoded form survives only in raw_path.
    if raw_path:
        lowered = raw_path.lower()
        if any(token in lowered for token in _ENCODED_MALFORMED):
            return True
        if any(byte > 0x7F for byte in raw_path):
            return True
    return any(token in path for token in _DECODED_MALFORMED)


def _resolve(
    method: str, path: str, extras: Mapping[tuple[str, str], Entry] | None
) -> Entry | None:
    entry = _direct(method, path, extras)
    if entry is not None or method != "HEAD":
        return entry
    # Starlette answers HEAD on every route that declares GET.
    inherited = _direct("GET", path, extras)
    if inherited is None:
        return None
    if inherited.verdict is Verdict.REWRITTEN:
        return Entry(Verdict.REFUSE, "HEAD carries no body to rewrite; the GET entry is REWRITTEN")
    return inherited


def _direct(method: str, path: str, extras: Mapping[tuple[str, str], Entry] | None) -> Entry | None:
    entry = lookup(method, path)
    if entry is not None:
        return entry
    if extras is None:
        return None
    return extras.get((method, path))
