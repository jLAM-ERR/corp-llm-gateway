---
name: readiness-audit
description: Audit corp-llm-gateway's operational/product readiness — config resolution, management CLI, Helm/Docker deploy, observability, docs, packaging — and produce a prioritized gap report with a top-5 build list. Use for "readiness audit", "is this deployable", "what's missing to ship", "productionization gaps". Not for code correctness (use security-audit for leaks).
---

# Readiness audit

Assess whether corp-llm-gateway is a *deployable product* an operator can install, configure, run, and manage — not just a library. Output is a gap report, not code changes.

## Depth
- **Quick** (default): one pass, inline, over the six dimensions below.
- **Thorough**: fan out parallel Explore agents (one per dimension), then synthesize. Use when the caller says "thorough"/"comprehensive" or after a big change.

## What to examine (six dimensions)
1. **Config** — `config.py` (resolution chain env → `$CORP_LLM_GATEWAY_CONFIG_FILE` → `~/.corp-llm-gateway` → `/etc` → default), `config.example.toml`, `settings.py` if present. Every key documented? Single source of truth? Any `os.environ` read at a call site (forbidden)? Startup validation? Undocumented server keys?
2. **Management surfaces** — `cli/{status,admin,proxy}.py`. What can operators actually DO (team CRUD, token issue/revoke, extensions list/health, config check, rules reload, audit query, quota)? What's a `print()` stub vs wired to a real store?
3. **Deployment** — `helm/corp-llm-gateway/` + `Dockerfile*`. Does the chart run the gateway image with the guardrail callback (`corp_llm_gateway.bootstrap.guardrail`) or stock LiteLLM? Secrets, resource limits, HPA/PDB, probes (`/healthz/*`), NetworkPolicy, Vector source (`kubernetes_logs` not `stdin`), values-staging. CPU-only (no GPU).
4. **Observability** — `healthz/` served or just classes? Metrics emitted (do `siem-alerts.yaml`/runbook metric names exist in code)? `/metrics`, ServiceMonitor, dashboards, runbooks in `docs/ops/`.
5. **Docs** — `docs/` + `docs/ops/` + `docs/remaining-steps.md`. Install/prereqs/secret-contract, config reference, admin-CLI reference, troubleshooting, upgrade/migration.
6. **Packaging** — `pyproject.toml` (`[project.scripts]`, extras), `scripts/install.sh`, `.github/workflows/` (does CI build+publish the image/chart, or only the wheel?).

## Grounding rules
Cite exact `file:line`. Be specific to THIS repo — no generic "add monitoring". Respect conventions: default branch `master`, CI is GitHub Actions, interface-registry pattern (ABC in `<module>/<base>.py` → impls → `__init__.py` re-export). Distinguish "class exists but nothing serves/consumes it" from "missing entirely" — that gap recurs here.

## Output
- **Current state** (2–3 sentences: how mature, library-vs-product).
- **Gap table**: Gap | Why it matters | Effort (S/M/L) | Category (config/mgmt/deploy/obs/docs/packaging).
- **Top 5 to build first**, each with a concrete deliverable (a file, a command, a chart addition).
- **Concrete file/module list** for the new work, following repo conventions.

Cross-check against the live plan `docs/plans/*ga-readiness*` before reporting a gap as open — much may already be done or in flight.
