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

Benchmark output (when M0-10 runs against real content) gates these numbers; this table is a placeholder for first-day deploy. If CPU latency makes the 4 s p99 budget infeasible, mitigate first by lowering the M1-11 content-size threshold from 100 KB to 25 KB so the largest payloads bypass sanitization rather than spending the budget.

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
