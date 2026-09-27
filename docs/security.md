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

### Refused at the route gate (never reaches a provider)

Coverage above describes what the guardrail rewrites on the routes it runs on.
A class of litellm handlers takes user text and never calls `pre_call_hook` at
all — the three confirmed bypasses (both token counters and the Responses
WebSocket) and every other hook-less handler the table refuses — and a further
class reaches the hook but is not rewritten, because the hook reads only
`messages` / `input`.
Since the route gate (§14) all of them are refused before litellm's router sees
the request — default-deny, so a route litellm adds in a future release is
refused too until it is classified.

| Route | Why it is refused | Reason code |
|---|---|---|
| `POST /v1/messages/count_tokens` | No hook: raw `messages`/`system`/`tools` went to Anthropic's CountTokens API unsanitized, unaudited. Claude Code calls it for its context display | `route_gate_listed` |
| `POST /v1/responses/input_tokens` | No hook: raw Responses `input` went to the provider's token counter | `route_gate_listed` |
| `WEBSOCKET /v1/responses`, `/v1/realtime`, `/openai/v1/realtime` | The handshake reaches the hook with no content and every `response.create` frame after it never does. Refused at the handshake, before connect | `route_gate_websocket` |
| `POST /utils/token_counter` | Refused on every call, whatever the query string: the gate classifies `(method, path)` only. `call_endpoint` is a query **parameter**, so `?call_endpoint=true` sends the raw `messages`/`prompt` to the provider's counting API from a path that reads like an admin utility | `route_gate_listed` |
| `POST /queue/chat/completions`, `/mcp-rest/tools/call`, `/search/{tool}`, `/apply_guardrail`, `/health/test_connection` | Take user text and reach a provider without the hook | `route_gate_listed` |
| `POST /v1/completions` | Reaches the hook, but the hook never reads `prompt`, so nothing is rewritten | `route_gate_listed` |
| `/v1/embeddings`, `/v1/moderations`, `/v1/audio/speech`, the provider-native passthrough trees (`/anthropic/…`, `/openai/…`, `/{provider}/…`) and the rest of `_NON_CHAT_INPUT_CALL_TYPES` (`litellm_hook.py:2099`) | Reach the hook as the documented no-rewrite set: previously DLP-scanned only, now refused outright — a DLP scan blocks a *known* pattern, it does not sanitize | `route_gate_listed` |
| `POST /api/event_logging/batch` | Claude Code's telemetry batch. No hook, and it carries whatever the client chose to put in it | `route_gate_listed` |
| litellm's management surface: the admin JSON API (`/key/*`, `/team/*`, `/model/*`, `/policies*`, `/guardrails*`, …), spend analytics, login / SSO / invitations, the public catalogue, UI assets, `GET /`, lazy warm-up, and every `/health/*` route except the three probes | Refused by design: identity is `X-Corp-Auth` in the gateway's own store and observability is Langfuse, so none of it is needed at runtime — and several of these routes can switch the sanitizer off after boot (§14, "The management surface is refused") | `route_gate_listed` |
| Anything absent from the table, including litellm's mounted sub-apps (the admin **UI**, `/swagger`, `/docs`, `/openapi.json`) | Default-deny. `ast` cannot see inside a mounted ASGI app, so it gets no entry | `route_gate_unlisted` |

**Pre-flight token counting is therefore unavailable** — a deliberate trade, see
§11 (i). `usage.input_tokens` on every real turn is the exact, post-sanitization
count; Claude Code falls back to its own estimate for the indicator.

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
| `profileUnavailable` (D4, when `profile_ids` set) | **fail-closed** (503 `E_PROFILE_UNAVAILABLE`) — a team's resolved profile bundle is missing or malformed; never fall through to un-profiled egress (invariant 6). Empty `profile_ids` → passthrough (no profile resolution, no 503). A team-config store that is unreachable or past its per-call bound is the same 503, counted as `gateway_failure{component="team_config"}`, logged by exception class only |
| `providerBlocked` (D4) | **block** (403 `E_PROVIDER_BLOCKED`) — the merged `allowed_providers` policy rejects the upstream target; a clean policy denial before any content processing, no raw body |
| `spanApplyFailed` | **fail-closed** (500 `E_SPAN_INVALID`) — `apply_spans` rejects a pre-selected replacement span that no longer matches the segment text (e.g. a stale Cache-A/allocator remap); `StaleSpanError` (`sanitizer/placeholder.py`) is mapped to an audit record + `gateway_failure{component="sanitize"}` rather than escaping as a generic, undocumented 500 |
| `routeGate` | **default-deny, fail-closed.** Every request is classified by `(method, path)` before litellm's router sees it (`route_gate/table.py`, generated from litellm's own source). A route the table marks REFUSE is 403 `E_ROUTE_BLOCKED`; a route with no entry at all is 404, same error code; a websocket handshake is refused before connect; a path is 403 without ever being matched when its raw bytes carry `%2f`, `%00` or `%2e%2e` (any case) or any non-ASCII byte, or its decoded form carries `..`, `//` or NUL. The gate's OWN faults are failures, not refusals: a REWRITTEN route while the guardrail callback is not registered is 503 `E_ROUTE_GATE_UNARMED` and is never forwarded, and any exception in classification is 500 `E_ROUTE_GATE_ERROR` — both also record `gateway_failure{component="route_gate"}`. No off switch: the only widening is `CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH`, which can add PASSTHROUGH entries and nothing else. The process exits 70 at startup rather than serve with the callback absent |
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

### 8.1 `vectorBufferFull` on the compose stack — `continue`, not the default

The matrix row above is unchanged and remains the source of truth. This note
records where one shipped deployment sits on it, because the deviation was
previously silent.

**`compose/` takes the `audit_buffer_full=continue` opt and cannot do
otherwise. That stack does NOT provide the matrix's default fail-closed (503)
behaviour: requests keep egressing while audit delivery is stalled.**

Why it cannot: `compose/docker-compose.yml` leaves `CORP_AUDIT_SINK` unset, so
`audit/factory.py`'s default `StdoutSink` applies and the gateway's whole audit
action is one line on its own stdout, which always succeeds. Vector then tails
that log file **out of process**, from another container. No signal path exists
from Vector's buffer state back into `pre_call` / `post_call`, so nothing in the
request path can observe saturation and return 503; `when_full: block` only
stops Vector reading. Closing the gap needs a health/buffer gate in `src/`
feeding the hook — not built.

Residual risk, and what bounds it: while the sink is stalled, accepted audit
records live only in the `litellm` container's docker json-file logs plus
whatever already reached Vector's disk buffer. Vector's file glob covers the
active `-json.log` **and** its numbered rotations (`-json.log.[0-9]`, two-digit
form too), so a rotation no longer discards records on its own — but a file
rotated past `max-file` is deleted by docker, so **docker log retention, not the
disk buffer, is still what bounds audit durability here**. Separately, Vector's
HTTP sink retries only 408/429/5xx, so a wrong or rotated Langfuse project key
(401) is dropped rather than buffered — and the file source checkpoints on read,
not on delivery, so those bytes are not re-read on a restart. Recovery means
deleting the source's checkpoint under `data_dir`; records already rotated away
are unrecoverable.

Operators of that stack must therefore size docker log retention
(`LITELLM_LOG_MAX_SIZE` × `LITELLM_LOG_MAX_FILE`, set on the `litellm` service's
own `logging.options`) against the longest tolerated Langfuse outage, and alert
on buffer growth and on Vector's `Events dropped` errors. Note that setting these
in the **daemon's** `log-opts` does not work: docker merges daemon-level log-opts
into a container only when the container's log driver equals the daemon's default
driver, and that service pins `json-file` because the audit pipeline requires it.
Concrete commands: `compose/README.md` "Audit buffering is not fail-closed".

The Helm deployment is not covered by this note; it is a separate composition.

### 8.2 Audit-source trust on the compose stack — a host precondition

Additive note, same scope as §8.1: it records a precondition of one shipped
deployment, and changes nothing in the matrix above.

`compose/vector/vector.yaml` reads **every** container's log on the host (a
read-only bind of `/var/lib/docker/containers`, chosen over mounting the docker
socket, which is host root). Its `gateway_container_only` filter scopes the
pipeline to the gateway by requiring the docker json-file `attrs` stamp that the
`litellm` service's `com.corp-llm-gateway.audit-source` label plus its
`logging.options.labels` produce.

**That filter is a misconfiguration guard, not a security boundary.** The label
name and value are public and unverified. Any container started on the same host
with the same label and the same `--log-opt labels=…` receives the same `attrs`
stamp; if it then prints `AuditEvent`-shaped JSON (`request_id` +
`redaction_count`, no NEVER field), the line passes `gateway_container_only`, the
NEVER-fields gate and `audit_only`, and lands in Langfuse as a genuine-looking
audit record. The filter raises the attacker requirement from "can write a log
line" to "can start a container on this host" — a real improvement, and the
whole of it.

Consequences to accept before deploying that stack:

- **No untrusted `docker run` on the host.** Membership of the `docker` group,
  and any CI runner, agent or sidecar with daemon access, is equivalent to write
  access to the audit trail. Restrict it the way you would restrict the audit
  store itself.
- The exposure is **forgery (insertion), not disclosure**. Nothing here lets a
  co-located container read gateway audit records, and invariant #1 / the
  NEVER-fields gate are unaffected: a forged record still cannot carry a NEVER
  field through, and originals still never reach any of these surfaces.
- Closing it properly needs a **private channel** only the gateway can write — a
  dedicated bind-mounted audit file, or a unix socket, in place of container
  stdout. That is a change to the audit sink in `src/corp_llm_gateway/audit/`
  plus the Vector source, not to the compose stack, and is not built.

The Helm deployment is not covered: there the audit path is the pod's own log,
scoped by the k8s log collector, and a different trust model applies.

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
| 7 | **Default-deny route gate**: no request reaches a provider unless the table says the hook rewrites its body, and the gateway does not serve at all unless that hook is registered. An unclassified `(method, path)` is refused; an unarmed gate refuses every rewritten route; startup exits rather than serve half-configured (§14) | `route_gate/table.py` + `tests/route_gate/test_litellm_route_guard.py` + `asgi.py` (exit 78 / 70) |
| 7a | **The issuance surface terminates in the gateway and is bounded**: `POST /internal/issue-token` never reaches litellm; header-only bearer, no body, own in-flight cap and rate, per-subject minting under an advisory lock, one store bound; error bodies and logs carry codes only (§14) | `route_gate/table.py` (`GATEWAY_ROUTE_TABLE`) + `healthz/server.py` + `tests/invariants/test_issuance_no_leak.py` + `tests/invariants/test_issuance_error_codes.py` |
| 7b | **The limiter's refusals never inspect, echo or log the body, and no slot is held before the body is complete**: `E_CAPACITY` (429), `E_BODY_TIMEOUT` (408), `oversize:blocked` (422) answer before auth, litellm and the sanitizer; a task several requests share is started with `inflight.spawn_shared` and never cancelled by one request's disconnect; the cap is not authorization (§14) | `route_gate/inflight.py` + `tests/route_gate/test_inflight.py` + `tests/test_inflight_served_stack.py` |

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
| (g) | **A payment card buried between stray digits on both sides is not detected.** `BANK_CARD` scans inside a glued digit run, but only for a PAN that reaches at least one end of the run (`_CARD_STRAY_MARGIN = 0`, `detectors/regex_checksum.py`). A PAN with junk digits on BOTH sides — `123` + PAN + `123` — is missed, and so is any deliberately padded one. The threat model for this detector is **accidental paste** (a developer drops a card into a prompt), not a determined insider: every realistic paste shape is still caught — a bare PAN, a PAN with a CVV or amount glued to either end, a grouped PAN with a glued tail, and a PAN glued to letters. Closing the gap needs unbounded-depth scanning, measured at **65.8% false positives on random 32-digit runs** (79.3% at 40) and climbing with run length, while a finite margin closes nothing — 7 junk digits in front escape at margin 6 exactly as at 0. Margin 6 cost 2-3x the false positives of 0 on ordinary long digit runs (19-digit nanosecond timestamps 27.9% → 18.4%; 20-digit 24.1% → 10.4%; 23-digit 34.1% → 11.9%) and bought no coverage against a padder, so it was dropped. Anyone deliberately obfuscating a card would equally defeat a regex with base64 or unusual spacing, so chasing depth in `regex_checksum` is unbounded work for no real adversary gain. The layers that can catch a card in prose context are **corp NER** (Workstream B, in progress) and the **Stage 5 DLP egress guard**. Pinned by `test_card_buried_between_stray_digits_is_a_known_limitation`. | **Low** — accepted; accidental paste is covered, deliberate obfuscation is out of this detector's scope |
| (h) | ✅ **FIXED** — **Helm deploys sent the client's `Content-Length` upstream with a longer, sanitized body.** `CORP_LLM_STRIP_INBOUND_HEADERS` was absent from the chart and defaulted to `0`, so the guardrail's `data["headers"]` bucket carried the inbound wire headers into litellm's upstream call; the provider then read the request truncated at the client's length (wire-captured on `/v1/messages` and `/v1/chat/completions` — a `"stream": true` cut off that way came back non-streamed). Every request whose sanitized body grew was corrupted. The flag now defaults **on** (`bootstrap.build_guardrail`) and the chart sets it explicitly. The dropped set (`_WIRE_HEADERS_TO_DROP`) is hop-by-hop / wire-level only and never includes `authorization` — BYOK passthrough (invariant 3) is unaffected, and `X-Corp-Auth` was already stripped unconditionally one step earlier (invariant 4). | **Resolved** |
| (i) | **Pre-flight token counting is unavailable — accepted, not a gap to close here.** `POST /v1/messages/count_tokens`, `POST /v1/responses/input_tokens` and `POST /utils/token_counter` (refused whatever the query string — `?call_endpoint=true` is the shape that leaks, but the gate classifies `(method, path)` only) are refused at the route gate (§14), so a client cannot ask the gateway what a prompt will cost before sending it. Reasons: litellm's upstream counter cannot carry the per-request credential (it uses the deployment key and the hard-coded public URL) and its local counter uses an OpenAI/Claude-2 vocabulary that undercounts current Claude models by 15-35 %. `usage.input_tokens` on every real turn is an exact, post-sanitization count, and Claude Code falls back to its own estimate for the context indicator. A gateway-side adapter that runs the pre-call pipeline and then calls the provider's counter with the request's own credential is on the backlog. | **Accepted** — no confidentiality impact; a client-side estimate replaces an exact pre-flight count |

**(a), (c) and (h) are fixed; (d) is correct by design; (g) and (i) are accepted
with no fix planned.** The remaining open items are **(b)** — wiring the SIEM sink (gated on
the SIEM target), see [`remaining-steps.md`](remaining-steps.md) — **(e)**, and
**(f)** — widening F9 to guard litellm's global `SSL_VERIFY` read, not just the
oracle client's.

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

### Topology

Subscription auth is the production mode. It runs on the production compose
stack with the `docker-compose.oauth.yml` override, and on the demo
`anthropic-oauth` overlay (`docker-compose.demo.yml` +
`docker-compose.anthropic-oauth.yml`).

| Deployment | Status | Why |
|---|---|---|
| Production compose + `docker-compose.oauth.yml` | **Supported** | Anthropic-only routes, no wildcard, no `LITELLM_MASTER_KEY`, so the inbound bearer reaches `pre_call` |
| `anthropic-oauth` demo overlay | **Supported (demo)** | Same shape over the demo stack |
| Helm chart | **Unsupported** | Its litellm ConfigMap routes `"*"` to the corp vLLM and has no `anthropic/` route, so litellm's OAuth branch is unreachable — and that wildcard is exactly the shape the alias gate cannot protect |

**Consequence to accept knowingly:** there are no litellm virtual keys, and
therefore no native budget, rate-limit or quota enforcement. That is by design,
not a gap waiting for a header decision: litellm's management surface, `/key/*`
included, is refused at the route gate (§14), so no virtual key can be minted.
API-key mode (virtual keys) survives only as a test posture.

## 14. The route gate

`CorpLlmGuardrail` is a litellm **callback**, and litellm calls it only from the
handlers that go through its shared request processor. Handlers in the pinned
litellm (1.101.0) that do not — the three confirmed bypasses and every other
hook-less handler the table refuses — reached a provider with **no
`X-Corp-Auth` check, no sanitization, no Stage-5 DLP scan and no audit
record**; a further class reached the hook but was never rewritten, because the
hook reads only `messages` and `input`. §2 lists all of them. The route gate
closes that class of hole for good: whatever litellm's routers do, a request now
has to be classified before one of them can answer it.

### Where it runs

`src/corp_llm_gateway/asgi.py` is the only supported serve target
(`python -m corp_llm_gateway.serve`, the image ENTRYPOINT). It wraps litellm's
ASGI app — by wrapping, not `add_middleware` — so `RouteGateMiddleware` is
**outermost**: every middleware litellm adds sits inside it and none can answer
ahead of the gate. The `litellm` CLI and `litellm.proxy.proxy_server:app` must
never be the served target again; both serve the routers with nothing in front.

The middleware is pure ASGI, not `BaseHTTPMiddleware`, for two reasons that are
security-relevant: it must see `websocket` scopes (an HTTP middleware never
does, so a handshake would pass unclassified), and it must not buffer — SSE
streaming has to flow through untouched.

### The table is generated, not written

`route_gate/table.py` is regenerated from litellm's own source by an `ast`
collector (`tests/route_gate/litellm_routes.py`), and
`tests/route_gate/test_litellm_route_guard.py` re-runs that collector against
the installed litellm on every CI run. A route litellm adds in a future bump has
no entry, so it fails the guard until someone classifies it — and at runtime it
is refused by default-deny in the meantime. A hook-less `POST`/`PUT`/`PATCH`
route may only be PASSTHROUGH with a written justification, and the guard checks
that justification against the `ast`: a handler whose body calls
`litellm.acompletion`, `aresponses`, `token_counter`, `router.a*` or
`pass_through_request` cannot be excused by a comment.

### What a refusal looks like

| Reason | Status | Error code | When |
|---|---|---|---|
| `route_gate_listed` | 403 | `E_ROUTE_BLOCKED` | the table refuses this `(method, path)` |
| `route_gate_unlisted` | 404 | `E_ROUTE_BLOCKED` | no table entry — default-deny |
| `route_gate_websocket` | 403 | `E_ROUTE_BLOCKED` | a websocket scope or an `Upgrade: websocket` header, on any path |
| `route_gate_malformed` | 403 | `E_ROUTE_BLOCKED` | raw path carrying `%2f`, `%00`, `%2e%2e` (any case) or a non-ASCII byte, or decoded path carrying `..`, `//` or NUL (`route_gate/classify.py`); never matched against the table at all |
| `route_gate_unarmed` | 503 | `E_ROUTE_GATE_UNARMED` | a REWRITTEN route while the guardrail callback is not registered; never forwarded |
| `route_gate_error` | 500 | `E_ROUTE_GATE_ERROR` | classification raised; never forwarded |

The last two also record `gateway_failure{component="route_gate"}`. Every one
records `corp_llm_gateway_blocked_requests_total{block_reason=…}` and emits an
audit record carrying the reason and the error code — ALWAYS fields only, since
the gate refuses before any identity is resolved.

**A refusal never reads the request body** and never echoes one. The response
names the route the caller itself sent and nothing else; the log line carries
the reason, the scope type and a method narrowed to the known verbs — no path,
no header, no exception text (M1-14 surfaces ii, iii, vi;
`tests/invariants/test_no_originals_leak.py`).

### Startup: the gateway does not serve half-configured

litellm's own lifespan skips a missing config file in silence, which starts the
proxy with **no guardrail callback at all** — the fail-open this gate exists to
close. The entrypoint refuses first: exit 78 (`EX_CONFIG`) when litellm's config
is missing, unreadable, not YAML, or configures `pass_through_endpoints` or
`general_settings.database_url`, and when `CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH`
is malformed or names a refused route; exit 70 (`EX_SOFTWARE`) when litellm's
startup completes without a `CorpLlmGuardrail` in `litellm.callbacks`. Until that check
passes the gate is **unarmed**, and an unarmed gate answers 503 on every
rewritten route rather than forward it.

### The issuance route: gateway-owned, terminates locally, bounded

`POST /internal/issue-token` is the one body-method row in `GATEWAY_ROUTE_TABLE`.
It is PASSTHROUGH so the gate hands it to the gateway's own `HealthRouter`, which
answers it and never forwards it to litellm — with issuance off
(`CORP_GATEWAY_ISSUE_OIDC_ISSUER` unset) it is a local 404 `E_ISSUE_DISABLED`
for every method. It never takes an in-flight slot. When on, it is bounded like
any public endpoint, in this order:

1. **Route caps, before any work.** Its own in-flight cap
   (`CORP_GATEWAY_ISSUE_MAX_INFLIGHT`, default 4) and token bucket
   (`CORP_GATEWAY_ISSUE_RATE_PER_MINUTE`, default 30), answered 429
   `E_ISSUE_INFLIGHT` and 429 `E_ISSUE_THROTTLED` without queueing; a request
   refused by the first spends no bucket token.
2. **No body.** The Keycloak access token comes from the `Authorization:
   Bearer` header only. Any request body byte is refused (400 `E_ISSUE_BODY`);
   the read stops at 1 KiB and at 2 s (408 `E_ISSUE_BODY_TIMEOUT`).
3. **The JWT.** RS256 only; `exp`, `iat`, `iss`, `aud`, `sub`, `jti` and `azp`
   required; `aud` must include `CORP_GATEWAY_ISSUE_OIDC_AUDIENCE` and must not
   include the operator audience `CORP_GATEWAY_OIDC_AUDIENCE` (the two must
   differ — config check and boot refuse otherwise); `azp` must equal
   `CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID`. The JWKS fetch is HTTPS (in prod), no
   redirects, 3 s, 64 KiB, one shared in-flight refresh, and it is verified
   against `CORP_LLM_CA_BUNDLE` when set. A failure is 401 `E_OIDC_*`; a JWKS
   that cannot be fetched is 503 `E_JWKS_UNAVAILABLE`.
4. **Team.** The first group of `CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP`, in the map's
   order, that the user belongs to names the team; none is 403 `E_ISSUE_NO_TEAM`,
   and a mapped team that does not exist is 403 `E_ISSUE_UNKNOWN_TEAM` — the
   gateway never creates a team from claims.
5. **Minting policy, per `(iss, sub)`, in one Postgres transaction** under a
   transaction-scoped advisory lock (so replicas serialise, not just one
   process): a `jti` already used is 403 `E_ISSUE_REPLAY` (a unique index is the
   backstop); a second issuance within `CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS`
   is 403 `E_ISSUE_RATE` (revoked and expired rows count, so revoking does not
   reset it); past `CORP_GATEWAY_ISSUE_MAX_ACTIVE` live tokens the oldest are
   revoked. The transaction runs with `lock_timeout` 5 s and
   `statement_timeout` 8 s; either firing is 503 `E_ISSUE_BUSY`.
6. **One bound over steps 3-5**: `CORP_GATEWAY_ISSUE_STORE_TIMEOUT_SECONDS`
   (default 10, 5-300). Past it the request answers 503 `E_ISSUE_STORE_TIMEOUT`
   and frees its slot; a connection-class store failure is 503
   `E_ISSUE_STORE_UNAVAILABLE`; anything else is 500 `E_ISSUE_INTERNAL`. A
   cancelled statement gets a server-side cancel request within 0.5 s, then its
   connection is terminated (`pg_session.py`).

Success is 200 `{"corp_token", "expires_at"}`. Every issuance response carries
`cache-control: no-store`. Error bodies carry a code only, and the one log line
per request carries the status and the code — never the bearer, the minted
token, `sub`, groups or username (`tests/invariants/test_issuance_no_leak.py`;
the code set is pinned by `tests/invariants/test_issuance_error_codes.py`).

**Boot.** Issuance needs Postgres. The entrypoint exits 78 when it is set without
`CORP_LLM_PG_DSN`, partially configured, missing the `oidc` or `postgres` extra,
naming an unreadable `CORP_LLM_CA_BUNDLE`, or when Postgres refuses the DSN
(credentials, database name, syntax), the TLS handshake (including a DSN
`sslmode=require|verify-*` the server declines), or the privilege to read
`corp_tokens`, and when `corp_tokens` lacks the issuance columns or a valid
unique index `corp_tokens_oidc_jti_key` on `oidc_jti` alone. A Postgres the
network cannot reach (08xxx, 57P0x, 53300, a socket error) warns and boots;
readiness reports it (`pg_session.BOOT_PROBE_OUTCOMES`).

**The runtime auth path is bounded too.** Every rewritten request looks its
`X-Corp-Auth` token up in the same store: one lookup per token at a time
(single-flight, started outside the request so one caller's disconnect cannot
cancel it for the others), a 5 s statement bound and 6 s overall. A store that
cannot answer is 503 `E_STORE_UNAVAILABLE` with
`gateway_failure{component="token_store"}` — never a pass.

### The management surface is refused

Every litellm route that is not generation, model listing, a stored response by
id or one of three probes answers **403 `E_ROUTE_BLOCKED`**
(`route_gate_listed`), from everywhere and in both auth modes: the admin JSON
API, spend analytics, login / SSO / invitations, the unauthenticated public
catalogue, UI assets, `GET /` and `GET /routes`, lazy-router warm-up and the
non-probe `/health/*` rows — 462 admin-family rows plus 8 health rows. In
litellm 1.101.0 the litellm tables hold 26 PASSTHROUGH / 879 REFUSE / 8
REWRITTEN rows (913), plus 6 PASSTHROUGH rows in the gateway's own
`GATEWAY_ROUTE_TABLE` (the `/healthz/*` probes, `/metrics` and the issuance
route); `tests/route_gate/test_table.py` pins those counts and that no admitted
row carries a refused-family reason.

**Why refuse, not gate by network.** Nothing the gateway needs at runtime lives
in litellm's management plane. Identity is the opaque `X-Corp-Auth` token in the
gateway's own store — minted by `POST /internal/issue-token` from a Keycloak
login, revoked with `gateway-admin token revoke` — and observability is the
audit pipeline into Langfuse. What the surface would add is a way to change the
sanitizer's behaviour after boot. The litellm adoption plan
(`docs/plans/20260926-litellm-guardrail-api-adoption.md`) found three such paths,
each probe-confirmed:

- **hazard 9** — `POST /guardrails/apply_guardrail` logs the original text and
  swallows post-call exceptions (it was already REFUSE; it stays so);
- **hazard 14** — `POST /policies` / `PUT /policies/{id}/status` can create a
  pipeline naming our guardrail, which the pre-call loop then skips: zero
  sanitizer invocations, originals egress;
- **hazard 15b** — registering a second guardrail under our name
  (`POST /guardrails`, `PUT|PATCH /guardrails/{id}`) makes litellm's load
  balancing run the other callback instead of ours.

A network rule (loopback, tunnel, nginx allow-list) would leave all three one
misconfiguration away. A REFUSE row holds inside the image, on every path in.

**Health rows, one by one.** Shipped probes use only the gateway's own
`/healthz/live` and `/healthz/ready`.

| Route | Verdict | Why |
|---|---|---|
| `GET` / `OPTIONS /health/liveliness`, `/health/liveness` | kept | Read the shutdown flag and return a constant (`_health_endpoints.py:1847-1865`, OPTIONS `:1884-1901`) |
| `GET` / `OPTIONS /health/readiness` | kept | Verified in litellm 1.101.0 (`_health_endpoints.py:1740-1760`): reads the shutdown flag and, when litellm has a database, a Prisma ping cached for 15 s and bounded at 4 s (`:1410-1458`, `:1720-1737`). It never calls a provider; neither does the opt-in `allow_public_health_readiness_details` branch (`:1585-1667`). OPTIONS (`:1868-1881`) returns a constant |
| `GET /health` | refused | Runs model health checks **against providers** when background checks are off (`:1036-1048`), and without a master key litellm accepts an empty identity (`user_api_key_auth.py:1622-1633`) |
| `GET /health/drain` | refused | Flips litellm's process-wide shutdown state (`:1799-1843`) |
| `GET /health/backlog`, `/history`, `/latest`, `/license`, `/readiness/details`, `/shared-status` | refused | Operator diagnostics, not probes |

`GET /health/services` and `POST /health/test_connection` were already refused
(they reach providers).

**API-key mode is a test posture** (DRI decision, 2026-09-27). Its developers
held a litellm virtual key minted through `POST /key/generate`; with `/key/*`
refused there is no onboarding path, and handing developers the master key is
not an option. Subscription mode is the production mode. The container suite
(`tests/integration/test_route_gate_container.py`) keeps API-key mode covered —
exit 70 and the route gate — by inserting the sha256 of a synthetic `sk-…` key
straight into litellm's `LiteLLM_VerificationToken`, which is how litellm looks
a key up.

`CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH` cannot re-open any of this (see
"Widening it").

### The in-flight cap: after the verdict, gateway-owned

An armed REWRITTEN request also takes a slot in the gateway's in-flight limiter
(`route_gate/inflight.py`, `CORP_LLM_MAX_INFLIGHT`, default 64 per pod) and holds
it for the whole request, stream included. PASSTHROUGH routes (probes, model
listing, the issuance route) never count. The cap is the gateway's because
litellm's own is dead here: litellm 1.101.0 honours
`general_settings.global_max_parallel_requests` only in its legacy limiter
(selected by `LEGACY_MULTI_INSTANCE_RATE_LIMITING`), and no shipped config sets
either.

**No slot before the body is complete.** The limiter is the single reader of the
request's `receive`. It drains the whole body first, before any slot is taken
and before any authentication, and only then tries to acquire; so the body is
bounded on its own:

- it must arrive within `CORP_LLM_BODY_READ_SECONDS` (default 30): past it 408
  `E_BODY_TIMEOUT` (`block_reason` `body_timeout`);
- at most `CORP_LLM_MAX_DRAINING` requests read a body at once (default 4 ×
  the cap): the next gets 429 `E_CAPACITY` unread;
- all buffered body bytes, being read or held for an admitted request until it
  ends, share `CORP_LLM_MAX_DRAINING_BYTES` (default 512 MiB): a declared
  `Content-Length`, or a chunk, that would pass it gets 429 `E_CAPACITY` and
  gives back what it held;
- one body over 25 MiB is `oversize:blocked`, 422.

An unauthenticated client that never finishes its body therefore holds no slot,
and one that finishes it after the last slot went gets 429 then. Every
`E_CAPACITY` carries `Retry-After: 1` and is counted as
`corp_llm_gateway_blocked_requests_total{block_reason="capacity"}`; the limiter
answers before litellm or the sanitizer runs, and none of its refusals reads,
echoes or logs the body.

**Disconnects end the request.** The limiter replays the body to litellm and
watches the socket. A client that disconnects — during our pre-call hook, before
the first byte or mid-stream — gets its request cancelled, its leftover tasks
cancelled, one `cancelled` audit record with counts only (`E_CLIENT_DISCONNECTED`)
and its slot back within 2 × `CORP_LLM_CANCEL_GRACE_SECONDS`
(`docs/ops/capacity.md`). A pre-call litellm reaches after the cancel is refused
408 `E_CLIENT_DISCONNECTED` and writes no second record.

**Shared tasks are never a request's.** A task that several requests await (the
per-token auth lookup, the JWKS fetch) is started outside every request
(`inflight.spawn_shared`), and litellm's logging worker is started in the
lifespan and never tagged, so one caller's disconnect never cancels work under
the others or stops later audit callbacks.

**The cap is capacity, not authorization.** It decides how many requests run,
never which: every admitted request still passes the corp-token check, the
sanitizer, the DLP guard and the audit, and a refusal here grants nothing.

### Consequences to know

- **Pre-flight token counting is gone.** The two token-count routes and
  `POST /utils/token_counter` (unconditionally — the gate reads `(method, path)`,
  never the query string) are refused. `usage.input_tokens` on every real turn is
  the exact post-sanitization count; §11 (i) records the trade and the backlog
  item.
- **`POST /api/event_logging/batch` answers 403.** That is Claude Code's
  telemetry batch: no hook, and it carries whatever the client chose to put in
  it. Client-side telemetry is therefore dropped at the gateway — accepted, since
  the alternative is an unclassified body leaving the boundary.
- **litellm's admin UI and admin API are not served.** `ast` cannot see inside
  a mounted ASGI app, so `/ui`, `/swagger`, `/docs`, `/openapi.json` and the
  other mounts get no table entry and are refused as unlisted. The JSON admin
  API (`/key/*`, `/team/*`, …) is pinned route by route as REFUSE — see "The
  management surface is refused" above.
- **`HEAD` on a litellm route answers 405, not a refusal.** The gate's rule is
  that HEAD inherits its path's GET verdict, so it is admitted — but FastAPI's
  `APIRoute`, unlike a plain Starlette `Route`, does not add HEAD to a GET route,
  so litellm answers 405. The gateway's own `HEAD /healthz/*` answers 200.
- **An unreachable team-config store stops every rewritten request.** The
  guardrail reads the caller's team config on every request, even for a team
  with no profiles, and a store that cannot answer within its bound (5 s) is
  503 `E_PROFILE_UNAVAILABLE` with `gateway_failure{component="team_config"}`
  — fail-closed, never an un-profiled pass (`docs/ops/runbook.md`).
- **Background responses are unsupported, not blocked.** `POST /v1/responses`
  with `background: true` is admitted and the upstream body is sanitized, but the
  client then polls `GET /v1/responses/{id}`, which re-runs under a different
  request id. Cache B is keyed per conversation and `conversation_id ==
  request_id`, so desanitization of the polled result is not guaranteed. Neither
  the create nor the poll returns an original.

### Widening it

There is no off switch. `CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH` takes
`"METHOD /path"` items, comma- or newline-separated, and can add **PASSTHROUGH**
entries only: it can never admit a route as rewritten, never override a REFUSE,
and never disable the gate. Both are enforced twice. At load, an item that is
malformed or names a refused route — including a `HEAD` on a path whose `GET`
is refused — is a config problem: `config check` reports it and the entrypoint
exits 78. The error names the item's position and, when it parses, only
`METHOD path` — the env value itself never reaches stdout. At runtime, the tables answer before any extra, for `HEAD` too, so an
extra only ever reaches a pair no table lists. Use it for an operator route that
provably sends no user text anywhere.

Each item is **one exact `(method, path)` pair** — there is no prefix form, so
widening cannot open a tree by accident. That is also why it cannot re-open the
admin UI either: a mounted sub-app serves many paths under its prefix, and listing them
one by one is not a widening anyone should write. `gateway-admin config check
--routes` prints the effective table and every extra.

The nginx front door (`docs/plans/20260806-nginx-profile-tls.md`) denies the same
routes at the edge. That is defence in depth, not a substitute: the gate runs
inside the image, so it holds on the SSH-tunnel path and under compose too, where
there is no nginx.
