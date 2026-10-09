# Changelog

All notable changes to corp-llm-gateway are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [1.1.0]

### Added — `gateway-admin` bootstrap for a fixed team token

- **`token issue --value VALUE`** stores a value you choose instead of a random `ct_…` token
  (for a fixed team token such as the Local setup's). The value never appears in the command's
  output or logs: plain output has no `token:` line and `--json` omits it. It does appear in the
  process's argv, and a misspelled flag makes argparse echo it (see `docs/ops/admin-cli.md`).
  It must be 16-256 printable ASCII characters (0x21-0x7E) and must not start with `ct_`
  (reserved for generated tokens); otherwise it is a usage error (exit 2) that names the flag
  and the reason, never the value. Re-issuing the same value for the same user and team
  replaces its `expires_at`, `issued_at` and `scopes` (re-issuing without `--scopes` resets them
  to the default, none). A value held by another user or team, or a revoked value, is refused
  (exit 2); the check and the write are two steps, not one transaction (see
  `docs/ops/admin-cli.md`). The global `--token` (operator JWT) and RBAC are unchanged.
- **`gateway-admin db init`** applies the token and team-config schemas to the database
  `CORP_LLM_PG_DSN` names. Idempotent, RBAC-gated. Each schema file runs in its own transaction
  under a transaction-scoped advisory lock, so concurrent runs take turns, and it works through
  a transaction-mode pooler (PgBouncer). Like the gateway's pools it sends the TCP keepalive
  startup parameters, so PgBouncer needs them in `ignore_startup_parameters`
  (`docs/ops/runbook.md`), else it fails with `StartupParameterRejectedError`. `lock_timeout`
  bounds each lock wait: 60 s for another `db init`
  (`another db init holds the lock (LockNotAvailableError)`) and 1.5 s for each table lock
  (`LockNotAvailableError`). Lock waits can stall serving gateways' token lookups for about 3 s
  at most (two table waits), under the 5 s lookup timeout; on a busy database it fails fast with
  exit 2 and is safe to re-run. Work done once a lock is granted is not bounded and also blocks
  lookups: the OIDC column and index builds on an upgrade from a pre-OIDC schema, or a slow
  synchronous-replication `COMMIT` (see `docs/ops/admin-cli.md`). No DSN or no `postgres` extra exits 2 with a
  named message; a connection or SQL failure exits 2 with the error type only. The DSN is
  never printed.
- **`team create --if-absent`** exits 0 and changes nothing when the team exists (without the
  flag it still exits 2).

### Changed — `gateway-admin`

- **`token list` masks a chosen value fully, as `***`**; a generated `ct_` token still shows
  its first 8 chars.
- **`--ttl-days` is validated on every `token issue`**, random or `--value`: below 1, or past
  the year 9999, is a usage error (exit 2).
- **`team create` is one atomic insert**, so a concurrent `create` never overwrites an existing
  team's settings.

## [1.0.0] — GA (2026-10-09)

The first GA release — the **local-first detection cycle** (below) plus the **GA-readiness /
security & extensibility** build. Non-negotiable criterion: zero confirmed leak incidents in the
90 days post-GA. Release candidates v1.0.0-rc.1 to rc.6 were cut from 2026-07-10 to
2026-08-04.

### Security — litellm's logging surfaces, DEBUG and policy bodies

Found by the litellm guardrail adoption plan (`docs/security.md` §15, hazards 1-19). Hazards 17,
17b and 18 are **pre-existing on `release/1.0.x`** (same litellm 1.101.0). They are config-gated:
only a callback registered in litellm receives these surfaces, and the shipped configs register
none but the gateway's own.

- **`/v1/responses` logging payloads held the original input** (hazard 17). litellm snapshots the
  request into its logging object before any pre-call hook, so the log kwargs and every
  `StandardLoggingPayload`, success and failure, carried the text the gateway had just rewritten.
  The pre-call now hands the logging object the rewritten request, for every `input` shape.
- **A Stage 5 (DLP) refusal handed every callback's failure hook the original request** (hazard
  17b). The body snapshot's content keys are now rewritten before the scan. A Stage 0 refusal
  still hands over the original — it refuses before anything is rewritten; no callback of ours
  overrides that hook.
- **The `X-Corp-Auth` value reached litellm's logging surfaces** (hazard 18, invariant 4). On
  `/v1/chat/completions` litellm copies the request headers into `metadata.requester_metadata`,
  which the strip never visited: the log kwargs, every `StandardLoggingPayload`, the spend-log
  request (with `store_prompts_in_spend_logs`) and a litellm DEBUG line carried the token. And on
  every route a request refused with 401 handed the token to every callback's failure hook,
  because the strip ran after authentication. The pre-call now strips the token first, from that
  copy and from litellm's logging object too.
- **litellm DEBUG refuses to arm** (exit 70, `litellm_debug_logging` / `litellm_set_verbose`).
  litellm's DEBUG lines print the original request before any pre-call hook, and two of them
  ("Request Headers" and "[PROXY] returned data from litellm_pre_call_utils") print the raw corp
  token; the second prints the whole request, so the developer's BYOK `Authorization` too (a
  subscription OAuth token, invariant 3). The arm check is their only guard.
  `CORP_LLM_ALLOW_LITELLM_DEBUG=1` allows DEBUG outside prod, for tests only; in prod it is exit 78.
- **Also refused at arm:** an `apply_guardrail` anywhere in the guardrail's MRO (litellm would
  never run its pre-call), `scan_raw_request` / `run_in_parallel` (litellm would discard its
  rewrite), and a response compressor on litellm's app (`response_compressor`).
- **Request bodies naming policies are refused at the route gate.** An admitted rewritten request
  whose JSON body has a top-level `policies` key is 403 `E_ROUTE_BLOCKED`
  (`block_reason=route_gate_body_policies`), and one whose body is not `application/json` is 415
  (`route_gate_body_not_json`: litellm would read a form body, a `policies` field included) — both
  before litellm parses it. litellm applies a body's `policies` to that request with no
  attachment.
- **The 415 covers any body that is not UTF-8 JSON**: a `charset` other than `utf-8`/`utf8`, a
  BOM, UTF-16/32, or bytes that do not decode as UTF-8. The `policies` check reads bytes, and a
  UTF-16 body spells the key in bytes it never matched, so the gate served it. litellm 1.101.0
  turns such a body into an empty one (with a BOM, or bytes that are not UTF-8) or answers 400,
  so nothing leaked; stdlib `json.loads(bytes)` would decode every one of them.

### Fixed — chat-completions streaming came back with placeholders

- **OpenAI chat-completions streaming is restored.** The callback reversal passed litellm's
  `ModelResponseStream` chunks through untouched, so OpenAI-SDK streaming clients got the
  placeholders. Live on `release/1.0.x`. Tail chunks now carry the stream's `id` / `object` /
  `created` / `model` and `choices[].index`, and each choice's held text rides in the chunk with
  its `finish_reason` — pinned against the installed OpenAI SDK's stream accumulator.

### Changed — responses are restored outside litellm

- **The gateway's one response reversal is now an ASGI middleware** in front of litellm's app,
  inside the in-flight limiter (`route_gate/desanitize_middleware.py`). The guardrail callback no
  longer restores anything, so litellm and every callback, hook or log inside it sees placeholders
  only; the client gets its originals. It is keyed by the gateway's request ticket and holds its
  own mapping snapshot (released at the end of the response), so it no longer depends on Cache B.
  A restoration failure is a content-free 500 `E_INTERNAL` (or, once the stream started, a closed
  stream) plus `gateway_failure{component="desanitize"}`. A response with a `Content-Encoding`
  passes through unrestored. Measured overhead on the served stack (stub provider, p50 per
  flow, against 37c21f9): under 0.75 ms on every flow; within +10% on every SSE flow and every
  unary flow except chat unary (+13.4% in one of two batches, +6.4% in the other); run-to-run
  spread on the base alone was up to 1.2 ms.
- **One audit record per request, written when the response ends** (`route_gate/terminal_audit.py`),
  never by a litellm callback: `ok` at the final body; `failed` for a non-2xx response or a
  stream error, and `failed` + `E_INTERNAL` for a restoration failure; `cancelled` +
  `E_CLIENT_DISCONNECTED` when the client left, and the new `E_SERVER_SHUTDOWN` when the server
  cancelled the request (shutdown, pod drain). A failed write on the response path is retried
  once, by the close; a record the close decides (nothing was published) has one write and no
  retry; a lost one counts
  as `gateway_failure{component="desanitize"}` (`docs/audit-schema.md`, "The terminal record").
- **Token counts come from the response itself** (the JSON `usage`, the stream's usage events),
  not only from litellm's success log. A chat stream now always asks for its usage chunk
  (`stream_options.include_usage`); when the client did not ask, the gateway reads it and drops it.
- **The shipped litellm configs pin `general_settings.supported_db_objects: ["models"]`**
  (compose and Helm), so litellm never loads a policy or guardrail row from its database. Keep the
  pin in any config of your own (`docs/ops/configuration.md`).
- **`enforces_request_content = True`** on the guardrail, so litellm's `guardrails_only` pre-call
  walk cannot skip it. The guardrail stays a plain `CustomLogger`: litellm's `CustomGuardrail`
  base would re-open the policy-pipeline skip and the guardrail-name substitution
  (`docs/security.md` §15).

### Known limitations — the response boundary

- **Interleaved tool-call fragments.** A chat stream that interleaves the argument fragments of
  two tool calls (off-spec for OpenAI; the v1 providers stream calls in sequence) gets the
  placeholders back in those arguments, never the originals, and the stream does not fail.
- **Responses `sequence_number` is renumbered** on a restored `/v1/responses` stream (held text
  and tails would otherwise repeat or skip numbers), so the numbers a client sees can differ from
  the provider's.
- **Free-text fields not yet covered** — chat `messages[].name` and `prediction.content`,
  Responses `prompt.variables` and Anthropic `citations[]` egress unrewritten (citations also come
  back with placeholders): the open rows in `docs/security.md` §2, "Not sanitized / deferred".
- **litellm's config table (hazard 14c) and the provider key in its log kwargs (hazard 19b)**
  are characterised, not closed (`docs/security.md` §11 (j), §15).

### Added — content-free `guardrail_information` in litellm's logging payload

- **The pre-call writes one `guardrail_information` entry** (`corp-llm-sanitizer`, `pre_call`)
  into litellm's `StandardLoggingPayload` with litellm's own writer, so every `litellm.callbacks`
  logger sees the guardrail's outcome: `guardrail_status` derived from `block_reason`, timing,
  `redaction_count`, `finding_label_counts` and `block_reason` when set. Content-free and
  allow-listed (`assert_guardrail_information_allowed`); the NEVER-fields gate now checks such
  entries wherever a record carries them (`docs/audit-schema.md`).
- **New log event `litellm_guardrail_information_failed request_id=… error=<type>`** when the
  entry is not written; the request and its audit record go on (`docs/security.md` §8,
  `guardrailInformationWriteFailed`).
- **`gateway_failure{component="audit"}` widened**: it also counts that event, next to a litellm
  log event with no request state (`docs/ops/runbook.md`).

### Added — HTTPS front door for the compose stack (nginx, opt-in)

- **`nginx` / `nginx-ports` profiles** in `compose/docker-compose.yml`, off unless the server's
  `.env` sets `COMPOSE_PROFILES` — the one switch a deploy and a reboot both read. `nginx` routes
  `gateway.<GATEWAY_DOMAIN>` / `langfuse.<GATEWAY_DOMAIN>` by name; `nginx-ports` is the no-DNS
  fallback on two ports. Published on `NGINX_BIND_ADDR`, loopback by default. With no profile the
  stack is unchanged.
- **HTTPS only, two TLS modes, no default.** `NGINX_TLS_MODE=terminate` (nginx serves a
  bring-your-own certificate from `compose/nginx/certs/`, TLS 1.2+, HSTS) or `behind-proxy` (the
  admins' load balancer terminates; every peer outside `NGINX_TRUSTED_PROXIES` gets no response).
  A validating entrypoint refuses a bad key with one log line and exit 64-69.
- **Exact-path allow-list**: `POST /v1/messages`, `/v1/chat/completions`, `/v1/responses`,
  `GET /v1/models`, `GET /healthz/live`, `POST /internal/issue-token`; everything else is 404 at
  the edge — defence in depth over the route gate, and nginx admits nothing the gate refuses.
- **Per-token edge limits** (`NGINX_TOKEN_RATE`, `NGINX_TOKEN_BURST`, `NGINX_TOKEN_CONN`,
  `NGINX_ISSUE_RATE`): 429 `E_RATE_LIMITED` before the gateway (`docs/ops/capacity.md`, "Edge
  limits").
- **Deploy:** `deploy.sh` refuses a `.env` that enables both profiles, fails at once on a dead
  front door, and never syncs `nginx/certs/`; certificates are installed on the server
  (`docs/ops/deploy-handoff.md`, step 4a). `scripts/deploy/make-selfsigned-certs.sh` makes a
  throwaway CA + leaf for pilots and tests.
- **Logs:** a JSON access log with no credential header, body or query string; `error_log` at
  `crit`, since nginx appends the request line at `error` and `warn`.

### Added — Keycloak-issued corp tokens, gateway-owned capacity cap

- **`POST /internal/issue-token`** — trades a Keycloak access token (RS256 only; `iss`/`aud`/`azp`
  required; the operator audience refused; single-flight JWKS) for an `X-Corp-Auth` token under a
  per-`(iss, sub)` policy: at most `MAX_ACTIVE` active tokens (oldest rotated out), a minimum
  interval, `jti` replay refused, a Postgres advisory lock. The route is header-only, bounded (body,
  time, in-flight, rate) and terminates locally. `install.sh` runs the real device flow; Helm gets
  `issuance.*` values and Keycloak egress in the NetworkPolicy; compose gets
  `docker-compose.issuance.yml` (`deploy.sh --issuance`). Issuance needs Postgres and a token-table
  migration (oidc columns + a unique `jti` index).
- **In-flight cap** (`route_gate/inflight.py`) — request N+1 gets 429 `E_CAPACITY` before litellm and
  before the sanitizer. On a client disconnect the gateway cancels, waits a bounded grace, sweeps the
  request's tasks, writes one `cancelled` audit record and releases the slot once. Both litellm
  configs set `cancel_on_disconnect: true`.
- **New settings:** `CORP_GATEWAY_ISSUE_OIDC_{ISSUER,AUDIENCE,CLIENT_ID,JWKS_URL,TEAM_CLAIM,USER_CLAIM,TEAM_MAP}`,
  `CORP_GATEWAY_ISSUE_{TOKEN_TTL_DAYS,MAX_ACTIVE,MIN_INTERVAL_SECONDS,MAX_INFLIGHT,RATE_PER_MINUTE,STORE_TIMEOUT_SECONDS}`,
  `CORP_LLM_{MAX_INFLIGHT,MAX_DRAINING,MAX_DRAINING_BYTES,BODY_READ_SECONDS,CANCEL_GRACE_SECONDS}`.
- **CI** — a new `integration-container` job (digest-tagged image, route-gate container suite); the
  `test` job runs on Python 3.14 only, with a Postgres service. `pymorphy3` is now in the `ner` extra.

### Changed — litellm management surface refused; Mode A is a test posture

- **462 admin/auth/spend/UI/public rows + 8 health rows now return 403 `E_ROUTE_BLOCKED`** (`/key/*`,
  `/policies`, `/guardrails/*`, `/login`, `/sso/*`, `GET /`, `/health`, `/health/drain`, …). The
  table is 26 PASSTHROUGH / 879 REFUSE / 8 REWRITTEN; `/health/liveliness` and `/health/readiness`
  stay. `CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH` can no longer re-admit a refused row — boot exits 78.
- **Mode A (virtual keys) is a test posture only.** `deploy.sh` defaults to `--mode oauth`; hosts
  still on Mode A must pass `--mode virtual-keys` on every run.
- **The gateway owns `litellm_call_id`**: a client-sent `x-litellm-call-id` header is stripped at
  the gate.
- **Boot also exits 78** on issuance without Postgres, partial issuance config, out-of-range
  capacity settings, a wrong token schema, or (issuance on) a PgBouncer that rejects the keepalive
  startup parameters. **PgBouncer operators:** add
  `ignore_startup_parameters = tcp_keepalives_idle,tcp_keepalives_interval,tcp_keepalives_count`,
  or every LLM request answers 503 (`docs/ops/configuration.md`).

### Security

- `docs/security.md` §14 gains "The issuance route: gateway-owned, terminates locally, bounded",
  "The management surface is refused" and "The in-flight cap: after the verdict, gateway-owned"
  (the cap is not authorization). Properties the cap guarantees: tasks shared between requests
  survive a client disconnect; no slot is held before the request body is complete; a cancelled
  request never retains user content.

### Added — production compose deploy target (`compose/`)

- **A second production deploy target**, for hosts without Kubernetes, alongside the Helm chart:
  the data plane (`litellm` + `redis` + `postgres`), **self-hosted Langfuse v3** (web/worker,
  ClickHouse, MinIO, its own capped Redis — no host port published by any of them) and the
  **audit pipeline** (`vector`, reading a read-only bind of the container log directory rather
  than the docker socket, with the `never_fields_gate` / `audit_only` transforms byte-identical
  to the Helm chart's configmap). Every secret comes from `.env`; the four keys with no default make
  `docker compose up` refuse to start rather than boot half-configured.
- **Two mutually exclusive auth modes.** Mode A — corp API keys, developers hold a LiteLLM
  virtual key (per-person revocation + spend) (superseded: virtual keys are a test-only posture
  since the management surface was refused — see above). Mode B (`docker-compose.oauth.yml`) — the
  developer's own Anthropic subscription OAuth bearer is forwarded upstream and **no corp
  `ANTHROPIC_API_KEY` exists at all**; it serves `claude-*` only, which is a binding control
  rather than a simplification (litellm resolves the deployment after the hook runs, so an
  Anthropic-only routing table is the only proof of where a request lands). A master key and the
  bridge cannot coexist — `build_guardrail()` refuses to boot and names the cause.
- **Server bootstrap + deploy scripts** — `scripts/deploy/bootstrap-server.sh` (idempotent day-0
  host prep + optional systemd unit) and `scripts/deploy/deploy.sh` (`up`/`down`/`restart`/
  `logs`/`status`, `--mode oauth`, `--dry-run`, `--yes`). The local `.env` is never uploaded and
  the server's is never read or overwritten.
- **`docker-compose.build.yml`** — build the current branch instead of the published tag, pinned
  to the `ru-en` NER profile (the `base` default ships no EN model and would 503 every request
  under `CORP_LLM_REQUIRE_NER=1`).

### Added — corp NER service (optional, off by default)

- **`CorpNerDetector` + `corp_ner/` client** — a remote NER detector appended to the local-first
  cascade, off unless `CORP_NER_ENABLED=1`, and requiring `CORP_NER_ENDPOINT` when on (enabled
  without an endpoint is a boot refusal, not a silent skip). Tuning:
  `CORP_NER_TIMEOUT_S` / `CORP_NER_MAX_TEXTS` / `CORP_NER_MAX_INPUT_CHARS` / `CORP_NER_CA_BUNDLE`.
- **Network-backed detectors are excluded from `CODE` segments** — shipping source to an external
  service is the leak this prevents. All *local* detectors keep scanning code; the exclusion is
  local-vs-network, not code-safe-vs-not.
- **The NER call carries raw user content**, so its TLS verification can never be disabled; an
  internal CA goes through `CORP_NER_CA_BUNDLE`.
- A readiness check for the service, and `gateway-admin config check` coverage of the new keys.

### Added — detection

- **`BANK_CARD` Luhn rule** — IIN-plausible, Luhn-valid PANs (13–19 digits, tolerating space and
  hyphen grouping), with length and offset enforced before Luhn is spent so that a longer
  incidental Luhn hit cannot evict a real PAN.

### Changed — Cache A key boundary

- The Cache A key now folds a **detector-policy fingerprint** as well as a coverage-version
  constant, so entries produced under different detector coverage (corp NER on/off, a widened
  profile, a different NER engine capability, a different gazetteer lemmatizer) can never be
  served to each other. Flipping either network toggle needs **no cache flush**.

### Docs

- `docs/ops/deployment-modes.md` + `.ru.md` — the mode matrix, both network toggles, every
  failure mode.
- `docs/ops/deploy-handoff.md` + `.ru.md` — the condensed step-by-step for whoever runs the
  deploy.
- `compose/README.md` + `.ru.md` — the full stack reference (routing, virtual keys, why BYOK is
  not available here, Langfuse, the audit pipeline and its recovery procedures, TLS to the corp
  vLLM, environment posture).
- README (EN/RU) now documents the deployment targets, the compose stack and the corp NER
  toggle; the RU README caught up on the local-compose section, the `replace.md` matching
  semantics and the licence section.

### Known limitations of the compose target

- **No TLS without a profile** — with `COMPOSE_PROFILES` unset the only published port is
  `127.0.0.1:4000` (the front door above).
- **In Mode B litellm's management endpoints are unauthenticated** (`/key/*`, `/model/*`,
  `/user/*`, the UI) — its proxy auth is skipped without a master key, which is what the mode
  requires. The LLM routes stay gated by `X-Corp-Auth`. Must be closed at nginx before the port
  leaves loopback.
- **Audit is buffered but not fail-closed** — a documented deviation from the `vectorBufferFull`
  default in `docs/security.md` §8. Durability is bounded by docker log rotation.
- **No untrusted `docker run` on the host** — Vector's container-label filter is a
  misconfiguration guard, not a security boundary (`docs/security.md` §8.2).

### Added — Local mode (oracle on/off switch + compose quickstart)
- **`CORP_LLM_ORACLE_ENABLED`** — on/off switch for the LLM oracle (corp vLLM). Off = local-first
  cascade only (replace.md, regex+checksum, dual-NER, gazetteer, splitter); no oracle call ever
  attempted, `CORP_LLM_ENDPOINT` no longer required. Refuses to boot as a no-op sanitizer if
  `CORP_LLM_LOCAL_FIRST` is also off.
- **`CORP_LLM_DEV_TEAM_TOKEN`** — dev-only seam that seeds a working `X-Corp-Auth` token for team
  `local-dev` in the in-memory token store; ignored (with a warning) when a Postgres DSN or
  `CORP_ENV=prod` is set.
- **`examples/compose/`** — docker-compose quickstart running the published GHCR image as a local
  sanitizing proxy in front of Anthropic/OpenAI with the oracle off; documents the BYOK trade-off
  of native anthropic/openai routing (gateway-side shared key, not per-developer passthrough).
  (first published image: v1.0.0-rc.5)

### Added — GA-readiness, security & extensibility
- **Plugin / profile layer** — declarative `profiles/` bundles (country / division / regime),
  monotone-tightening `PolicyKnobs.merge`, hash-sealed integrity, SHA-256 cross-jurisdiction cache
  isolation, `TeamConfig.profile_ids` selection.
- **Extension seams** — keyed `extensions/` + `providers/` registries (fail-closed register +
  api-version gate; v1 anthropic / openai / corp-vllm, v2 gated), `DETECTOR_REGISTRY`, pluggable
  metrics exporter, `bootstrap.build_guardrail()` composition root; contributor guide
  `docs/extending.md`.
- **Security hardening** — 11 repro-first leak-surface fixes (oversize + NER fail-closed, OpenAI
  `tool_calls` + streaming, segmenter coverage, `X-Corp-Auth` stripping across all header locations,
  dev-proxy host-pin, error-body, TLS/RBAC, recursive NEVER-gate, RS256 + aud/iss).
- **Ops** — real `gateway-admin` (team / token / extensions / config check), production Helm chart
  (guardrail image + callback, config-check initContainer, NetworkPolicy, CoreDNS sinkhole), served
  healthz, ops docs.
- **`replace.md`** — `=` is now the canonical rule separator (legacy `→` still parsed).
- **Release tooling** — shared `scripts/release/{gates,ship,cut-rc}.sh` delivery scripts,
  `github-release` workflow (auto GitHub Release on `v*` tags, `--prerelease` for rc),
  `docs/ops/release.md`, least-privilege `dco.yml` permissions (closes CodeQL alert #1).
- **ChatGPT Codex Responses profile** (opt-in, `CORP_LLM_FORWARD_CHATGPT_AUTH`) — OpenAI
  Responses API sanitize/desanitize coverage (`input`/`instructions`, `custom_tool_call`,
  `reasoning.summary[]`, `local_shell_call`, `mcp_call`, function-call arguments, streaming
  events including a placeholder split across SSE chunks) and a header bridge that forwards
  the developer's live ChatGPT subscription OAuth to the Codex backend instead of a static
  provider key. Wired into `bootstrap.build_guardrail()`, so the flag is honored in every
  deployment (Helm/k8s included), not just the docker-compose demo overlay. See
  `docs/chatgpt-codex.md`.

### Changed — `replace.md` rule-matching semantics (behavior change for every existing dictionary)

- **Case-insensitive substring matching (previously case-sensitive).** A `replace.md` rule now
  matches as a plain case-insensitive substring, for a single-word source or a multi-word
  phrase alike — a rule `Acme = [X]` now also matches `acme` and `ACME`, not just `Acme`. This
  is the real widening on upgrade: review existing dictionaries for short or common sources
  that also occur as ordinary lowercase/uppercase text. See `docs/replace-md-authoring.md`.
- **Rules and findings now compete in one longest-span-wins pool.** `replace.md` rule matches
  and detector/NER/oracle findings are selected from a single candidate pool ordered by span
  length descending — whichever span is longer wins, regardless of source; a rule wins a tie
  only when its span is identical to a finding's span. This guarantees a shorter rule can never
  silently discard a longer overlapping finding (and its Cache-B mapping).

### Changed — other flag-off behavior changes

- **Unrecognized Anthropic content-block types are now scanned instead of passed through
  unchanged.** A block type this gateway doesn't recognize (e.g. a new
  `web_fetch_tool_result` shape) used to egress as-is; it's now walked as a generic JSON
  value tree so every string leaf is sanitized — this widens redaction coverage on unmodified
  Anthropic traffic. See `docs/security.md` ("Not sanitized / deferred").
- **A request carrying both `messages` and `input` is now rejected (HTTP 422).** Previously
  the ambiguous shape forwarded whichever field the request-item walker happened to pick,
  silently bypassing sanitize/Stage 0/Stage 5 for the other field; the gateway now fails
  closed instead of guessing which one is real.

### Local-first detection cycle (2026-06-30)

> Plan: `docs/plans/20260630-bilingual-local-first-detection.md`
> ADR: `docs/adr/ADR-003-ner-orchestration.md` — hand-roll dual-NER (Natasha RU + spaCy EN)
> over Presidio-as-orchestrator and DeepPavlov/BERT (rejected: install-time kill-shot on CPU,
> 1.44 GB model, no wheels for torch<1.14 on modern platforms).
> Compliance delta: ✅ 2 / 🟡 8 / ❌ 5 → **✅ 11 / 🟡 3 / ⚪ 1** vs the 15 ИБ requirements.

### Added — Detection (Track 1, tasks DP-0…DP-9)

- `RegexChecksumDetector` (`detectors/regex_checksum.py`) — algorithm-validated ИНН (10/12),
  КПП, ОГРН (13/15), БИК, СНИЛС, р/счёт, plus JWT, PEM private key, `sk-`/`AKIA`/`ghp_`/
  generic `password=`, IPv4/6 (via `ipaddress`), CIDR, internal hostnames
  (`*.corp.internal/.lan/.local`), DB-URLs. Near-zero false positives via checksum. (DP-1)
- Bilingual `DualNerDetector` (`detectors/dual_ner.py`) — Natasha/Slovnet RU + spaCy
  `en_core_web_md` EN, run-both-union with de-overlap by longest span and provenance labels;
  covers ФИО, organisations, addresses in mixed-language requests. (DP-2)
- Local-first detection pass merged with oracle in `sanitizer/engine.py` — additive; oracle
  remains unconditionally on at DP-3, narrowed at DP-4. (DP-3)
- Lemma-gazetteer (`rules/gazetteer.py`) with built-in word-lists for products/code-names
  (`rules/defaults/products.txt`), regulated ПОД-ФТ/AML-CFT terms (`rules/defaults/regulated.txt`),
  and confidentiality markings (`rules/defaults/markings.txt`). Lemma-matched so inflected forms
  (`легализации`) hit. Oracle invoked only on a gazetteer hit. (DP-4)
- Code-aware segmenter + identifier splitter (`sanitizer/segmenter/`) — splits camel/snake
  identifiers (`CompanynameabcService` → `Companynameabc`) and scans segments against the
  gazetteer. (DP-5)
- Stage 0 pre-egress payload classifier (`payload/classifier.py`) — `.env`, kubeconfig,
  nginx.conf, log-dump/stack-trace signatures → HTTP 422 `block_reason`; upstream never called.
  `block_reason` is a CONDITIONAL audit field, carried to Langfuse. (DP-6)
- Stage 5 DLP egress guard (`sanitizer/dlp_guard.py`) — independent second-layer re-scan of the
  sanitized outbound payload for canary strings and high-confidence secrets; blocks any survivor
  with HTTP 422. (DP-7)
- Test-data allowlist (`sanitizer/allowlist.py`) — deterministic exemption for test fixtures;
  designed so it cannot suppress actual secrets. (DP-8)
- NER imports are lazy; Natasha + spaCy in `[ner]` optional extra. Python 3.14 degrades
  gracefully (no NER wheels); authoritative test run on Python 3.12 (875 passed). (DP-2, DP-9)
- Thread-offload of local NER off the async event loop (`asyncio.get_event_loop().run_in_executor`)
  to avoid blocking LiteLLM's callback coroutine. (DP-9)
- Demo LiteLLM image baked with `[ner]` extra — bilingual NER live in the demo stack.

### Added — Compliance (Track 2, tasks CP-1…CP-4)

- `PostgresTokenStore` (`tokens/postgres_store.py`) — asyncpg-backed persistent token store;
  `make_auth_middleware()` selects it when `CORP_LLM_PG_DSN` is set; contract tests
  parametrised over in-memory + Postgres backends. (CP-1)
- `gateway:operator` RBAC gate on admin CLI — `verify_operator()` in `auth/rbac.py` checks
  JWT claim via PyJWT; `_enforce_rbac()` called at each `gateway-admin` mutating subcommand;
  failure → stderr + exit code 2. (CP-2)
- SIEM sink wired in Vector configmap (HTTP sink under `audit.sinks.siem.enabled`, inherits
  NEVER-VRL gate). Helm alerts `AuditVectorDropHigh` + `LeakAttemptDetected` in
  `helm/.../templates/siem-alerts.yaml` with CI render asserts. Endpoint remains placeholder
  pending open Q#3. (CP-3)
- `NetworkPolicy` + CoreDNS sinkhole enabled in `helm/.../values-prod.yaml`; egress constrained
  to upstream + corp CIDRs. (CP-4)

### Fixed

- Audit for Stage-0/Stage-5 blocks now emitted inline via `async_log_failure_event` (idempotent);
  `block_reason` appears in all audit sinks including Langfuse.
- Pre_call rejections (auth failure, bad request, corp-LLM-down) all audited inline.
- Dev-proxy upstream URL now rebuilt with `urlunsplit` — scheme+netloc pinned from config,
  client target confined to path+query; closes CodeQL `py/full-ssrf` alert #2 (critical).
- Helm chart now projects the litellm callback shim (`configMap.items`) into the mounted
  `/etc/litellm` dir alongside `config.yaml`, so a cluster deploy of the published GHCR image
  boots instead of failing with `ImportError: Could not import guardrail` (litellm resolves
  `callbacks:` as a file path, not a package import); render-tested in `tests/helm/`.

---

## [0.0.2] — v1 sanitization core + ops (2026-05-07, plan rev 7)

> Plan: `docs/plans/20260507-external-sanitizer-gateway-v1.md` (milestones M0–M8).
> Milestones M1–M6 + M8 code-complete. M0 provisioning, M5 cluster enforcement,
> rollout phases, and sign-offs remain (infra- and process-gated).

### Added

**M0 — Foundations**

- Repo scaffold: `corp_llm_gateway` package, `pyproject.toml` entry points, pre-commit hooks,
  CI skeleton.
- Helm chart (`helm/corp-llm-gateway/`) — Deployment (litellm + vector sidecar), Service,
  Ingress, ConfigMap, NetworkPolicy, CoreDNS sinkhole templates.
- Corp-LLM (vLLM) contract closed; `CorpLlmClient` (`corp_llm/`) speaking
  `/v1/chat/completions`.

**M1 — Sanitization core**

- `PIIDetector` ABC + `ShadowDetector` registry (`detectors/`); ADR-001 interface-registry
  pattern.
- `MappingStore` (`storage/`) with in-memory and Redis backends; contract-test parametrisation.
- `CorpLlmSanitizer` with original three-tier strategy: `FunctionCallStrategy → JsonStrategy →
  RegexStrategy` (first to succeed wins; regex is the floor).
- Length-descending placeholder substitution invariant (#5, M1-9).
- `StreamingDesanitizer` (`sanitizer/`) with rolling SSE-aware buffer for Anthropic and OpenAI
  streaming.
- `RequestPlaceholderAllocator` — per-request bijection preventing cross-segment placeholder
  collision.
- Content-block walker: sanitizes `tool_use.input`, `tool_result`, `document`, `system` blocks;
  streaming `tool_use` desanitize; `thinking` blocks passed through by design (Anthropic-signed).
- `litellm_hook.py` `CorpLlmGuardrail` — `async_pre_call_hook`, `async_post_call_success_hook`,
  streaming iterator hook, `async_log_*` audit callbacks. (M1-7)
- `replace.md` parser + 5-minute cached file loader (M1-10, M1-15).
- Payload size threshold + gzip + per-team quota helpers (`payload/`). (M1-11)

**M2 — Auth & multi-tenancy**

- `tokens/schema.sql` + `AuthMiddleware` with 60 s revocation cache.
- `TokenIssuer` with pluggable OIDC verifier (M2-3).
- `TeamConfigStore` with per-team retention config + fail-policy overrides (M2-4).
- `gateway-admin` CLI skeleton: `team create/update/delete`, `token issue/revoke` (M2-5).
- BYOK `Authorization: Bearer` passthrough invariant (#3).

**M3 — Audit pipeline**

- `AuditEvent` schema with ALWAYS / CONDITIONAL / NEVER field tiers; `docs/audit-schema.md`.
- Structured audit logger + NEVER-fields gate (`audit/invariants.py`); Vector VRL
  defense-in-depth for the same field set.
- Langfuse sink + e2e integration test + CI job (M3-4).
- S3 lifecycle-policy generator from team retention config (M3-7).
- `finding_label_counts` + distinct-secret counts in audit events.

**M4 — Failure modes & health**

- `/healthz/live`, `/healthz/ready`, `/healthz/sanitization` deep-check endpoints.
- Fail-policy matrix (M4) as source of truth; 503 `E_CORP_LLM_DOWN` + fail-closed paths;
  no ad-hoc fail-open paths in code.

**M5 — Egress / CoreDNS**

- Helm templates for `NetworkPolicy` egress lockdown + CoreDNS sinkhole.
- Corp-LLM TLS verified via `CORP_LLM_CA_BUNDLE` (Corp CA bundle; `SSL_CERT_FILE` for
  LiteLLM's aiohttp path).

**M6 — Onboarding**

- `scripts/install.sh` — bash/zsh/fish, macOS/Linux, Keycloak device-flow OAuth, idempotent
  rc-block updater, round-trip smoke test.
- `corp-llm-gateway status` CLI (dev diagnostics — token present, gateway live, version,
  update check).
- `corp-llm-gateway-proxy` localhost header-injecting proxy (Pattern 3, re-reads token file
  per request).
- Auto-update check + CI release job (M6-6…M6-8).

**M8 — Documentation**

- `docs/ops/runbook.md`, `docs/ops/capacity.md` (sizing alpha → GA at 1000 devs / 50 RPS).
- `docs/replace-md-authoring.md`, `docs/rbac-matrix.md`, ADR-001 (interface-registry).
- `docs/security.md` — sanitization coverage, audit-pipeline guarantees, known config gaps.
- TOML property-file fallback for all env vars (`config.py`, `config.example.toml`).
- Internal git mirror created; open Q#1 closed.

### Fixed

- Anthropic content-block leak — content walker now sanitizes block lists, `tool_result`,
  `system`.
- Cross-segment placeholder collision — `RequestPlaceholderAllocator` bijection.
- User-typed literal placeholder collision prevented (case-4 hardening).
- SSE-aware streaming desanitization for both Anthropic and OpenAI wire formats.
- Audit attribution keyed on `litellm_call_id`; audit records retain real identity +
  `redaction_count` across pre/post handoff.
- Production Vector configmap: duplicate `transforms:` key fixed; NEVER-gate complete;
  `audit_only` path added.
- Corp-LLM fail-closed 503 on `E_CORP_LLM_DOWN`; correct audit attribution restored.

---

## [0.0.1] — initial scaffold (2026-05-07)

### Added

- Repo scaffold, CI skeleton, `pyproject.toml` with CLI entry points
  (`corp-llm-gateway`, `corp-llm-gateway-proxy`, `gateway-admin`).
- `CorpLlmAuthProvider` pluggable auth interface (`auth/`) — Noop default; Bearer/mTLS/OIDC
  stubs raise `NotImplementedError` naming the blocking task.
- `PIIDetector` ABC + `ShadowDetector` stub.
