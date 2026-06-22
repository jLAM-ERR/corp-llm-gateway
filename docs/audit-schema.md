# Audit field schema

Source of truth for what the gateway emits to its audit pipeline.
Plan ref: M3-0. Read-with: `docs/plans/20260507-external-sanitizer-gateway-v1.md`.

The custom logger (M3-1) emits one JSON record per gateway request.
Vector (M3-3) parses each record and asserts the `NEVER` rules — any record
containing a `NEVER` field is dropped, and the `audit_drop` metric increments
(SIEM alert wired in M3-9).

## ALWAYS fields

These fields appear in every emitted record. Missing → record is malformed
and Vector drops it.

| Field | Type | Description |
|---|---|---|
| `timestamp` | string (RFC3339, UTC) | When the gateway received the request |
| `request_id` | string (uuidv7) | Stable per-request id; survives streaming |
| `user_id` | string | Resolved from `X-Corp-Auth` token (M2-2) |
| `team_id` | string | Resolved from token; gates per-team rules + retention |
| `provider` | string | `anthropic` or `openai` |
| `model` | string | Resolved from upstream request body |
| `latency_ms` | int | Wall-clock from receive to last byte upstream |
| `prompt_token_count` | int | From upstream response |
| `completion_token_count` | int | From upstream response |
| `redaction_count` | int | Number of DISTINCT secrets redacted in the request (one per distinct original — NOT an occurrence count) |
| `finding_label_counts` | object\<string, int\> | `{"EMAIL": 2, "PERSON": 1}` style; label histogram only — no text; always populated; `sum(values) == redaction_count` |
| `cache_a_hit` | bool | Whether this request hit the dedup cache |
| `gateway_version` | string | App version that handled the request |
| `status` | string | `ok` / `failed` / `degraded` |

## NEVER fields

These keys MUST NEVER appear in any emitted record. They are checked
structurally by Vector VRL (M3-3); presence is treated as a regression bug
and the record is dropped.

| Forbidden key | Why |
|---|---|
| `mapping` / `mapping_table` / `pairs` | Reveals original ↔ placeholder pairs |
| `original_content` / `unredacted_content` / `pre_sanitization` | Pre-sanitization payload |
| `replace_md` / `rule_values` | Per-team rule values may contain regulated terms |
| `x_corp_auth` / `corp_token` / any case variation | Gateway auth credential |
| `authorization` / any header name `*-bearer-*` | Developer's BYOK key (Anthropic/OpenAI key) |
| `cookie` / `set_cookie` | Out-of-band auth material |

The list extends to any key whose name suggests a credential or unredacted
content. Vector's VRL transform uses an explicit allow-list (the ALWAYS
table above) — anything not on it is dropped, so the NEVER list is a
defense-in-depth tripwire, not the only defense.

## CONDITIONAL fields

Present only under the conditions noted; absent otherwise.

| Field | Condition | Description |
|---|---|---|
| `placeholder_list` | `redaction_count > 0` | Unique, sorted list of placeholder strings only (e.g. `["[EMAIL_001]", "[NAME_002]"]`) — NEVER includes the originals |
| `error_code` | `status != "ok"` | Stable error code; no exception text |
| `corp_llm_latency_ms` | corp-LLM path was taken | Sub-stage latency for capacity tuning |
| `pre_pass_latency_ms` | pre-pass path was taken | Sub-stage latency |
| `audit_buffer_full` | Vector buffer at ≥50% | Operational signal |

## Invariants

These are tested in code:

1. **No originals (M1-14)**: across the test corpus, originals must not appear in any audit record's serialized form.
2. **No credentials (M2-7)**: the BYOK Authorization header value must not appear in any audit record.
3. **Vector drops on NEVER (M3-10)**: an injected record containing a NEVER key must not reach Langfuse, S3, or SIEM.
4. **Audit completeness (acceptance criteria)**: 100% of non-failed requests appear in S3 within 24h; measured monthly.

## Schema versioning

Records carry an implicit version equal to `gateway_version`. Field additions
are non-breaking; field removals require a major version bump and a migration
plan with the auditors.
