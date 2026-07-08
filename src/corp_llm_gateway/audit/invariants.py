from collections.abc import Mapping
from typing import Any

NEVER_FIELDS: frozenset[str] = frozenset(
    {
        "mapping",
        "mapping_table",
        "pairs",
        "original_content",
        "unredacted_content",
        "pre_sanitization",
        "replace_md",
        "rule_values",
        "x_corp_auth",
        "corp_token",
        "authorization",
        "cookie",
        "set_cookie",
    }
)


class NeverFieldPresentError(Exception):
    pass


def assert_no_never_fields(record: Mapping[str, Any]) -> None:
    """Raise if any NEVER field key is present at ANY depth.

    Comparison is case-insensitive and treats `-` as `_` so HTTP-style
    header names (X-Corp-Auth, Set-Cookie) match their underscore
    counterparts in NEVER_FIELDS. The walk recurses into nested dicts and
    lists (F10) so a NEVER key nested under a benign field can't smuggle
    mapping/original/credential data past the gate.

    Mirrors the Vector VRL gate (M3-3) in-process. Defense in depth: if the
    in-process logger ever regresses, Vector still drops the record before it
    lands in any sink. NOTE: this recursive walk is the PRIMARY defense — the
    Vector VRL `!exists(.field)` gate is flat (top-level only); see the
    configmap comment and docs/security.md §6.
    """
    _walk(record)


def _walk(node: Any) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(key, str) and key.lower().replace("-", "_") in NEVER_FIELDS:
                raise NeverFieldPresentError(f"NEVER field {key!r} present in audit record")
            _walk(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _walk(item)
