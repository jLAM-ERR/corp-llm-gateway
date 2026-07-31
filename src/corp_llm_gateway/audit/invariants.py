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
        "api_key",
        "authorization",
        "cookie",
        "set_cookie",
        "extra_headers",
    }
)

# Fields whose dict KEYS are data, not schema field names. `finding_label_counts`
# maps a detector label (API_KEY, JWT, PASSWORD, …) to a count, so its keys must
# not be matched against NEVER_FIELDS — only its values are walked.
DATA_KEYED_FIELDS: frozenset[str] = frozenset({"finding_label_counts"})


class NeverFieldPresentError(Exception):
    pass


def assert_no_never_fields(record: Mapping[str, Any]) -> None:
    """Raise if any NEVER field key is present at ANY depth.

    Comparison is case-insensitive and treats `-` as `_` so HTTP-style
    header names (X-Corp-Auth, Set-Cookie) match their underscore
    counterparts in NEVER_FIELDS. The walk recurses into nested dicts and
    lists (F10) so a NEVER key nested under a benign field can't smuggle
    mapping/original/credential data past the gate.

    A `DATA_KEYED_FIELDS` entry (currently `finding_label_counts`) is a map
    whose own keys are detector-label DATA (e.g. "API_KEY", "JWT"), not
    schema field names, so those keys are skipped — but every value nested
    inside is still walked normally, so a NEVER key smuggled as a VALUE
    there is still caught.

    Mirrors the Vector VRL gate (M3-3) in-process. Defense in depth: if the
    in-process logger ever regresses, Vector still drops the record before it
    lands in any sink. NOTE: this recursive walk is the PRIMARY defense — the
    Vector VRL `!exists(.field)` gate is flat (top-level only); see the
    configmap comment and docs/security.md §6.
    """
    _walk(record)


def _normalize_key(key: str) -> str:
    return key.lower().replace("-", "_")


def _walk(node: Any) -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if not isinstance(key, str):
                _walk(value)
                continue
            normalized = _normalize_key(key)
            if normalized in DATA_KEYED_FIELDS:
                _walk_values_only(value)
                continue
            if normalized in NEVER_FIELDS:
                raise NeverFieldPresentError(f"NEVER field {key!r} present in audit record")
            _walk(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _walk(item)


def _walk_values_only(node: Any) -> None:
    """Walk a DATA_KEYED_FIELDS value: its own keys are detector-label data, so
    skip matching them against NEVER_FIELDS — but still walk every value with
    the normal `_walk`, so a NEVER key nested one level deeper is still caught.

    Only THIS immediate key layer is exempt. A list found directly as the
    data-keyed field's value hands its items to the normal `_walk` (not back
    to `_walk_values_only`) — otherwise a NEVER key nested under that list
    would be exempted from key matching at every depth below it, not just
    this one level.
    """
    if isinstance(node, Mapping):
        for value in node.values():
            _walk(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _walk(item)
