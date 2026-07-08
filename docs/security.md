# Security model

How the corp-llm-gateway keeps PII inside the corp boundary, what it sanitizes,
and where to look when investigating an incident.

Read-with: [`audit-schema.md`](audit-schema.md) (field source of truth),
[`x-corp-auth.md`](x-corp-auth.md), [`conversation-id.md`](conversation-id.md),
[`ops/runbook.md`](ops/runbook.md), and the plan
[`plans/20260507-external-sanitizer-gateway-v1.md`](plans/20260507-external-sanitizer-gateway-v1.md)
(M4 fail-policy matrix is the single source of truth for failure behavior).

## 1. Overview & threat model

The gateway sits between developer Claude Code instances and the upstream LLM
APIs (`api.anthropic.com` / `api.openai.com`). On `pre_call` it routes request
content through a corp-internal sanitization LLM, replacing PII / regulated
terms with `[LABEL_NNN]` placeholders **before any bytes leave the corp
boundary**. On `post_call` it reverses the placeholders back to originals using
a per-conversation mapping that never leaves the gateway.

| Property | Posture |
|---|---|
| Success criterion | **Zero confirmed leak incidents** in the 90 days post-GA (non-negotiable) |
| Failure posture | **Fail-closed** for the sanitization path: if the corp-LLM can't run, the request is rejected (503 `E_CORP_LLM_DOWN`) rather than forwarded unsanitized (`litellm_hook.py` `pre_call`) |
| BYOK `Authorization` | The developer's `Authorization: Bearer …` (Anthropic/OpenAI key) is forwarded **untouched** to upstream and is **never logged** — it is a NEVER field in the audit gate |
| `X-Corp-Auth` | The corp token is consumed in `pre_call` (`AuthMiddleware.strip_corp_token`), stripped from forwarded headers, and **never enters the audit pipeline** — NEVER field |

Defense in depth: the no-leak guarantee is enforced at multiple independent
layers (placeholder bijection, the in-process NEVER gate, the Vector VRL gate,
and the M1-14 invariant test) so any single regression is caught downstream.

## 2. What gets sanitized (content coverage)

The content walker `sanitizer/content_blocks.py` traverses each request shape.
`pre_call` calls it for every message `content` plus the top-level Anthropic
`system` field; `collect_text` mirrors the same traversal read-only so the
pre-scan sees exactly what will be sanitized.

### Covered (sanitized on egress)

| Shape | What is sanitized |
|---|---|
| Top-level string `content` | The whole string |
| `text` block (`{"type":"text","text":…}`) | The `text` value |
| `tool_result` block | Its `content`, **recursively** (re-enters `sanitize_content`) |
| `tool_use.input` | String **leaves** of the input JSON tree, recursively; dict **keys** (tool-arg names) are preserved; non-str scalars pass through |
| `document` block | `title`, `context`; `source.data` when `source.type == "text"`; `source.content` recursively when `source.type == "content"` |
| Anthropic top-level `system` | The whole field (string or block list) |
| OpenAI multimodal content parts | `text` parts (text blocks in the list); other part types pass through |

### Not sanitized / deferred

| Shape | Why it is acceptable / status |
|---|---|
| `document` `source` with `type` `base64` / `url` | Binary or out-of-scope content; left untouched (deliberate) |
| `image` / `image_url` blocks | Binary payload or a low-risk URL; passed through |
| `thinking` / `redacted_thinking` blocks | **Intentionally** passed through unmodified — Anthropic signs thinking blocks and rejects modified ones on multi-turn replay, so they must never be rewritten; the model only ever sees placeholders anyway (no original reaches them). Correct by design, not a gap. |

Response-side de-sanitization (the reverse path) restores originals in streamed
and unary **text**, **`tool_use` input** (`input_json_delta`, JSON-escaped so the
rebuilt JSON stays valid), and OpenAI content; only `thinking` is deliberately
left untouched (per the row above).

Oversize policy (F1): when a single text leaf exceeds
`guardrail.contentSizeThresholdBytes` (default `102400`), the old code delivered
the leaf **unsanitized** — a confirmed leak. Handling is now governed by
`CORP_LLM_OVERSIZE_POLICY`:

- **`fail-closed` (default)** — refuse egress with HTTP 422 `E_OVERSIZE_BLOCKED`
  (`OversizeContentError` carries byte sizes only, never raw content). No
  original reaches the error body, logs, or upstream.
- **`chunk`** — split the leaf into overlapping sliding windows and sanitize.
  Regex/checksum detection is **linear and runs over the full text** (so an
  unbounded-pattern secret — JWT, `Bearer {20,}`, `sk-{32,}`, DB URL — cannot
  survive a chunk seam, H1); only the size-bounded NER + gazetteer + conditional
  oracle pass is chunked, with an overlap that keeps a bounded entity inside one
  window. One `RequestPlaceholderAllocator` preserves the request-wide bijection.
- **`deliver-flag`** — forward the original, but ONLY for a team listed in
  `CORP_LLM_OVERSIZE_DELIVER_TEAMS` and ONLY when a full rescan (the same
  detection the normal path runs: regex+checksum + configured detectors +
  gazetteer + rules + the conditional oracle) is clean; any finding falls back to
  fail-closed. A delivered leaf is marked `block_reason="oversize:delivered"` in
  the audit record and logged as `litellm_pre_call_system_oversize_delivered`, so
  every deliver-flag egress is auditable.

## 3. Placeholder model

The corp-LLM returns `(original, placeholder)` pairs where each placeholder is
`[LABEL_NNN]` (e.g. `[EMAIL_001]`). The pre-call path then enforces a strict
per-request **bijection** via `RequestPlaceholderAllocator`
(`sanitizer/placeholder_allocator.py`), one allocator instance per request:

- **Same original → one token.** A repeated original (even across different
  message segments and the `system` field) reuses its first placeholder, so the
  upstream model sees one consistent token for it.
- **Different originals → distinct tokens.** The corp-LLM numbers each segment's
  placeholders from `[LABEL_001]` independently, so two distinct originals can
  collide on the same token; on collision the allocator **mints a fresh label in
  the same family** (`placeholder_family`, e.g. another `EMAIL_NNN`). Without
  this, de-sanitization (keyed by placeholder) could only restore one of them.
- **Length-descending substitution (M1-9).** Both the forward (`apply_pairs`)
  and reverse (`_apply_reverse_to_response`,
  `sort_placeholders_by_descending_length`) passes sort longest-first so a short
  token can't shadow a longer one.

### Input pre-scan (forbid user-typed literals)

Before sanitizing, `pre_call` scans the input (`collect_text` →
`find_placeholder_literals`) for any `[LABEL_NNN]`-shaped substring the user
typed **literally**. Every such literal is passed to `allocator.forbid(...)` so
a real redaction can never be assigned a token a user already typed verbatim —
otherwise the user's literal would be reversed to an unrelated original on the
return pass. Today `conversation_id == request_id` so a collision stays within
one request, but this would become a cross-context leak if `conversation_id`
widened (see [`conversation-id.md`](conversation-id.md)). When any literal is
seen, `pre_call` logs a **content-free** breadcrumb:

```
litellm_pre_call_input_placeholder_literal_detected request_id=… count=N
```

which is also a sanitizer-probing signal (see §10).

### Depth guard (fail-closed)

The recursive JSON walk caps nesting at `_MAX_JSON_DEPTH = 64`. On the
**sanitize** path, exceeding it raises `ContentTooDeepError`, which `pre_call`
maps to **`400 E_BAD_REQUEST`** ("request content nesting too deep") — i.e. it
**fails closed**, never forwarding content the walker could not fully traverse.
(The reverse/desanitize walk simply stops descending past the cap and returns
the value as-is, since by then everything is already placeholders.)

## 4. Audit pipeline

Flow per request:

```
pre/post hooks build AuditEvent (audit/event.py — NEVER fields are not even
  constructible as attributes)
   ↓
AuditLogger.emit() → _serialize() → assert_no_never_fields()  [in-process gate]
   ↓
StdoutSink writes ONE JSON line to pod stdout (audit/sinks.py)
   ↓
Vector tails it  (prod: stdin source; demo: docker_logs source)
   ↓
parse JSON → NEVER-fields VRL gate (defense in depth)
   ↓
keep only AuditEvent-shaped records (have request_id AND redaction_count)
   ↓
reshape → sinks (Langfuse, S3; SIEM designed, see §6)
```

### ALWAYS fields (emitted on every record)

Exact set from `audit/logger.py::_serialize`:

`timestamp`, `request_id`, `user_id`, `team_id`, `provider`, `model`,
`latency_ms`, `prompt_token_count`, `completion_token_count`,
`redaction_count`, `finding_label_counts`, `cache_a_hit`, `gateway_version`,
`status`.

> `gateway_version` is injected by the logger (constructor arg), not carried on
> the `AuditEvent`. The standalone `LangfuseSink._event_to_record` does **not**
> set it, so a record fed directly to that sink (not via `AuditLogger`) has no
> `gateway_version`.

### CONDITIONAL fields (present only when applicable)

| Field | Present when |
|---|---|
| `placeholder_list` | `redaction_count > 0` (unique + sorted list of placeholder strings only) |
| `error_code` | `status != "ok"` |
| `corp_llm_latency_ms` | corp-LLM path was taken |
| `pre_pass_latency_ms` | pre-pass path was taken |
| `audit_buffer_full` | Vector buffer signal present |

See [`audit-schema.md`](audit-schema.md) for the full schema and types (it is
the field source of truth).

### Count semantics

- `redaction_count` = number of **DISTINCT** secrets (one per distinct
  original, counted as distinct canonical placeholders in
  `_merge_into_state`) — **not** an occurrence count.
- `finding_label_counts` = per-family histogram (`{"EMAIL": 2, "PERSON": 1}`),
  built by `_label_counts` over the distinct placeholders, so
  `sum(values) == redaction_count`.
- `placeholder_list` = the distinct placeholder tokens, `sorted(...)` — token
  strings only, **never** the originals.

## 5. Langfuse integration

Two code paths produce the **same** Langfuse shape:

- **Vector (prod default)** — `helm/.../templates/configmap.yaml` `to_langfuse`
  transform, plus the demo pipeline `docker/demo-vector/vector.yaml`.
- **In-process `LangfuseSink`** — `audit/langfuse_sink.py`, for tests,
  low-volume, or debug pods that opt out of Vector.

Each audit record maps to **one `trace-create` + one `generation-create`**
event POSTed to `{base}/api/public/ingestion` (verified against
`langfuse_sink.py` `_records_to_batch` and the configmap transform):

**`trace-create` body**

| Field | Value |
|---|---|
| `id` | `request_id` |
| `name` | `corp-llm-gateway` (in-process sink) / `gateway-request` (demo Vector) |
| `userId` | `user_id` |
| `metadata` | `team_id`, `redaction_count`, `cache_a_hit`, `finding_label_counts`, `gateway_version`, `status`, `error_code` |
| `tags` | `["team:<team_id>", "provider:<provider>"]` |

**`generation-create` body**

| Field | Value |
|---|---|
| `model` | `model` |
| `usage` | `{input: prompt_token_count, output: completion_token_count, total: input+output, unit: "TOKENS"}` |
| `metadata` | `latency_ms`, `corp_llm_latency_ms`, `pre_pass_latency_ms` |

**Auth & transport**

- HTTP **Basic** auth: `LANGFUSE_PUBLIC_KEY` : `LANGFUSE_SECRET_KEY`
  (`base64(public:secret)` in the Python sink; Vector `auth.strategy: basic`
  with the same env vars).
- `POST {base}/api/public/ingestion`, `Content-Type: application/json`.
- Buffer (prod Vector langfuse sink): **disk, 1 GiB** (`max_size:
  1073741824`).

**CRITICAL design point — metadata only, no content.** A Langfuse trace stores
**metadata only**; no prompt or response text is sent. The
`trace-create`/`generation-create` bodies carry token counts, latencies,
redaction stats, and placeholder labels — never message text. Consequently a
trace's **Input / Output panes are intentionally EMPTY**. There are no
originals in the audit store.

> Implementation note: the demo Vector pipeline sets the trace `metadata` to the
> whole audit record (`metadata: audit`), which still excludes originals because
> the audit record itself never contains them (NEVER gate). The in-process sink
> and prod configmap use the curated metadata subset above.

**Reading traces for security.** Filter by tag `team:<id>` / `provider:<p>`;
inspect `redaction_count` + `finding_label_counts` + `placeholder_list`.
Remember a **probe request legitimately shows `redaction_count = 0`** — absence
of redactions is not absence of activity.

## 6. SIEM integration

The NEVER-fields gate exists in **two** places — the in-process
`assert_no_never_fields` (`audit/invariants.py`) and the Vector VRL filter (prod
`enforce_audit_schema`; demo `never_fields_gate`) — **defense in depth**. A
record containing any NEVER key is **dropped**; per plan M3-3 this increments an
`audit_drop` metric that **should raise a SIEM alert** (M3-9).

What SIEM should monitor (per plan M3-9):

| Signal | Meaning |
|---|---|
| `audit_drop > 0` | A NEVER field reached the audit pipeline — a leak/regression attempt; investigate immediately |
| Fail-closed `503`s | `E_CORP_LLM_DOWN`, `E_NER_UNAVAILABLE`, `E_REDIS_DOWN`, S3/Vector-buffer fail-closed, etc. (availability + possible attack) |
| `litellm_pre_call_input_placeholder_literal_detected` | A user typed `[LABEL_NNN]` literals — possible sanitizer probing |
| Redaction anomalies | Redaction spike (3σ), bypass denials, auth-failure bursts |

**NEVER_FIELDS** (exact, from `audit/invariants.py`; comparison is
case-insensitive and treats `-` as `_`, so `X-Corp-Auth` / `Set-Cookie` match):

```
mapping, mapping_table, pairs, original_content, unredacted_content,
pre_sanitization, replace_md, rule_values, x_corp_auth, corp_token,
authorization, cookie, set_cookie
```

**Current status (2026-07-01).** The Vector SIEM sink **and** the
`AuditVectorDropHigh` / `LeakAttemptDetected` alerts are now **wired** (CP-3:
`helm/.../templates/configmap.yaml` siem sink + `templates/siem-alerts.yaml`,
routed through the same NEVER-fields VRL gate). The **only** remaining item is
confirming the real SIEM endpoint (open question #3) — the configured endpoint
is still a placeholder. See `docs/requirements-compliance.md` (R15) for the
current status.

## 7. S3 durable audit store

Prod Helm `s3` sink (`templates/configmap.yaml`, fed from the post-gate
`enforce_audit_schema` output):

| Setting | Value |
|---|---|
| Type | `aws_s3` |
| Bucket | `values.audit.sinks.s3.bucket` → `corp-audit` |
| Key prefix | `{{ team_id }}/dt=%Y-%m-%d/` (per-team, date-partitioned) |
| Compression | `gzip` |
| Encoding | `json` |
| Buffer | disk, **5 GiB** (`max_size: 5368709120`) |

S3 is the **durable** sink and is **fail-closed** (§8). Per-team retention is
generated by `audit/retention.py` (`lifecycle_configuration`): one S3 lifecycle
rule per team scoped to the `<team_id>/` prefix, transitioning to GLACIER after
`retention_hot_days` and expiring after `+ retention_cold_years * 365` days.

## 8. Fail-policy matrix

From `helm/.../values.yaml` `failPolicy` (the plan's **M4 matrix is the source
of truth** — do not add ad-hoc fail-open paths):

| Component | Behavior |
|---|---|
| `corpLlmDown` | **fail-closed** (503 `E_CORP_LLM_DOWN`) |
| `prePassDown` | **continue** (corp-LLM only; metric increments) |
| `nerUnavailable` (when `CORP_LLM_REQUIRE_NER`) | **fail-closed** (503 `E_NER_UNAVAILABLE`) — a configured NER model is absent (F2); `/healthz/ready` also probes NER-loaded so the pod leaves the LB. Knob **off** → dev / Python-3.14 graceful path (no NER, no 503) |
| `redisClusterDown` | **fail-closed** (503) |
| `postgresDown` | **fail-closed** (503) |
| `vectorBufferFull` | **fail-closed** (503) by default; team may opt `audit_buffer_full=continue` |
| `s3SinkDown` | **fail-closed** (503) — S3 is the durable sink |

See plan §M4 for the full matrix (Redis transient retry, Cache A/C miss
fall-through, single-audit-sink-down) and per-team override columns.

## 9. Invariants — never weaken these

| ID | Invariant | Enforced by |
|---|---|---|
| M1-14 | **No originals leak** across six surfaces: (i) logger emissions, (ii) error bodies, (iii) exception traces, (iv) metric labels, (v) forwarded headers, (vi) pod stdout | `tests/invariants/test_no_originals_leak.py` |
| M2-7 | **No BYOK credential in audit**: the `Authorization` value never appears in any audit surface | same test corpus + NEVER gate |
| M3-10 | **Vector drops NEVER**: an injected NEVER-key record reaches no sink | integration assertion |
| M1-9 | **Length-descending substitution** (forward + reverse) | `placeholder.py`, `litellm_hook.py` |
| — | **Per-request placeholder bijection** (same original → one token; distinct originals → distinct tokens) | `placeholder_allocator.py` |
| — | **Depth-guard fail-closed** (`_MAX_JSON_DEPTH=64` → `400 E_BAD_REQUEST` on sanitize) | `content_blocks.py`, `litellm_hook.py` |
| — | **NEVER gate, in-process + Vector** (defense in depth) | `audit/invariants.py` + Vector VRL |

## 10. Forensic breadcrumbs (incident investigation)

Where to look first, and what each breadcrumb can and cannot tell you:

| Breadcrumb | Tells you | Never contains |
|---|---|---|
| `finding_label_counts` | What KINDS of secret were redacted (family histogram) | Any text |
| `placeholder_list` | WHICH tokens were issued (`[EMAIL_001]`, …) | Originals |
| `redaction_count` | How many DISTINCT secrets | — |
| `litellm_pre_call_input_placeholder_literal_detected` (log) | A user typed `[LABEL_NNN]` literals — possible probing; **content-free** (count only) | The literal text |
| Pre/post lifecycle logs (`litellm_pre_call_*`, `litellm_post_call_*`, `litellm_audit_emitted`) | Per-request flow, byte sizes, redaction totals, latencies | Content bodies |

In-code deferred-gap markers (search these to confirm a behavior is a known gap
rather than a regression): `SECURITY` comments in
`sanitizer/content_blocks.py` (tool_use streaming desanitization,
thinking/redacted_thinking, document binary/url sources) and the
`project_tool_use_input_unsanitized` memory note.

**During an incident:** start in the S3 durable store (per-team,
date-partitioned) and Langfuse (filter by `team:`/`provider:` tag). Confirm
`audit_drop` is zero — a non-zero value means a NEVER field reached the pipeline
and is the first thing to chase. None of these surfaces can contain an original
by construction; if one appears to, that is an M1-14 regression.

## 11. Known gaps / follow-ups

| # | Gap | Severity |
|---|---|---|
| (a) | ~~Prod Helm `templates/configmap.yaml` had a DUPLICATE `transforms:` key that dropped `parse` + `enforce_audit_schema`.~~ **FIXED** — single `transforms:` block now (`parse` → `enforce_audit_schema` → `audit_only` → `to_langfuse`); `parse` is non-strict (tolerates plain-text uvicorn lines), an `audit_only` filter keeps non-audit events out of both sinks, and the Vector-side NEVER gate now mirrors the full in-process list (13 keys + `-`/`_` case variants). `langfuse` ← `to_langfuse`, `s3` ← `audit_only`. | **Resolved** |
| (b) | **SIEM sink enabled in values but not defined in the configmap.** `audit.sinks.siem.enabled: true` has no corresponding `sinks.siem` in the Vector configmap; `audit_drop` alerting (M3-9) also pending. | **Medium** — SIEM monitoring (incl. leak-attempt alerts) not yet active |
| (c) | ✅ **FIXED** — streamed `tool_use` `input_json_delta` is now desanitized (JSON-escaped) in `sanitizer/streaming.py`, so the developer's tool receives real values, not `[LABEL_NNN]` tokens. | **Resolved** |
| (d) | ✅ **By design (not a gap)** — `thinking` / `redacted_thinking` are passed through UNMODIFIED: Anthropic signs thinking blocks and rejects modified ones on multi-turn replay, and the model only ever sees placeholders (no original reaches them). | **Resolved (by design)** |

**(a) and (c) are fixed; (d) is correct by design.** The only remaining open item
is **(b)** — wiring the SIEM sink (gated on the SIEM target). See
[`remaining-steps.md`](remaining-steps.md).
