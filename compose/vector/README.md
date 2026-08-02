# Vector (audit pipeline) — not yet built

Lands in `docs/plans/20260802-production-compose-corp-ner.md` Task 3 (A3):
`vector.yaml`, ported from `docker/demo-vector/vector.yaml`, carrying the
NEVER-fields VRL gate and `audit_only` filter verbatim (defense-in-depth for
CLAUDE.md invariant #2), wired into `compose/docker-compose.yml` as an
always-on service (not profile-gated — audit is not optional).
