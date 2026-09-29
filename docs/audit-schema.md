# Audit field schema

Source of truth for what the gateway emits to its audit pipeline.
Plan ref: M3-0. Read-with: `docs/plans/20260507-external-sanitizer-gateway-v1.md`
and `docs/security.md` (pipeline flow, Langfuse/SIEM/S3 sinks, invariants).

The gateway emits one JSON record per request (M3-1). For a request the
pre-call passed, that is the request's terminal record (below).
Vector (M3-3) parses each record and asserts the `NEVER` rules — any record
containing a `NEVER` field is dropped, and the `audit_drop` metric increments
(SIEM alert wired in M3-9).

## ALWAYS fields

These fields appear in every emitted record. Missing → record is malformed
and Vector drops it.

| Field | Type | Description |
|---|---|---|
| `timestamp` | string (RFC3339, UTC) | When the record was written |
| `request_id` | string (uuidv7) | Stable per-request id; survives streaming |
| `user_id` | string | Resolved from `X-Corp-Auth` token (M2-2) |
| `team_id` | string | Resolved from token; gates per-team rules + retention |
| `provider` | string | `anthropic` or `openai` |
| `model` | string | Resolved from upstream request body |
| `latency_ms` | int | Wall-clock milliseconds; for a terminal record, from the pre-call's start to the moment the record's outcome was written (the response's end, or the ticket's close) |
| `prompt_token_count` | int | From the provider's response `usage` (read by the ASGI desanitiser as the response passes, or added by litellm's success log while the record is open); `0` when neither reported one |
| `completion_token_count` | int | Same source as `prompt_token_count` |
| `redaction_count` | int | Number of DISTINCT secrets redacted in the request (one per distinct original — NOT an occurrence count) |
| `finding_label_counts` | object\<string, int\> | `{"EMAIL": 2, "PERSON": 1}` style; label histogram only — no text; always populated; `sum(values) == redaction_count` |
| `cache_a_hit` | bool | Whether this request hit the dedup cache |
| `gateway_version` | string | App version that handled the request |
| `status` | string | `ok` / `failed` / `degraded` / `cancelled`. `cancelled`: the request ended before its response completed — `error_code` `E_CLIENT_DISCONNECTED` (the client left) or `E_SERVER_SHUTDOWN` (the server cancelled it, e.g. at shutdown); counts only, no `placeholder_list`. How a terminal record's status is decided: below |

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
| `api_key` | Provider credential (Anthropic/OpenAI/corp-vLLM key) |
| `authorization` / any header name `*-bearer-*` | Developer's BYOK key (Anthropic/OpenAI key) |
| `cookie` / `set_cookie` | Out-of-band auth material |
| `extra_headers` | Arbitrary caller-supplied headers, which may carry credentials |

The list extends to any key whose name suggests a credential or unredacted
content. Vector's VRL transform uses an explicit allow-list (the ALWAYS
table above) — anything not on it is dropped, so the NEVER list is a
defense-in-depth tripwire, not the only defense.

## CONDITIONAL fields

Present only under the conditions noted; absent otherwise.

| Field | Condition | Description |
|---|---|---|
| `placeholder_list` | `redaction_count > 0` | Unique, sorted list of placeholder strings only (e.g. `["[EMAIL_001]", "[NAME_002]"]`) — NEVER includes the originals |
| `error_code` | `status != "ok"` | Stable error code; no exception text. Among them: the route gate's `E_ROUTE_BLOCKED`, `E_ROUTE_GATE_UNARMED`, `E_ROUTE_GATE_ERROR`; the in-flight cap's `E_CAPACITY`, `E_BODY_TIMEOUT`, `E_OVERSIZE_BLOCKED`; `E_CLIENT_DISCONNECTED` and `E_SERVER_SHUTDOWN` (with `status` `cancelled`); `E_STORE_UNAVAILABLE` (the token store could not answer); `E_PROFILE_UNAVAILABLE` (a broken profile, or a team-config store that could not answer). The issuance route writes no audit record |
| `block_reason` | a block site fired | Short reason code for the refusal. Stage 0 (content policy): `config:env`, `config:kube`, `config:nginx`, `config:ini`, `log:dump`. Stage 5 (DLP egress guard): `dlp:canary`, `dlp:secret_leak`. Content-size and request policy: `oversize:blocked`, `request:ambiguous_shape`, `provider:not_allowed`. Route gate (before litellm's router, `route_gate/classify.py`): `route_gate_listed` (the table refuses this route), `route_gate_unlisted` (no table entry), `route_gate_websocket` (a handshake on any path), `route_gate_malformed` (encoded or traversing path), `route_gate_unarmed` (the guardrail callback is not registered) and `route_gate_error` (the gate could not classify). In-flight cap (after the route gate admits a rewritten route, `route_gate/inflight.py`): `capacity` (the pod's `CORP_LLM_MAX_INFLIGHT` slots or its `CORP_LLM_MAX_DRAINING` body-read places are all taken, or the body would pass the `CORP_LLM_MAX_DRAINING_BYTES` budget; 429 `E_CAPACITY`) and `body_timeout` (the body did not arrive whole within `CORP_LLM_BODY_READ_SECONDS`; 408 `E_BODY_TIMEOUT`); a body over the 25 MiB cap there is `oversize:blocked`, a complete body with a top-level `policies` key is `route_gate_body_policies` (403 `E_ROUTE_BLOCKED`, before litellm parses it), and a body that is not `application/json` is `route_gate_body_not_json` (415 `E_ROUTE_BLOCKED`). Never contains raw payload content, a path or a header. |
| `corp_llm_latency_ms` | corp-LLM path was taken | Sub-stage latency for capacity tuning |
| `pre_pass_latency_ms` | pre-pass path was taken | Sub-stage latency |
| `audit_buffer_full` | Vector buffer at ≥50% | Operational signal |

## The terminal record

A request the pre-call passed gets exactly one record, written by
`route_gate/terminal_audit.py` from content-free facts the pre-call leaves on the
request's ticket — never by a litellm callback. A request the route gate, the
in-flight limiter or the pre-call refused keeps the record written at the
refusal instead. Security rationale: [`security.md`](security.md) §15.

| `status` | `error_code` | When |
|---|---|---|
| `ok` | — | the final body of a 2xx response went out (restored, when the request had placeholders to restore) |
| `failed` | the pre-call's code, or none | a non-2xx response, a stream that carried an error event, or litellm's app raising mid-response |
| `failed` | `E_INTERNAL` | the ASGI desanitiser could not restore the response (also `gateway_failure{component="desanitize"}`), or the request ended with no final body and nobody cancelled it |
| `cancelled` | `E_CLIENT_DISCONNECTED` | the client left before the response completed |
| `cancelled` | `E_SERVER_SHUTDOWN` | the server cancelled the request (shutdown, pod drain) |

Precedence, first match wins: a restoration failure stands whatever happens
later; an outcome published at the response's end stands against a later
cancel (the client got the response), except that one published after the client
left is `cancelled`; otherwise the ticket's close decides. A failed write on the
response path is retried once, by the close, with the same outcome; a record the
close decides (nothing was published: `cancelled`, or `failed` + `E_INTERNAL`
with no final body) has one write and no retry; a write that may have landed is
never retried; a record lost after its last attempt is logged
(`gateway_terminal_audit_lost request_id=… outcome=… error=<type>`) and counted as
`gateway_failure{component="desanitize"}`.

## litellm's `guardrail_information` (not a field of this record)

The record above is the request's terminal audit record and does not carry
`guardrail_information`. That field belongs to litellm's own
`StandardLoggingPayload`, which every `litellm.callbacks` logger (Langfuse, S3, OTEL,
…) receives. The guardrail's pre-call writes one entry there with litellm's own writer
and syncs it into litellm's logging object. The entry is allow-listed
(`audit/invariants.py`, `assert_guardrail_information_allowed`): a NEVER key or any
free text is refused before litellm sees it, and an entry litellm builds differently
is taken back out of the request metadata. That removal does not reach litellm's OTEL
guardrail span: litellm's writer has already emitted it (`emit_guardrail_span`, litellm
1.101.0 `custom_guardrail.py:1209-1217`) before the gateway checks what the writer
built, so that span can carry the keys litellm generated around our allow-listed
entry. Either way the request goes on; the gateway logs
`litellm_guardrail_information_failed request_id=… error=<type>` and counts
`gateway_failure{component="audit"}`.

| Key | Value |
|---|---|
| `guardrail_name` | `corp-llm-sanitizer` |
| `guardrail_mode` | `pre_call` |
| `guardrail_status` | derived from `block_reason`, table below |
| `start_time` / `end_time` / `duration` | the pre-call's start and end (epoch seconds) and its length in seconds |
| `guardrail_response` | `redaction_count` and `finding_label_counts` (as in this record), plus `block_reason` when one is set |
| `guardrail_provider` / `masked_entity_count` | always `null` (litellm writes both keys) |

`block_reason` → `guardrail_status`. Our reason codes are the source; litellm's status
is derived from them, never the other way round:

| `block_reason` | `guardrail_status` |
|---|---|
| none | `success` |
| `oversize:delivered` | `guardrail_flagged` |
| every Stage 0, Stage 5 and content-size / request-policy reason | `guardrail_intervened` |

The route gate and the in-flight cap refuse before litellm runs: no entry.

Where the entry shows: in the success and failure payloads of a request the pre-call
passed (status `success`, or `guardrail_flagged` for `oversize:delivered`). For a
request the pre-call refuses, litellm builds no payload; the entry then reaches only
the request litellm hands each `async_post_call_failure_hook`, and litellm's OTEL
guardrail span when OTEL is configured. It is never part of the provider-bound body.

A pre-call that fails rather than refuses — 401 `E_PROVIDER_AUTH`, 503
`E_NER_UNAVAILABLE`, `E_STORE_UNAVAILABLE` or `E_PROFILE_UNAVAILABLE`, `E_BAD_REQUEST`
— raises before the entry is written: no entry, and litellm's
`guardrail_failed_to_respond` status is never used.

## Invariants

These are tested in code:

1. **No originals (M1-14)**: across the test corpus, originals must not appear in any audit record's serialized form.
2. **No credentials (M2-7)**: the BYOK Authorization header value must not appear in any audit record.
3. **Vector drops on NEVER (M3-10)**: an injected record containing a NEVER key must not reach Langfuse, S3, or SIEM.
4. **Audit completeness (acceptance criteria)**: 100% of non-failed requests appear in S3 within 24h; measured monthly.
5. **Content-free `guardrail_information`**: litellm's payload, its failure hooks and its OTEL span carry our entry with exactly the keys above and no original, placeholder or exception text (`tests/litellm_hook/test_guardrail_information.py`, `tests/audit/test_guardrail_information_gate.py`).

## Schema versioning

Records carry an implicit version equal to `gateway_version`. Field additions
are non-breaking; field removals require a major version bump and a migration
plan with the auditors.
