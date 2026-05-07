# Capacity sizing

Plan ref: M4-8 (Vector buffer), M0-10 (pre-pass GPU pod), M1-11
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

## Pre-pass engine (M0-10)

Default per rev-3: GPU pod (cost over latency tradeoff documented).

| Phase | Concurrent calls (est.) | GPU sizing |
|---|---|---|
| Phase 0–1 | ≤ 5 | 1× T4 / single replica |
| Phase 2 | ≤ 25 | 1× T4 (replica:1, autoscale to 2 if p95 > 200ms) |
| Phase 3 | ≤ 200 | 2–4× T4 (replica:2, autoscale to 4) |

Benchmark output (when M0-10 actually runs against real content) gates these numbers; this table is a placeholder for first-day deploy.

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
