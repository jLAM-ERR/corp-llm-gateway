# Capacity sizing

Plan ref: M4-8 (Vector buffer), M0-10 (in-process CPU detection), M1-11
(per-team Cache A quotas), risks-table corp-LLM rows.

## Workload assumptions

| Phase | Active devs | Concurrent sessions | Aggregate RPS |
|---|---|---|---|
| Phase 0 (alpha) | 5–10 | 5 | 1 |
| Phase 1 (canary) | 5–10 | 5 | 1 |
| Phase 2 (team-by-team) | 50 | 25 | 5 |
| Phase 3 (GA) | 1000 | 200 | 50 |

Numbers are pessimistic point-estimates. Real usage is bursty; budget for 10× burst over the steady-state RPS.

## Vector audit buffer (M4-8)

**Per-pod buffer requirement = `target_buffer_hours × aggregate_rps_per_pod × bytes_per_event`.**

- Per-event audit JSON ≈ 10 KB (ALWAYS fields + small CONDITIONAL set per `docs/audit-schema.md`).
- 3 pods → per-pod RPS = aggregate / 3.
- Default target buffer: 6 hours. Rationale: Langfuse / S3 / SIEM SLO is 99.5% uptime; 6 h covers the longest downstream incident we want to ride out without going fail-closed on `audit_buffer_full`.

| Phase | Per-pod RPS | 6h buffer needed | Helm value |
|---|---|---|---|
| Phase 0 | 0.4 | ~85 MB | `5Gi` (over-provisioned for safety) |
| Phase 2 | 1.7 | ~370 MB | `5Gi` |
| Phase 3 | 17 | ~3.7 GB | `5Gi` (margin tight; bump to `10Gi` if alerts at 50% fire) |
| Phase 3 burst (10× = 170 RPS/pod) | 170 | ~37 GB | revisit before GA — likely shorten buffer to 1h or scale pods |

SIEM alert at 50% buffer fill (M3-9) gates the bump decision.

## In-process detection (M0-10)

CPU-only — corp k8s has no GPU pods. There is **no separate pre-pass pod**:
detection (regex+checksum, dual-NER, gazetteer) runs **in-process inside the
gateway pod** as a local-first cascade (ADR-003, ~6 ms p50 on CPU); the corp-LLM
oracle is only a conditional fallback. Latency is mitigated by horizontal
scale-out (more gateway replicas / HPA) and the M1-11 content-size threshold.
The orphaned `prePass:` block in `values.yaml` is vestigial — it wires no pod.

Sizing below is per **gateway** pod (which also runs the LiteLLM proxy), driven
by the `autoscaling` HPA in `values.yaml`:

| Phase | Concurrent calls (est.) | Gateway pod sizing |
|---|---|---|
| Phase 0–1 | ≤ 5 | 1–2 pods × (2 vCPU, 8 GB) |
| Phase 2 | ≤ 25 | 2 pods × (4 vCPU, 16 GB), autoscale to 4 if p95 > 500 ms |
| Phase 3 | ≤ 200 | 4 pods × (4 vCPU, 16 GB), autoscale to 12 |

Benchmark output (when M0-10 runs against real content) gates these numbers; this
table is a placeholder for first-day deploy.

If CPU latency makes the 4 s p99 budget infeasible, note that lowering the M1-11
content-size threshold no longer "bypasses sanitization" — that was the old
deliver-unsanitized behaviour, a confirmed leak. Oversize handling is now
governed by `CORP_LLM_OVERSIZE_POLICY`, which defaults to **`fail-closed`**, so
lowering the threshold simply **rejects more requests** (422 `E_OVERSIZE_BLOCKED`).
Choose deliberately:

- stay `fail-closed` and scale out instead (more gateway replicas / HPA);
- set `chunk` to sanitize large payloads in overlapping windows — costs latency
  rather than shedding it;
- set `deliver-flag` only for teams in `CORP_LLM_OVERSIZE_DELIVER_TEAMS`, and
  only knowing a clean full rescan is required before anything is forwarded.

## In-flight cap per pod

The route gate caps concurrent LLM requests per pod. The cap is
`CORP_LLM_MAX_INFLIGHT` (default **64**), enforced by the gateway itself in
`route_gate/inflight.py`:

- **What counts.** Only an armed rewritten request (`/v1/messages`,
  `/v1/chat/completions`, `/v1/responses`, …). Health probes, `/metrics`, model
  listing and the issuance route never take a slot — issuance has its own bound
  (`CORP_GATEWAY_ISSUE_MAX_INFLIGHT`).
- **How long.** A slot is held for the **whole request**, SSE stream included.
  A Claude Code session that streams for two minutes holds its slot for two
  minutes, so size the cap by concurrent streams, not by requests per second.
- **When it is full.** A request that arrives with every slot taken gets **429**
  before its body is read, before litellm and before the sanitizer; one that
  loses the last slot while its body was still arriving gets the same 429 after
  it. The response carries `Retry-After: 1`:

  ```json
  {"error": {"type": "capacity", "code": "E_CAPACITY", "route": "POST /v1/messages", "reason": "capacity"}}
  ```

  It is counted in `corp_llm_gateway_blocked_requests_total{block_reason="capacity"}`
  and audited with `block_reason` `capacity`. `gateway_inflight_requests` is the
  current number of held slots.
- **Scale replicas, not the cap.** The cap protects one pod's CPU (the
  local-first cascade runs in-process) and memory (each admitted body, up to
  25 MiB, is buffered once). More concurrent sessions means more pods: 200
  concurrent sessions at the default cap is 4 pods with headroom.
- **The body comes first, under a deadline.** A slot is taken only once the
  whole body has arrived, so a client that announces a body and never sends it
  holds no slot. The body must arrive within `CORP_LLM_BODY_READ_SECONDS`
  (default **30**, above 0 and at most 300); past it the gate answers **408**
  `E_BODY_TIMEOUT` (`block_reason` `body_timeout`). At most
  `CORP_LLM_MAX_DRAINING` requests per pod read a body at once (default **4 ×
  `CORP_LLM_MAX_INFLIGHT`**, never below it, at most 40000); the next gets 429
  `E_CAPACITY` without a byte read. Each body being read is buffered, up to
  25 MiB, so the worst-case body memory per pod is `CORP_LLM_MAX_DRAINING` ×
  25 MiB; lower the draining cap if pod memory cannot hold that. Measured on a real socket (loopback, default
  cap 64, a 2 s deadline): 65 unauthenticated clients announcing a 10-byte body
  and sending nothing kept `gateway_inflight_requests` at 0 while a normal
  request was served (200 in about 20 ms); all 65 got 408 at 2.04 s.
- **Off.** `0` turns the cap off. The entrypoint refuses it (exit 78) when
  `CORP_ENV` is `prod`/`production`, and so does `gateway-admin config check`;
  so do a negative value, a non-integer and anything above 10000.
- **litellm's own setting does nothing.** litellm 1.101.0 honours
  `general_settings.global_max_parallel_requests` only in its legacy limiter,
  selected by `LEGACY_MULTI_INSTANCE_RATE_LIMITING`, and that limiter skips
  `/v1/messages`. No shipped config sets either, and a render test keeps it so.

### Client disconnects

uvicorn does not stop a request whose client went away; the limiter does. It
reads the request body up front (over 25 MiB is `oversize:blocked`, 422, the
same outcome as an oversize text leaf), replays it to litellm and watches the
socket for the whole request. When the client disconnects before the response
is complete — during auth or sanitization, while waiting for the first byte, or
mid-stream — the gateway:

1. cancels the request and gives it `CORP_LLM_CANCEL_GRACE_SECONDS` (default 5)
   to unwind, which closes the upstream connection;
2. cancels any task the request started that is still running — never a task
   other requests share, such as a token lookup or a JWKS fetch several requests
   wait on — and
3. writes one audit record with `status` `cancelled` and `error_code`
   `E_CLIENT_DISCONNECTED` (counts only), counted in
   `gateway_cancelled_requests_total`; steps 2 and 3 share one more grace, so a
   slot is held **at most 2 × `CORP_LLM_CANCEL_GRACE_SECONDS`** after a
   disconnect. If the audit sink fails or runs out of that budget, the
   request's state is kept and the next litellm event for it writes the terminal
   record instead;
4. frees the slot — once, whatever happened above.

Measured on a real socket (loopback, stalled upstream stub): the slot is back
and the upstream socket closed within about 10 ms of the client closing, in every
case. A request that does not unwind within the grace still frees its slot and
records `gateway_failure{component="route_gate"}`. litellm's own
`general_settings.cancel_on_disconnect: true` is set in the shipped configs as a
second layer.

## Corp-LLM throughput floor

Per the plan's open-question #2 settlement: assume **10 RPS sustained / 20 RPS burst** until the corp-LLM team confirms higher.

- Phase 0–1 (≤ 1 RPS): comfortably below floor.
- Phase 2 (5 RPS): comfortable.
- Phase 3 (50 RPS): exceeds the floor — revisit before Phase 2 exit. Mitigation per risks table: per-team rate limiting, content-size threshold tuning (M1-11).

## Redis (Cache A + Cache B)

Cluster size: 3 nodes × 4 GB = 12 GB total, 75% maxmemory ceiling = 9 GB working budget.

Per-team Cache A quota default: 1 GB (`values.guardrail.cacheA.perTeamQuotaBytes`). Supports 9 large teams or many small ones. Per-team override allowed via team_config.

Working set estimate at Phase 3:
- Cache A: 1000 devs × ~5 unique content hashes/day × 50 KB avg post-gzip ≈ 250 MB.
- Cache B: 200 concurrent conversations × 100 placeholder pairs × ~500 bytes ≈ 10 MB.
- Headroom: comfortable.

## Postgres

Single HA pair. Read load is dominated by token lookups (cached 60s in `AuthMiddleware`, so steady ≤ 1 QPS even at Phase 3). Write load is negligible — token issuance + team config edits are admin-driven.

No tuning needed at Phase 0–3 sizes.

## Sizing review cadence

- **Pre-Phase-1**: capacity-test with 10× expected RPS using `docs/ops/load-test-scenario.md` (ported from data-sanitizer plugin).
- **Pre-Phase-2**: re-test at the new dev count.
- **Pre-Phase-3**: re-test plus 10× burst.
- **Post-GA**: review monthly during the 90-day acceptance window; adjust if any alert from the M3-9 set fires.
