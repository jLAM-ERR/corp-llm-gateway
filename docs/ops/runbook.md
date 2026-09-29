# Operations runbook

Plan ref: M8-2.

## Daily operations

### Deploying a new version

0. Run the pre-push gates: `bash scripts/release/gates.sh`. It builds both image
   profiles and — since the route gate landed — runs the built image with **no
   litellm config** and requires exit **78**, which proves the fail-closed
   startup path on the real image. No launch path anywhere runs the `litellm`
   CLI any more; the only serve command is `python -m corp_llm_gateway.serve`
   (`release.md`).
1. Tag the release: `git tag v0.x.y && git push origin v0.x.y`.
2. GitHub Actions builds and publishes the gateway image on the tag (`.github/workflows/build-image.yml`). The wheel and Helm chart are built locally for now (those CI jobs are not yet ported).
3. Apply to staging: `helm upgrade --install gw helm/corp-llm-gateway -f values-staging.yaml --version v0.x.y`.
4. Wait for `/healthz/ready` green on all 3 pods.
5. Run the deep-check: `curl https://gateway-staging.corp.lan/healthz/sanitization`.
6. Promote to prod with the same command against `values-prod.yaml`.

### Rolling back

```
helm rollback gw <revision>
```

Revisions list: `helm history gw`. Default Helm keeps the last 10.

### Pinning the LiteLLM version

`values.yaml: litellm.versionPin`. Bump only after staging upgrade gate passes (per the plan's M0-7 task).

## Incident playbook

The fail-policy matrix in the plan (M4) is the source of truth for what "should" happen on each component failure. When reality disagrees, that's the bug.

Metrics note: the alert series `gateway_failure{component}` and `corp_llm_gateway_blocked_requests_total{block_reason}` are emitted by the metrics module and scraped via the ServiceMonitor — with `CORP_METRICS_EXPORTER=prometheus` (default `noop` emits nothing). The same conditions also surface in the gateway's structured logs — grep `error_code=` (e.g. `E_CORP_LLM_DOWN`, `E_NER_UNAVAILABLE`, `E_OVERSIZE_BLOCKED`, `E_DLP_BLOCKED`, `E_INTERNAL`, `E_ROUTE_BLOCKED`, `E_ROUTE_GATE_UNARMED`, `E_ROUTE_GATE_ERROR`, `E_CAPACITY`, `E_BODY_TIMEOUT`, `E_STORE_UNAVAILABLE`, `E_PROFILE_UNAVAILABLE`) and `block_reason=` (`litellm_pre_call_blocked` / `litellm_egress_blocked` / `route_gate_*`).

### Corp-LLM unreachable

Symptom: `gateway_failure{component="corp_llm"}` rises; requests return 503 with `error_code="E_CORP_LLM_DOWN"`.

Behavior: fail-closed (per matrix). Gateway is healthy; the dependency isn't.

Action:
1. Confirm corp-LLM is actually down (curl its endpoint from a gateway pod).
2. If yes: page corp-LLM team. The gateway will recover automatically when corp-LLM recovers.
3. If no: investigate gateway-side connectivity (NetworkPolicy, DNS).

**Not the same as local mode.** `CORP_LLM_ORACLE_ENABLED=0` (solo/local-mode
gateways, `examples/compose/`) is a deliberate config choice, not this
incident — no oracle call is ever attempted, so there's no `E_CORP_LLM_DOWN`
to page on. Local-first cascade findings (regex+checksum, dual-NER,
gazetteer, splitter) still apply; only the oracle's refinement pass is
absent. Don't page corp-LLM for a pod that was never configured to call it —
check `CORP_LLM_ORACLE_ENABLED` first if `E_CORP_LLM_DOWN` is unexpectedly
absent from an otherwise-live gateway's logs.

### Detection cascade degraded

There is no separate pre-pass Deployment. Detection runs **in-process** inside
the gateway pod (local-first cascade — regex+checksum, dual-NER, gazetteer — per
ADR-003); the corp-LLM oracle is only a conditional fallback. Two failure modes:

- **NER model absent/self-disabled.** With `CORP_LLM_REQUIRE_NER=1` (prod) this
  fails **closed**: requests return 503 `E_NER_UNAVAILABLE` (not a silent slow
  path). `/healthz/ready` also goes red on the NER probe. Fix the NER stack (the
  `ner` extra + model wheels) and the pod recovers. With the flag off (dev) NER
  degrades silently to no findings — do not run prod that way.
- **Oracle (corp-LLM) unreachable.** Surfaces as `E_CORP_LLM_DOWN` — see the
  section above.

Action:
1. Add detection capacity by scaling the **gateway** Deployment (it runs
   detection in-process), not a pre-pass pod: `kubectl scale -n corp-llm-gateway deploy/gw-corp-llm-gateway --replicas=N`, or enable/raise the HPA (`autoscaling` in values.yaml).
2. Investigate the pod (OOM? NER model load failure? unusually large payload —
   the M1-11 size threshold / `CORP_LLM_OVERSIZE_POLICY` governs those).

### Redis cluster down

Symptom: requests return 503 with `error_code="E_REDIS_DOWN"`.

Behavior: fail-closed (per matrix). No mappings = no de-sanitization = unsafe to serve.

Action:
1. `kubectl -n redis get pods` — at least 2/3 should be up. If 1 down: cluster is fine; transient.
2. If all down or split-brain: failover via Redis sentinel.

### Vector buffer at 50% (alert)

Symptom: SIEM alert "vector_buffer_50pct".

Behavior: **audit loss risk, NOT request blocking.** The M4 matrix lists
`vectorBufferFull` as fail-closed, but no deployed topology can enforce that
today: `CORP_AUDIT_SINK` is unset, so the gateway's only audit action is writing
a line to its own stdout (`audit/factory.py` default → `StdoutSink`), which
always succeeds. Vector reads that log file afterwards, from a different
container. There is no signal path from Vector's buffer state back into
`pre_call`/`post_call`, so a stalled audit path **cannot** return 503 — requests
keep egressing. See `compose/README.md` "Audit buffering is not fail-closed".

Action:
1. Check downstream sinks. Likely Langfuse or SIEM is down/slow.
2. If a single sink is down: the others continue. Pin which one via Vector metrics.
3. If the buffer fills, Vector back-pressures and stops reading; records stay in
   the container's log files. What actually bounds durability is **log
   retention**, not the buffer — once docker rotates a file past
   `LITELLM_LOG_MAX_FILE`, those audit records are gone permanently. Size
   `LITELLM_LOG_MAX_SIZE` × `LITELLM_LOG_MAX_FILE` for the longest outage you
   intend to survive.
4. `docker compose logs vector | grep "Events dropped"` — a wrong or rotated
   `CORP_LANGFUSE_*` key gives 401, which Vector does **not** retry. That is
   silent audit loss and needs the key fixed, not more buffer.

### Unexpected internal error (F8 safety net)

Symptom: `gateway_failure{component="internal"}` rises; requests return 500 with `error_code="E_INTERNAL"` and no other detail.

Behavior: fail-closed (per matrix). This is the catch-all for an exception the gateway didn't anticipate (a DB error, a bug, an audit-sink outage) — `pre_call` maps it to this opaque response rather than ever echoing the exception text to the client, the log, or the audit record; `litellm_pre_call_unexpected_error` logs the exception TYPE only, never its message. A failure restoring a response (the ASGI desanitiser) answers the same 500 `E_INTERNAL` (or closes a stream already under way) and logs `gateway_desanitize_failed request_id=… phase=… error=<type>`, counted as `gateway_failure{component="desanitize"}`.

Action:
1. Check the gateway pod logs for the matching `*_unexpected_error` line and its `exc_type=` — that names the exception class without leaking its message.
2. If `exc_type` points at a known dependency (Postgres, Redis, the audit sink), treat it as that component's own incident instead — this path is the safety net, not the root cause.
3. A request already blocked/failed by a specific component (e.g. `E_DLP_BLOCKED`) does NOT also count as `internal` — the wrapper skips the internal counter when a component-specific failure was already recorded for that request.
4. An upstream provider/transport failure mid-stream (e.g. `httpx.RemoteProtocolError`) counts as neither `internal` nor `desanitize`: the ASGI desanitiser sends the tails it restored so far, re-raises litellm's own exception unchanged, and the request's audit record is `failed`. `internal` rising means a bug in the gateway's own pre-call code, not a downstream provider outage and not a duplicate of a component-specific block; a restoration failure is `desanitize` (next section).

### `gateway_failure{component="desanitize"}` rises

Symptom: `gateway_failure{component="desanitize"}` counts up. The gateway log has one of:

- `gateway_desanitize_failed request_id=… phase=before_start|after_start error=<type>` — a response could not be restored;
- `gateway_terminal_audit_lost request_id=… outcome=… error=<type>` or `terminal_audit_publish_failed request_id=… error=<type>` — a request's audit record could not be written, retry included;
- `terminal_audit_drain_incomplete pending=<n>` — at shutdown, records were still being written when the cancel grace ran out.

Behavior: fail-closed, content-free. A restoration failure before the response started answers 500 `E_INTERNAL`; after it started, the client gets the events restored so far and the stream is closed, which also closes the upstream. litellm never sees an original either way, and the log lines carry the request id, the phase or outcome and the exception type only. The request's audit record is `failed` + `E_INTERNAL` (`docs/audit-schema.md`, "The terminal record").

Action:
1. `gateway_desanitize_failed`: a gateway bug or a response shape the restorer does not expect. Note `phase` and `error=`; if it started after a litellm or provider change, compare that route's response shape first. There is no fail-open to fall back on.
2. `gateway_terminal_audit_lost` / `terminal_audit_publish_failed`: the audit sink failed twice for that request (the first write and its one retry), so the request has no record — treat it as the sink's incident and as an audit-completeness gap (see below).
3. `terminal_audit_drain_incomplete`: shutdown cut writes short. Check the sink's latency; `CORP_LLM_CANCEL_GRACE_SECONDS` bounds the wait.

### `gateway_failure{component="audit"}` rises

Symptom: `gateway_failure{component="audit"}` counts up; the gateway log has `litellm_audit_orphan_event request_id=… status=…`.

Behavior: not an incident on its own. A litellm log event arrived for a request the guardrail holds no state for — the request's terminal audit record was already written through its ticket (or no pre-call ran), so the event is dropped instead of writing a second, "unknown" record. Nothing is lost and no request is affected.

Action: none for a flat or occasional count. If it grows steadily with traffic, compare the `request_id`s against the audit records: each should already have exactly one terminal record. A request with no record at all is an audit-completeness incident (see below).

Second source, `litellm_guardrail_information_failed request_id=… error=<type>`: the guardrail could not write its content-free entry into litellm's `guardrail_information` (`docs/audit-schema.md`). The request and its audit record are not affected; litellm's payload for that request lacks the entry. So does litellm's OTEL guardrail span, except after `error=GuardrailInformationShapeError`: litellm's writer emits that span itself (`emit_guardrail_span`, litellm 1.101.0 `custom_guardrail.py:1209-1217`) before the gateway checks what the writer built, and taking the entry back out reaches the request metadata copy only. That span holds our allow-listed entry plus the keys litellm's writer generated. `error=GuardrailInformationShapeError` after a litellm upgrade means litellm's writer builds a different entry shape: re-check it against the allow-list before anything else.

### A client gets 403 / 404 `E_ROUTE_BLOCKED`

Symptom: a request answers `{"error": {"type": "route_blocked", "code":
"E_ROUTE_BLOCKED", "route": "<METHOD> <path>", "reason": "<block_reason>"}}`;
`corp_llm_gateway_blocked_requests_total{block_reason="route_gate_*"}` rises.

Behavior: **working as designed, not an outage.** The route gate classifies
every request by `(method, path)` before litellm's router sees it, and refuses
anything the guardrail does not provably rewrite. Full rationale, the refused
set and the reason-code table: [`../security.md`](../security.md) §14.

Action — read `reason` first, it names the cause:

| `reason` | Status | What happened | What to do |
|---|---|---|---|
| `route_gate_listed` | 403 | the table refuses this route (token counting, embeddings, `/v1/completions`, provider-native passthrough, the telemetry batch, litellm's management surface — `/key/*`, the admin API, `GET /health`, …) | Nothing. Expected. For token counting, see [`../security.md`](../security.md) §11 (i): clients use `usage.input_tokens`. For litellm's admin API or UI: refused by design, use `gateway-admin` (§14). |
| `route_gate_unlisted` | 404 | no table entry — default-deny | Either the client asked for a route litellm does not have, or a litellm bump added one and the table has no row for it yet — rows are hand-classified against litellm's source, guarded by the collector test (`docs/extending.md`). For an operator route you own, widen with `CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH`. |
| `route_gate_websocket` | 403 | a `websocket` scope, or `Upgrade: websocket` on any path | Expected: frames after the handshake never reach the hook. Clients must use the HTTP transport. |
| `route_gate_malformed` | 403 | the raw path carries `%2f`, `%00`, `%2e%2e` or a non-ASCII byte, or the decoded path carries `..`, `//` or NUL | Not a config problem. A normal client does not send these — treat a sustained rate as probing and check the audit records. |

There is **no off switch.** The only knob is
`CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH` (`configuration.md`), it adds
PASSTHROUGH entries only — an item naming a refused route exits 78 at boot — and `gateway-admin config check --routes` prints the
effective table plus every extra. If a route genuinely needs to be *rewritten*,
that is a code change: a `table.py` row plus the guard test, never a knob.

### 503 `E_ROUTE_GATE_UNARMED` / 500 `E_ROUTE_GATE_ERROR`

Symptom: every generation route answers 503 with `E_ROUTE_GATE_UNARMED`, or some
answer 500 with `E_ROUTE_GATE_ERROR`; `gateway_failure{component="route_gate"}`
rises. Nothing is forwarded upstream in either case.

Behavior: fail-closed. `E_ROUTE_GATE_UNARMED` means the gate never armed — it
arms inside litellm's lifespan, only after `CorpLlmGuardrail` is confirmed in
`litellm.callbacks`. `E_ROUTE_GATE_ERROR` means classification itself raised.

Action:
1. **Unarmed** is defence in depth, not a steady state: uvicorn finishes lifespan
   startup before it binds, so with `workers=1` no request can arrive unarmed.
   Seeing it means something runs more than one worker or a reloader. Check the
   launch command is `python -m corp_llm_gateway.serve` with nothing overriding
   it — scale with replicas, never with workers.
2. If the pod instead **exited** at boot, see the exit codes below; that is the
   ordinary failure mode for a missing callback.
3. `E_ROUTE_GATE_ERROR` is a gateway bug. The log line carries the reason, the
   scope type and a narrowed method — no path, no headers, no exception text
   (M1-14). Reproduce against `route_gate/classify.py` with the client's method
   and path; it is a code fix, and there is no fail-open to fall back on.

### The gateway pod exits at startup

Symptom: the container never listens on 4000; the process exits with a fixed
code and one log line. `python -m corp_llm_gateway.serve` refuses to serve
half-configured rather than start litellm with no guardrail.

| Exit | Meaning | What to check |
|---|---|---|
| **78** (`EX_CONFIG`) | litellm's config is unusable | `CORP_LLM_LITELLM_CONFIG` (default `/etc/litellm/config.yaml`): file present, named `.yaml`/`.yml`, readable, a non-empty YAML mapping. Also refused: `general_settings.pass_through_endpoints` (registered at runtime, so the gate cannot classify them) and `general_settings.database_url` (the Prisma step reads `DATABASE_URL`/`DIRECT_URL` only). Same code when `CORP_LLM_SERVE_PORT` is not a port number, when a `CORP_LLM_*` in-flight key is out of range (or `CORP_LLM_MAX_INFLIGHT=0` in prod), when `CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH` is malformed or names a refused route, and when issuance is on but cannot be served: partial config, no `CORP_LLM_PG_DSN`, missing extras, a bad CA bundle, Postgres refusing the DSN, TLS or `SELECT`, or a `corp_tokens` without the issuance columns / a valid `corp_tokens_oidc_jti_key` (`upgrade.md`). The log line names the key or the fix; `configuration.md` lists every case. |
| **70** (`EX_SOFTWARE`) | litellm started, but the gateway refuses to arm; one `arm refused (<problem>)` line per problem | `guardrail_absent`: no `CorpLlmGuardrail` in `litellm.callbacks` — the `litellm_settings.callbacks` line in the config must name `corp_llm_gateway.bootstrap.guardrail` (the fail-open that used to start a proxy with no sanitization at all). `apply_guardrail`, `scan_raw_request`, `run_in_parallel`: the guardrail is set up so litellm would skip its pre-call hook or discard its rewrite. `litellm_debug_logging`, `litellm_set_verbose`: litellm's DEBUG output is on (`LITELLM_LOG=DEBUG`, `DETAILED_DEBUG`, `--detailed_debug`, `litellm_settings.set_verbose`); litellm logs the original request before any pre-call hook runs. `CORP_LLM_ALLOW_LITELLM_DEBUG=1` lets these two through outside prod, for tests. `response_compressor`: litellm's app compresses responses, which the gateway's response desanitiser would pass through unrestored. `configuration.md` lists every case. |
| **2** | Prisma schema migration cannot proceed | `DATABASE_URL` / `DIRECT_URL` reachable? The log line carries the RuntimeError text from litellm's `PrismaManager`. Same condition litellm's own CLI exits on. |
| **1** | Prisma schema setup failed after retries, with `ENFORCE_PRISMA_MIGRATION_CHECK` set | The database. Unset that variable to downgrade it to a warning — only if you accept booting against an unmigrated schema. |

Boot lines are on stdout in litellm's JSON record shape when the config sets
`json_logs: true` (or `JSON_LOGS=true`), so Vector parses them like any other
record. `prisma schema setup complete` is the proof the Prisma step ran.

`bash scripts/release/gates.sh` exercises exactly this path before a release: it
runs the built image with no config mounted and requires exit 78.

### 429 `E_CAPACITY` / 408 `E_BODY_TIMEOUT`

Symptom: clients get 429 with `Retry-After: 1`, or 408;
`corp_llm_gateway_blocked_requests_total{block_reason="capacity"}` or
`{block_reason="body_timeout"}` rises.

Behavior: the in-flight cap (`capacity.md`). 429 means every slot
(`CORP_LLM_MAX_INFLIGHT`), every body-read place (`CORP_LLM_MAX_DRAINING`) or the
body-byte budget (`CORP_LLM_MAX_DRAINING_BYTES`) is taken on this pod; 408 means
a body did not arrive whole within `CORP_LLM_BODY_READ_SECONDS`. Nothing reached
litellm or a provider.

Action:
1. `gateway_inflight_requests` at the cap on every pod: real load. Add replicas;
   do not raise the cap past what one pod's CPU and memory carry.
2. `gateway_draining_bytes` near the budget while slots are free: large bodies.
   Raise `CORP_LLM_MAX_DRAINING_BYTES` only if the pod's memory limit allows.
3. Sustained 408s from one source with slots free: a slow or stalled client, or
   probing. Nothing to fix on the gateway.
4. `gateway_failure{component="route_gate"}` alongside: a cancelled request did
   not unwind within its grace; the slot was freed anyway. File it with the
   log line `route_gate_cancel_*`.

### 503 `E_STORE_UNAVAILABLE` / 503 `E_PROFILE_UNAVAILABLE` (Postgres)

Symptom: every LLM request answers 503; `gateway_failure{component="token_store"}`
(`E_STORE_UNAVAILABLE`) or `gateway_failure{component="team_config"}`
(`E_PROFILE_UNAVAILABLE`) rises.

Behavior: fail-closed. Every rewritten request looks its corp token up
(bounded at 6 s) and reads its team config (bounded at 5 s), even for a team
with no profiles; a store that cannot answer refuses the request rather than
pass it unauthenticated or un-profiled. `E_PROFILE_UNAVAILABLE` without
`component="team_config"` is a broken profile instead (`profiles.md`).

Action:
1. Check Postgres (and PgBouncer, if any) reachability from the pod; the log
   line carries the driver's exception class only.
2. Behind PgBouncer, confirm `ignore_startup_parameters` lists the keepalive
   parameters (`configuration.md`, "Backends").
3. Recovery is automatic once the store answers; there is no cache to flush.

### Developer token issuance fails

Symptom: `scripts/install.sh` prints an HTTP status and an error code from
`POST /internal/issue-token`.

| Code | Status | Meaning / action |
|---|---|---|
| `E_ISSUE_DISABLED` | 404 | issuance is off (`CORP_GATEWAY_ISSUE_OIDC_ISSUER` unset) |
| `E_OIDC_*` | 401 | the Keycloak token failed verification — client, audience or groups mapper (`install.md`, "Keycloak realm and client") |
| `E_ISSUE_NO_TEAM` / `E_ISSUE_UNKNOWN_TEAM` | 403 | no mapped group / the mapped team does not exist (`gateway-admin team create`) |
| `E_ISSUE_RATE` | 403 | issued within `CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS`; wait — revoking does not reset it |
| `E_ISSUE_REPLAY` | 403 | this Keycloak token was already used; run the installer again for a new one |
| `E_ISSUE_INFLIGHT` / `E_ISSUE_THROTTLED` | 429 | the route's own caps; retry |
| `E_ISSUE_BUSY` | 503 | the subject's lock or statement timed out (5 s / 8 s); retry |
| `E_JWKS_UNAVAILABLE` | 503 | the pod cannot fetch Keycloak's JWKS — NetworkPolicy (`networkPolicy.keycloak`), CA bundle, Keycloak itself |
| `E_ISSUE_STORE_TIMEOUT` / `E_ISSUE_STORE_UNAVAILABLE` | 503 | Postgres slow or unreachable (see above) |
| `E_ISSUE_SCHEMA` | 503 | the pod booted while Postgres was unreachable and has not yet seen `corp_tokens` current; `/healthz/ready` names the problem — apply `tokens/schema.sql` (`upgrade.md`); readiness and the route re-check at most every 15 s, so retry after that |

### Token revocation didn't take effect immediately

Symptom: `gateway-admin token revoke --user alice` ran, but Alice's traffic still flows for ≤ 60 s.

Behavior: 60 s revocation cache (per `AuthMiddleware`). Documented offboarding lag.

Action: wait 60 s. If still flowing after 60 s, escalate — that's a real bug.

### Audit invariant test fails in CI

Symptom: `tests/invariants/test_no_originals_leak.py` red.

Behavior: build blocks. M1-14 is regression-grade — never bypass.

Action:
1. The file lists six leak surfaces. Find which assertion fired.
2. Trace back to the regression. Most common: someone added `logger.info("...%s", finding.text)` somewhere.
3. Fix the leak; the test pins the surface.

### Audit completeness < 100% in monthly check

Symptom: monthly S3 row count < non-failed request count for the month.

Behavior: violates the non-negotiable acceptance criterion.

Action:
1. Diff the missing records: which team_id, which time window?
2. Check Vector metrics at that window — buffer fill, sink errors.
3. If unexplained: this is incident-grade. Page security + DRI.

## Common operations

### Add a new team

```
gateway-admin team create --team-id team-x --name "Team X"
gateway-admin team set-rules --team-id team-x --from-file team-x.replace.md
gateway-admin team set-retention --team-id team-x --hot-days 90 --cold-years 7
```

### Revoke a fired employee's tokens

```
gateway-admin token revoke --user alice
```

Effect bound to ≤ 60 s by the revocation cache. Within that window, Alice's tokens remain valid.

### Check what's in a team's `replace.md`

The path is in `team_config.replace_md_path`. Read directly from the file or query the `team_config` table.

## Useful kubectl

> **Deployment name.** The chart renders `<release>-corp-llm-gateway`
> (`_helpers.tpl` `fullname`), so for `helm install gw ...` the object is
> `deploy/gw-corp-llm-gateway` — not `deploy/gw` or `deploy/gateway`. The
> release-independent form is a label selector:
> `kubectl -n corp-llm-gateway -l app.kubernetes.io/name=corp-llm-gateway ...`.
> Set `fullnameOverride` if you want a fixed name.


```
kubectl -n corp-llm-gateway get pods
kubectl -n corp-llm-gateway logs deploy/gw-corp-llm-gateway -c litellm
kubectl -n corp-llm-gateway logs deploy/gw-corp-llm-gateway -c vector
kubectl -n corp-llm-gateway exec -it deploy/gw-corp-llm-gateway -c litellm -- python -m corp_llm_gateway.cli.admin team --help
```
