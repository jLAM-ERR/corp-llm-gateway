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
| BYOK `Authorization` | The developer's `Authorization: Bearer …` (Anthropic/OpenAI key) is forwarded **untouched** to upstream and is **never logged** — it is a NEVER field in the audit gate. The opt-in subscription-auth bridges also read that bearer without rewriting it; see §13 |
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
| OpenAI multimodal content parts | `text`/`input_text`/`output_text`/`reasoning_text`/`summary_text` (the `text` value) and `refusal` blocks (the `refusal` value) |
| `server_tool_use` / `mcp_tool_use` blocks | Their `input`, recursively (same JSON-tree scan as `tool_use.input`) |
| `web_search_tool_result` / `code_execution_tool_result` / `mcp_tool_result` blocks | Their `content`, recursively as an arbitrary JSON tree (string leaves regardless of key name) — `encrypted_content` keys are protected (see below) |
| `search_result` block | `title` and `content` (recursively re-enters `sanitize_content`, like `tool_result`) |
| A Responses `input` item (`custom_tool_call`, `reasoning`, `function_call_output`, …) | Every field EXCEPT the structural identifiers (`_RESPONSES_STRUCTURAL_FIELDS`: `type`/`id`/`call_id`/`status`/`role`/`container_id`/`name`/`server_label`) and the opaque signed fields (`encrypted_content`, `computer_call_output.output`, `image_generation_call.result`) — known fields get specialized handling (`arguments` JSON-parsed, block-list fields recursed via `sanitize_content`, `output` scanned as a JSON tree), everything else — including a field the registry has never named — gets the same generic string-leaf scan `_sanitize_json` applies to an arbitrary JSON blob, via `sanitize_responses_item`/`collect_responses_item_text`, applied per item |
| Chat-Completions-shaped `tool_calls`/`function_call` on a Responses item | `function.arguments` / `arguments`, same as the `messages` shape |
| A bare string element of `data["input"]` (not a dict) | The whole string, same as top-level string content |

### Not sanitized / deferred

| Shape | Why it is acceptable / status |
|---|---|
| `document` `source` with `type` `base64` / `url` | Binary or out-of-scope content; left untouched (deliberate) |
| `image` / `image_url` / `input_image` blocks | Binary payload or a low-risk URL; passed through |
| `input_file` / `file` blocks | Binary attachment reference (`file_data`/`file_id`/`filename`); passed through — filenames are not currently scanned |
| `input_audio` / `output_audio` blocks | Base64 audio; passed through |
| `container_upload` block | A file-id reference only, no text; passed through |
| `thinking` / `redacted_thinking` blocks | **Intentionally** passed through unmodified — Anthropic signs thinking blocks and rejects modified ones on multi-turn replay, so they must never be rewritten; the model only ever sees placeholders anyway (no original reaches them). Correct by design, not a gap. |
| `encrypted_content` (any key inside a JSON tree we scan, e.g. `web_search_result.encrypted_content`) | Same signed-content reasoning as `thinking`; excluded from `_sanitize_json`/`_desanitize_json`/`_collect_json_text` by key name |
| `computer_call_output.output` | A base64 screenshot; scanning it would trip the oversize policy on ordinary screenshots, so `output` is only scanned for `function_call_output`/`custom_tool_call_output` items |
| A genuinely unrecognized block `type` | **Fails safe**: `_sanitize_block` scans it as a generic JSON value tree (the same recursion `_sanitize_json` applies to an arbitrary blob) instead of hard-rejecting the request. `"type"` and the opaque-key set (`_BLOCK_FALLBACK_OPAQUE_KEYS`, e.g. `signature`) are preserved verbatim; every other key's string leaves are sanitized, so nothing egresses unscanned — a new provider block shape (`web_fetch_tool_result`, `bash_code_execution_tool_result`, …) no longer 400s real traffic or poisons a multi-turn conversation that replays it. `UnsanitizableContentBlockError` still exists, but now only for a genuinely unscannable *value* under a *known* text field name (a bare scalar where str/dict/list was expected), not for an unrecognized block type. |
| Responses `input` item structural fields (`_RESPONSES_STRUCTURAL_FIELDS`: `type`, `id`, `call_id`, `status`, `role`, `container_id`, `name`, `server_label`) and `data["tools"]` | **Deliberately excluded from both rewriting and the Stage-0/Stage-5 scan.** `name`/`server_label` on an `input` item (e.g. `function_call`, `mcp_call`) must correlate by exact value to its own declaration in the untouched `data["tools"]` array; rewriting only the `input`-side occurrence desyncs the two and the provider rejects the request. The remaining fields in the set are routing/status enums, never free text. This is the one real, currently-undocumented-elsewhere blind spot in Responses coverage. |

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

**Recursion asymmetry (F10).** The in-process gate is the **primary** defense and
is **recursive** — it walks nested dicts and lists, so a NEVER key smuggled under
a benign field (e.g. `{"debug": {"mapping": …}}`) is caught before any record is
written to stdout (and therefore before Vector ever sees it). The Vector VRL gate
is **flat** — `!exists(.field)` inspects **top-level** keys only. A fully
recursive VRL walk is not cleanly expressible (closures can't accumulate across
`for_each`, and a `flatten()`+`filter()` rewrite is unverifiable without a Vector
runtime in CI), so the Vector filter stays a top-level backstop while the
recursive in-proc walk carries the nested case. See the configmap comment above
`enforce_audit_schema`.

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
api_key, authorization, cookie, set_cookie, extra_headers
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
| `oracleDisabled` (`CORP_LLM_ORACLE_ENABLED=0`) | **continue by config** — local-first cascade only, no oracle call ever attempted; boot **refused** (`ConfigError`) if `CORP_LLM_LOCAL_FIRST` is also off (no-op-sanitizer guard) |
| `prePassDown` | **continue** (corp-LLM only; metric increments) |
| `nerUnavailable` (when `CORP_LLM_REQUIRE_NER`) | **fail-closed** (503 `E_NER_UNAVAILABLE`) — a configured NER model is absent (F2); `/healthz/ready` also probes NER-loaded so the pod leaves the LB. Knob **off** → dev / Python-3.14 graceful path (no NER, no 503) |
| `redisClusterDown` | **fail-closed** (503) |
| `postgresDown` | **fail-closed** (503) |
| `vectorBufferFull` | **fail-closed** (503) by default; team may opt `audit_buffer_full=continue` |
| `s3SinkDown` | **fail-closed** (503) — S3 is the durable sink |
| `profileUnavailable` (D4, when `profile_ids` set) | **fail-closed** (503 `E_PROFILE_UNAVAILABLE`) — a team's resolved profile bundle is missing or malformed; never fall through to un-profiled egress (invariant 6). Empty `profile_ids` → passthrough (no profile resolution, no 503) |
| `providerBlocked` (D4) | **block** (403 `E_PROVIDER_BLOCKED`) — the merged `allowed_providers` policy rejects the upstream target; a clean policy denial before any content processing, no raw body |
| `spanApplyFailed` | **fail-closed** (500 `E_SPAN_INVALID`) — `apply_spans` rejects a pre-selected replacement span that no longer matches the segment text (e.g. a stale Cache-A/allocator remap); `StaleSpanError` (`sanitizer/placeholder.py`) is mapped to an audit record + `gateway_failure{component="sanitize"}` rather than escaping as a generic, undocumented 500 |
| `internalError` (F8) | **fail-closed** (500 `E_INTERNAL`) — an unexpected exception in `pre_call`/`post_call_unary`/`post_call_stream` (a DB error, a bug, an audit-sink outage) is never echoed to the client, the log, or the audit record: only the opaque error_code + `gateway_failure{component="internal"}`. The safety net that maps this is guarded against re-entrancy — a failure while recording the failure itself (e.g. the audit sink is also down) is caught and logged rather than replacing the client-visible 500. A request that already recorded a component-specific failure (e.g. `dlp`) is not double-counted as `internal` too |

See plan §M4 for the full matrix (Redis transient retry, Cache A/C miss
fall-through, single-audit-sink-down) and per-team override columns.

`oracleDisabled` is a deliberate operator choice (solo/local-mode gateways —
see `examples/compose/`), not a failure: with the oracle off, the
deterministic local-first cascade (replace.md, regex+checksum, dual-NER,
gazetteer, splitter) is the whole detection story and Stage 0/Stage 5 are
unaffected. `CORP_LLM_ORACLE_ENABLED=0` with `CORP_LLM_LOCAL_FIRST=0` refuses
to boot rather than run with no deterministic floor at all — see
`docs/plans/20260713-oracle-toggle-compose-quickstart.md`.

## 9. Invariants — never weaken these

| ID | Invariant | Enforced by |
|---|---|---|
| M1-14 | **No originals leak** across six surfaces: (i) logger emissions, (ii) error bodies, (iii) exception traces, (iv) metric labels, (v) forwarded headers, (vi) pod stdout | `tests/invariants/test_no_originals_leak.py` |
| M2-7 | **No BYOK credential in audit**: the `Authorization` value never appears in any audit surface | same test corpus + NEVER gate |
| M3-10 | **Vector drops NEVER**: an injected NEVER-key record reaches no sink | integration assertion |
| M1-9 | **Length-descending substitution** (forward + reverse) | `placeholder.py`, `litellm_hook.py` |
| — | **Per-request placeholder bijection** (same original → one token; distinct originals → distinct tokens) | `placeholder_allocator.py` |
| — | **Depth-guard fail-closed** (`_MAX_JSON_DEPTH=64` → `400 E_BAD_REQUEST` on sanitize) | `content_blocks.py`, `litellm_hook.py` |
| — | **NEVER gate, in-process (recursive, primary) + Vector (flat backstop)** (defense in depth) | `audit/invariants.py` + Vector VRL |

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
| (e) | **`_corp_gateway_request_id` reaches the outbound Anthropic body on `/v1/chat/completions`.** `pre_call` writes this correlation key to four places in `data`, one of them the top level, and litellm's chat-completions adapter carries unknown top-level keys into the request it sends. Observed with the subscription bridge **off** as well as on, so it is independent of that bridge. The value is a request id (litellm's call id or a generated UUID), never user content, so it is not an M1-14 leak. The primary `/v1/messages` route — the one Claude Code uses — is unaffected. Not fixed opportunistically because the key is the audit-attribution fallback chain (`_REQUEST_ID_LOOKUP_PATHS`), which has its own regression history. | **Low** — correlation id only; affects the chat-completions route's acceptance upstream, not confidentiality |
| (f) | **F9 only guards the corp-LLM oracle client, not litellm's own global TLS switch.** `corp_llm_verify()` (`config.py`) is reached only through `bootstrap.build_corp_llm_client()`, itself called only when `CORP_LLM_ORACLE_ENABLED=1`. But litellm reads the SAME `SSL_VERIFY` env var directly, via `get_ssl_verify()` — at higher priority than `SSL_CERT_FILE` — for every upstream provider (`anthropic/`, `openai/`, `hosted_vllm/`), with no `CORP_ENV=prod` guard on that read. `SSL_VERIFY=false` therefore disables certificate verification stack-wide on any deployment fronted by litellm with the oracle off (the default posture — see `compose/docker-compose.yml`). Widening F9 to cover litellm's read is a `src` follow-up; `compose/` mitigates today by not exposing `SSL_VERIFY` as an operator-set `.env` key and hardcoding it `true`. | **Medium** — silent TLS-verification bypass for any deployment that sets `SSL_VERIFY=false` outside the documented `.env` surface |

**(a) and (c) are fixed; (d) is correct by design.** The remaining open items are
**(b)** — wiring the SIEM sink (gated on the SIEM target), see
[`remaining-steps.md`](remaining-steps.md) — **(e)**, and **(f)** — widening F9
to guard litellm's global `SSL_VERIFY` read, not just the oracle client's.

## 12. GA security hardening (F8–F11)

Low-severity hardening landed for GA (plan Task A8):

| # | Fix | Where |
|---|---|---|
| F8 | `CorpLlmHttpError` on a `>=400` corp-LLM response carries the **status code only** — never `resp.text`. A corp-LLM error body may echo the RAW, pre-sanitization request; embedding it would ride the exception chain into logs/audit (an M1-14 surface: exception traces). | `corp_llm/client.py` |
| F9 | `corp_llm_verify()` **refuses `SSL_VERIFY=false` when `CORP_ENV=prod`** (raises at resolution). Disabling TLS verification on the corp-LLM call — which carries raw user content — is a demo-only convenience; in prod, pin an internal CA via `CORP_LLM_CA_BUNDLE` instead (CA bundle keeps verification ON and is still honored in prod). | `config.py` |
| F10 | In-proc NEVER-gate made **recursive** (see §6). | `audit/invariants.py` |
| F11 | Operator RBAC JWT verification **pinned to RS256** with **`aud`/`iss` verification** and empty-key rejection (see below). | `auth/rbac.py` |

### F11 — RBAC RS256 + aud/iss (BREAKING CHANGE)

`verify_operator` (gateway-admin RBAC) previously decoded the operator JWT with
the caller-configured `CORP_GATEWAY_OIDC_ALG` and **no** audience/issuer check. An
`HS256` algorithm with an empty or leaked symmetric key made an operator token
**forgeable**. Now:

- Verification is **pinned to `algorithms=["RS256"]`** — an `HS256` (or `none`)
  token is rejected. **`CORP_GATEWAY_OIDC_ALG` is no longer honored.**
- `aud` and `iss` are **required and verified** against
  `CORP_GATEWAY_OIDC_AUDIENCE` / `CORP_GATEWAY_OIDC_ISSUER`. A missing/mismatched
  claim, or unset expected value, is rejected (fail-closed, per the M4 matrix).
- An **empty `CORP_GATEWAY_OIDC_KEY`** is rejected (no signature to verify).

**Migration.** Any deployment relying on `HS256` operator tokens **stops
validating** and every operator call is denied until it switches to
RS256-signed tokens and sets `CORP_GATEWAY_OIDC_AUDIENCE` +
`CORP_GATEWAY_OIDC_ISSUER` to the Keycloak values. `CORP_GATEWAY_RBAC=0` still
bypasses RBAC for local dev. RS256 verification needs the `cryptography` package
(the `oidc` extra); without it `verify_operator` raises a clear `RuntimeError`
rather than falling back to a weaker algorithm. The dev-facing upgrade note
belongs in `docs/ops/upgrade.md` (plan Task B8).

## 13. Subscription-auth bridges

Two opt-in flags let the developer pay for the upstream call with their own
subscription OAuth token instead of a shared API key, so no provider API key
sits on the laptop or in the cluster:

| Flag | Upstream | Accepted token |
|---|---|---|
| `CORP_LLM_FORWARD_CHATGPT_AUTH` | ChatGPT Codex (OpenAI Responses) | Codex OAuth bearer |
| `CORP_LLM_FORWARD_ANTHROPIC_AUTH` | Anthropic | `sk-ant-oat…` OAuth tokens **only** |

Both read the same inbound `Authorization` bearer, so **exactly one may be on**.
Both flags set is refused at boot by `build_guardrail()` and by
`gateway-admin config check` (`settings.forward_auth_conflict`). The runtime
refusal is the load-bearing one: the compose and demo boots these bridges ship
on never call `settings.validate()`. A set `LITELLM_MASTER_KEY` is refused the
same way while either bridge is on — with a master key, litellm reads the
inbound `Authorization` as one of its own virtual keys and answers 401 before
`pre_call` runs, so the bridge could never see the token. Presence counts, not
truthiness: litellm treats a blank `LITELLM_MASTER_KEY=` as set.

The rest of this section is the Anthropic bridge (`litellm_hook.py`,
`_anthropic_upstream_headers` + the `forward_anthropic_auth` branch in
`pre_call`). Operator setup is in
[`ops/configuration.md`](ops/configuration.md) and
[`ops/install.md`](ops/install.md).

### What it forwards, and what it does not

On a request whose model alias resolves to the `anthropic` provider, and only
when the flag is on, `pre_call`:

- copies the bearer value into `data["api_key"]`. That is what selects litellm's
  own Anthropic OAuth branch, which emits `Authorization: Bearer <token>`
  upstream, adds `oauth-2025-04-20` to `anthropic-beta` and
  `anthropic-dangerous-direct-browser-access: true`, and suppresses `x-api-key`;
- copies the allowlisted **non-auth** inbound headers into
  `data["extra_headers"]`. The allowlist is exactly `anthropic-beta`,
  `anthropic-version`, `user-agent` (plus `authorization`, which is selected for
  validation and then excluded from `extra_headers`). Every other inbound header
  is dropped;
- **never** selects `X-Corp-Auth` — the corp token is skipped before the
  allowlist is consulted, on top of the normal `strip_corp_token` path
  (invariant 4);
- drops `data["metadata"]` and a top-level `data["user"]`, and drops the same
  two keys from litellm's own `model_call_details` copy of the request (see
  below).

Rejected: a missing `Authorization`, a non-`Bearer` scheme, an empty bearer, a
bearer carrying CR or LF, and any token that does not start with litellm's
`ANTHROPIC_OAUTH_TOKEN_PREFIX` (`sk-ant-oat`) — including a plain
`sk-ant-api…` API key. Each is `401 E_PROVIDER_AUTH` with an audit record
(`status="failed"`) and `gateway_failure{component="auth"}`, and the refused
credential reaches no error body, exception trace, metric label, audit record or
log line (`tests/invariants/test_no_originals_leak.py`).

API keys are refused deliberately, not as a shortcut. On the non-OAuth branch
litellm adds `x-api-key` while the inbound `Authorization` is still merged in,
so the outbound request would carry two competing auth schemes. Plain-API-key
BYOK on this bridge is a separate decision that has not been made.

### Invariant 3 on this route

Invariant 3 (BYOK `Authorization` passthrough) holds unchanged: the inbound
`Authorization` header is left in every header bucket exactly as it arrived, and
the bridge only *reads* it. The upstream `Authorization` header litellm builds
from `api_key` is the token's **one authorized destination**; the token
appearing anywhere else — another header, the request body, the router's model
list, the proxy's log stream — is a leak.

That rule is pinned on both sides of the process boundary, because no single
harness sees both:

| Surface | Pinned by |
|---|---|
| Logger emissions, error bodies, exception traces, metric labels, audit records, forwarded header buckets | `tests/invariants/test_no_originals_leak.py` (in-process) |
| Upstream request headers and body, retained router deployments, the litellm process's own stdout/stderr | `tests/integration/test_anthropic_oauth_outbound.py` (runs the pinned litellm image against a capturing upstream) |

Every assertion in the capture suite depends on docker, the pinned image and
container → host reachability, so on a machine without them it skips. CI sets
`CORP_REQUIRE_PROXY_CAPTURE=1`, which turns each of those skips into a failure —
otherwise a daemon, registry or network fault produces a green job that verified
none of it. The suite asserts that wiring against `.github/workflows/ci.yml`
itself, so dropping the variable fails the run rather than silencing it.

### Provider gating is defence in depth, not a routing guarantee

The bridge runs only when `_detect_provider(data) == "anthropic"`. That check
reads the **client-visible model alias** (`data["model"]`) and nothing else.
litellm resolves the actual deployment *after* the pre-call hook runs, and a
litellm `model_name` may map to any `litellm_params.model` — so on a config with
a `model_name: "*"` catch-all, a `claude-…` alias passes the gate and can still
land on a non-Anthropic upstream, taking the subscription token with it.

The gate is therefore a cheap second line of defence against copying the token
onto an OpenAI or corp-vLLM call. **The binding control is the deployment
shape**: a litellm config in which every route is `anthropic/` and there is no
wildcard. `docker/anthropic-oauth/litellm-config.yaml` is such a config, and
`tests/test_anthropic_oauth_profile.py` pins both properties. Do not read the
gate as a guarantee it cannot make.

### The `metadata` / `user` scrub

The gateway sanitizes `messages`, `system` and `instructions`. It never
sanitizes `metadata` or a top-level `user`, and both egress to Anthropic:

- on the **chat-completions adapter**, litellm maps a top-level `user` string to
  `metadata.user_id` and copies `metadata.user_id` into the outbound body. Its
  only filter rejects nothing but complete email and phone shapes;
- on the **`/v1/messages` pass-through route** — the one Claude Code actually
  uses — `metadata` is absent from the declaratively "supported" params list,
  but *nothing filters the request against that list*, so a caller-supplied
  `metadata` is forwarded intact.

So the scrub is load-bearing on **both** routes. The bridge is gated by provider
rather than by call type, so both are reachable with the flag on.
`tests/litellm_hook/test_litellm_route_assumptions.py` pins both litellm
behaviors, and deleting the scrub turns the capture tests red with the canary
visible in the upstream body.

**Drop, not sanitize.** litellm's proxy fills `data["metadata"]` with dozens of
internal accounting keys of mixed types, so there is no single field to narrow
to; sanitizing would mint placeholders in fields the response path never
reverses, leaving pairs no reverse pass consumes; and a key-specific scrub would
silently reopen the hole the day litellm copies one more metadata key into the
body. The cost is litellm's own accounting metadata on this route, which is
acceptable because the route ships without virtual keys and therefore without
spend tracking anyway. Audit attribution is unaffected — `user_id` / `team_id`
come from `AuthMiddleware`, and `request_id` keys on `litellm_call_id`.

### The identity-preamble carve-out

Anthropic's OAuth-authenticated route accepts a request as a Claude Code request
on the strength of the leading `system` block, matched by **exact string
equality** against three fixed client literals (`sanitizer/identity_preamble.py`).
One redacted character stops it being that block — and with the production
detector set, spaCy tags `Claude` as PERSON and `CLI` as ORG, so the sanitizer
really did rewrite it before this carve-out existed.

The exemption is deliberately narrow. It applies **only** when:

- `CORP_LLM_FORWARD_ANTHROPIC_AUTH` is on **and** the request resolves to the
  Anthropic provider — off the bridge, an identity literal is ordinary content;
- the field is `system`. Never `instructions`, a user message, a `tool_result`
  or a `document` — an operator `replace.md` rule or gazetteer entry must still
  be able to redact the same string pasted into those;
- the block is the **leading** one. Only `x-anthropic-billing-header:` blocks may
  precede it (litellm keeps those on the first-party Anthropic route, so the real
  payload can arrive with the identity block at index 1). A later occurrence,
  after any ordinary prompt block, is sanitized like any other leaf;
- the match is **byte-exact**. No `strip()`, no whitespace tolerance: padding
  would both fail upstream anyway and let a caller ride unbounded text into an
  unchecked leaf.

Two properties keep this from being a hole. The exempt strings are fixed client
constants, never user content, so exempting them cannot leak an original
(M1-14). And the exemption is **rewrite-only**: the block is still collected by
`collect_text`, so the Stage-0 payload classifier and the Stage-5 DLP egress
guard read it exactly as before, and the fail-closed size check runs before the
carve-out can apply. This is the same rewrite-vs-scan split already used for
block `signature` fields.

Neighbouring `system` blocks are unaffected, the billing-marker blocks ahead of
the identity one included: those go through the walker **whole**, marker text and
all. An earlier revision held the fixed `x-anthropic-billing-header:` prefix back
and reattached it after sanitizing the remainder; because `replace.md` rules are
literal substring matches over the text handed to the orchestrator, that hid every
rule whose pattern reached across the prefix/remainder boundary and then rebuilt
the full original on the way to Anthropic — an M1-14 violation Stage 5 cannot
catch, since it does not replay `replace.md`. Nothing is reattached now. If a rule
rewrites the marker the identity block stops being a *leading* one and Anthropic
may refuse the request; a refused request is a UX cost, a reconstructed original
is a leak. `tests/sanitizer/test_oauth_system_preamble.py` drives real `pre_call`
with the production detectors — including a rule spanning that boundary — and the
capture test asserts a redactable email in a neighbouring block still comes back
as `[EMAIL_nnn]`, so the byte-identical assertions cannot go quietly vacuous.

### Credential retention in the litellm process

litellm treats any request carrying `api_key` as a clientside credential:
`_handle_clientside_credential` builds a `Deployment` whose id hashes the
dynamic params and upserts it into `Router.model_list`, **including the raw
key**. Nothing evicts it. Each distinct `sk-ant-oat…` value therefore leaves one
credential-bearing deployment resident for the lifetime of the proxy process;
re-using a token adds none. Measured, not assumed —
`test_each_distinct_token_retains_exactly_one_router_deployment`.

This is **not introduced by the Anthropic bridge** — the ChatGPT Codex bridge
has had the identical shape since it shipped — but this route adds a second
place it happens, so more distinct developer credentials accumulate in memory.
Bounds and mitigation:

- the admin surface does not hand them back: `/model/info` returns none of the
  tokens;
- the tokens do not reach the process's stdout/stderr (pinned by the capture
  test) or any audit surface;
- **restarting the litellm process is the mitigation.** There is no eviction
  knob. Treat process lifetime as the retention window when sizing how long a
  compromised subscription token stays resident.

### Topology: demo overlay only

Subscription auth runs on the `anthropic-oauth` docker-compose overlay
(`docker-compose.demo.yml` + `docker-compose.anthropic-oauth.yml`) and nowhere
else.

| Deployment | Status | Why |
|---|---|---|
| `anthropic-oauth` compose overlay | **Supported** | Anthropic-only routes, no wildcard, no `LITELLM_MASTER_KEY`, so the inbound bearer reaches `pre_call` |
| Production compose | **Unsupported** | `Authorization` there already carries the litellm virtual key. Putting the OAuth token on the wire needs a second header, and which header carries which credential is an open governance decision |
| Helm chart | **Unsupported** | Its litellm ConfigMap routes `"*"` to the corp vLLM and has no `anthropic/` route, so litellm's OAuth branch is unreachable — and that wildcard is exactly the shape the alias gate cannot protect |

Both unsupported cases are blocked on the same header-layout decision (the
deferred litellm-governance plan's first gate), not on missing code.

**Consequence to accept knowingly:** the supported overlay has no litellm
virtual keys, and therefore no native budget, rate-limit or quota enforcement.
**Subscription auth and virtual-key governance are mutually exclusive today.**
A rollout that needs both has to wait for the header-layout decision.
