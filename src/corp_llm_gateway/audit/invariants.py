import re
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


# Our entry in litellm's `standard_logging_guardrail_information`: exactly the keys
# litellm's writer builds, and in its free-form `guardrail_response` only our facts —
# `block_reason` only when set (litellm drops a None there, as the audit record omits
# it). Every value is a label, a count or a time, never text (docs/audit-schema.md).
GUARDRAIL_INFORMATION_KEYS: frozenset[str] = frozenset(
    {
        "guardrail_name",
        "guardrail_provider",
        "guardrail_mode",
        "guardrail_response",
        "guardrail_status",
        "start_time",
        "end_time",
        "duration",
        "masked_entity_count",
    }
)
GUARDRAIL_RESPONSE_KEYS: frozenset[str] = frozenset(
    {"block_reason", "redaction_count", "finding_label_counts"}
)
_GUARDRAIL_RESPONSE_REQUIRED: frozenset[str] = GUARDRAIL_RESPONSE_KEYS - {"block_reason"}
# litellm's `GuardrailStatus` literal (types/utils.py), restated: this module imports nothing.
GUARDRAIL_STATUSES: frozenset[str] = frozenset(
    {
        "success",
        "guardrail_flagged",
        "guardrail_intervened",
        "guardrail_failed_to_respond",
        "not_run",
    }
)
# Where a record carries such entries; `assert_no_never_fields` checks them against the list.
GUARDRAIL_INFORMATION_FIELDS: frozenset[str] = frozenset(
    {"guardrail_information", "standard_logging_guardrail_information"}
)
_GUARDRAIL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_GUARDRAIL_MODE = re.compile(r"[a-z][a-z_]{0,31}")
_BLOCK_REASON = re.compile(r"(?=.{1,64}$)[a-z][a-z0-9_]*(?::[a-z][a-z0-9_]*)?")
_FINDING_LABEL = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class NeverFieldPresentError(Exception):
    pass


class GuardrailInformationRejectedError(NeverFieldPresentError):
    """A `guardrail_information` entry outside the allow-list. The message names the
    rule broken, never a value or an unknown key: either can be the content refused."""


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
            if normalized in GUARDRAIL_INFORMATION_FIELDS:
                assert_guardrail_information_allowed(value)
                continue
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


def assert_guardrail_information_allowed(value: Any) -> None:
    """Raise unless *value* is None (litellm's "no guardrail ran"), one allow-listed
    entry or a list of them.

    The NEVER walk runs first (a NEVER key raises `NeverFieldPresentError` naming it);
    then every key must be on the list and every value a label of its pattern, a
    non-negative count, a time or None. Free text of any kind is refused.
    """
    if value is None:
        return
    entries = value if isinstance(value, list) else [value]
    for entry in entries:
        if not isinstance(entry, Mapping):
            _reject("entry is not a mapping")
        _walk(entry)
        if set(entry) != GUARDRAIL_INFORMATION_KEYS:
            _reject("entry keys differ from the allow-list")
        if not _matches(_GUARDRAIL_NAME, entry["guardrail_name"]):
            _reject("guardrail_name is not a name")
        mode = entry["guardrail_mode"]
        if not _matches(_GUARDRAIL_MODE, getattr(mode, "value", mode)):
            _reject("guardrail_mode is not a mode")
        if entry["guardrail_status"] not in GUARDRAIL_STATUSES:
            _reject("guardrail_status is not a litellm status")
        if entry["guardrail_provider"] is not None or entry["masked_entity_count"] is not None:
            _reject("guardrail_provider and masked_entity_count must be None")
        for key in ("start_time", "end_time", "duration"):
            if entry[key] is not None and not _is_number(entry[key]):
                _reject("a timing field is not a number")
        _check_response(entry["guardrail_response"])


def _check_response(response: Any) -> None:
    if not isinstance(response, Mapping) or not (
        _GUARDRAIL_RESPONSE_REQUIRED <= set(response) <= GUARDRAIL_RESPONSE_KEYS
    ):
        _reject("guardrail_response keys differ from the allow-list")
    if "block_reason" in response and not _matches(_BLOCK_REASON, response["block_reason"]):
        _reject("block_reason is not a reason code")
    if not _is_count(response["redaction_count"]):
        _reject("redaction_count is not a count")
    labels = response["finding_label_counts"]
    if not isinstance(labels, Mapping):
        _reject("finding_label_counts is not a map")
    for label, count in labels.items():
        if not _matches(_FINDING_LABEL, label) or not _is_count(count):
            _reject("finding_label_counts holds something other than label counts")


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _reject(rule: str) -> None:
    raise GuardrailInformationRejectedError(f"guardrail_information refused: {rule}")
