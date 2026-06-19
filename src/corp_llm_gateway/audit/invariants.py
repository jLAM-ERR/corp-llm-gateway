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
    """Raise if any NEVER field key is present.

    Comparison is case-insensitive and treats `-` as `_` so HTTP-style
    header names (X-Corp-Auth, Set-Cookie) match their underscore
    counterparts in NEVER_FIELDS.

    Mirrors the Vector VRL gate (M3-3) in-process. Defense in depth:
    if the in-process logger ever regresses, Vector still drops the
    record before it lands in any sink.
    """
    for key in record:
        normalized = key.lower().replace("-", "_")
        if normalized in NEVER_FIELDS:
            raise NeverFieldPresentError(f"NEVER field {key!r} present in audit record")
