# corp-llm-gateway

**English** · [Русский](README.ru.md)

Corporate LLM gateway. Sanitizes traffic between developer Claude Code instances and Anthropic/OpenAI before it leaves the corp boundary.

## Status

**GA-ready.** Landed: the local-first detection cascade, a full security-hardening pass (11 repro-first leak-surface fixes — oversize, NER fail-open, OpenAI `tool_calls`, segmenter coverage, header stripping, dev-proxy, TLS/RBAC), the country / division / regulatory-regime **profile-plugin** layer (declarative bundles + in-tree detector registry + cross-jurisdiction cache isolation), and the operational surfaces (composition root, real `gateway-admin`, production Helm chart, pluggable metrics, served healthz, ops docs). Non-negotiable success criterion: **zero confirmed leak incidents** in the 90 days post-GA.

## Table of contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Repo layout](#repo-layout)
- [Developer quickstart (laptop)](#developer-quickstart-laptop)
- [Operator quickstart (k8s)](#operator-quickstart-k8s)
- [Team rules (`replace.md`)](#team-rules-replacemd)
- [Identity & token flow](#identity--token-flow)
- [Development](#development)
- [Built on](#built-on)

## Overview

A laptop harness (Claude Code, Codex, Cursor) talks HTTP to `gateway.corp.lan`. The gateway is a LiteLLM proxy with a custom guardrail (`corp_llm_gateway.litellm_hook.CorpLlmGuardrail`) registered as a callback. Every request is sanitized in `pre_call`, forwarded to Anthropic / OpenAI with the developer's BYOK key intact, de-sanitized in `post_call`, and audited. Two headers matter on the wire:

| Header | Source | Purpose |
|---|---|---|
| `X-Corp-Auth` | `~/.corp-llm-gateway/token` (laptop) | corp identity / team resolution; **stripped** before egress |
| `Authorization: Bearer …` | dev's Anthropic / OpenAI key | BYOK passthrough; forwarded **untouched** |

## Features

### Detection

- **Russian entity checksums** — ИНН (10/12), КПП, ОГРН (13/15), БИК, СНИЛС, р/счёт with algorithm-validated checksums; near-zero false positives
- **Bilingual NER** — Natasha/Slovnet RU + spaCy `en_core_web_md` EN, run-both-union; covers ФИО, organisations, addresses in mixed-language requests
- **Lemma-gazetteer** — product code-names, regulated ПОД-ФТ / AML-CFT terms, confidentiality markings (`Коммерческая тайна`, `ДСП`, `Confidential`, `NDA`) matched by lemma, not exact string
- **Code-identifier splitter** — splits camel/snake identifiers (`CompanynameabcService`) and scans segments against the gazetteer
- **Test-data allowlist** — deterministic exemption for test fixtures; cannot suppress actual secrets
- **Secret patterns** — JWT, PEM private key, `sk-` / `AKIA` / `ghp_` / generic `password=` / `Bearer` values

### Blocking

- **Stage 0 pre-egress block** — `.env`, kubeconfig, nginx.conf, log-dump signatures → HTTP 422 with `block_reason`; upstream is never called
- **Stage 5 DLP egress guard** — independent second-layer re-scan of the sanitized payload for canary strings and high-confidence secrets; blocks any survivor

### Auth & compliance

- **X-Corp-Auth + Postgres token store** — `AuthMiddleware` validates tokens against `PostgresTokenStore` (asyncpg); 60 s revocation-propagation upper bound
- **`gateway:operator` RBAC** — admin CLI commands gated on JWT claim `gateway:operator`; verified via PyJWT against Keycloak realm roles
- **Audit pipeline** — rich `AuditEvent` schema (ALWAYS / CONDITIONAL field tiers) + NEVER-fields gate: the logger refuses records containing `mapping`, `original`, or `credentials`
- **SIEM sink** — Vector HTTP sink with inherited NEVER-gate + Helm alerts (`AuditVectorDropHigh`, `LeakAttemptDetected`)
- **Egress lockdown** — `NetworkPolicy` (pod egress constrained to upstream + corp CIDRs) + CoreDNS sinkhole (blocks direct `api.anthropic.com` / `api.openai.com` resolution from the cluster), both enabled in `values-prod.yaml`

Detection maps to the corp ИБ requirement set: structural-entity checksums, marked-confidentiality and ПОД-ФТ gazetteers, secret patterns, and config/log egress blocks. The Tier-1 (deterministic) vs Tier-2 (best-effort oracle) split is documented in [`docs/security.md`](docs/security.md).

## Architecture

**Architecture B — assemble best-of-breed.** One custom Python guardrail (`CorpLlmGuardrail`) plugged into a LiteLLM proxy; audit, auth, and observability are operated open-source, not built in-house. Each request runs a deterministic local-first cascade (~6 ms p50 on CPU) — payload classifier → `replace.md` rules → regex+checksum → dual-NER → lemma-gazetteer → code-splitter — with the corp vLLM oracle called only on a gazetteer hit, then a DLP egress guard before upstream.

**→ Full diagram and request lifecycle: [`docs/architecture.md`](docs/architecture.md).**

## Repo layout

```
src/corp_llm_gateway/   Python guardrail (LiteLLM custom hooks + sanitizer engine)
  auth/                 corp-LLM auth provider (Noop default; Bearer/mTLS/OIDC) + factory
  audit/                AuditEvent + Logger + Sinks + factory + retention generator + NEVER-fields gate
  bootstrap.py          production composition root — build_guardrail() from config; lazy `guardrail` singleton
  cli/                  gateway-admin (team/token/extensions/config check), corp-llm-gateway status, proxy
  config.py/settings.py config loader (env→file→default) + typed single-source-of-truth registry + validate()
  corp_llm/             httpx client speaking vLLM /v1/chat/completions
  detectors/            PIIDetector + RegexChecksumDetector + DualNerDetector (RU+EN); fail-closed on missing NER
  extensions/           ExtensionRegistry (audit-sink / provider / detector / … kinds); fail-closed register + api-version gate
  healthz/              live / ready / sanitization / extensions checks + ASGI server (build_health_router)
  metrics/              pluggable exporter (noop / prometheus) — blocked_requests_total + gateway_failure
  payload/              size threshold + gzip + per-team quota + oversize policy
  profiles/             plugin bundles: ProfileBundle/PolicyKnobs + resolver + DETECTOR_REGISTRY + hash-integrity + defaults/
  providers/            ProviderRegistry + executable v1-guard (anthropic / openai / corp-vllm)
  rules/                replace.md parser + gazetteer + cached file loader
  sanitizer/            local-first engine + segmenter + StreamingDesanitizer + DLP guard + orchestrator + ProfileAwareOrchestrator
  storage/              MappingStore (in-memory + Redis)
  team_config/          TeamConfig (+ profile_ids) + store (in-memory + Postgres) + schema.sql
  tokens/               schema.sql + AuthMiddleware + TokenIssuer + stores
  litellm_hook.py       CorpLlmGuardrail — LiteLLM callback adapter (incl. OpenAI tool_calls + streaming)
helm/corp-llm-gateway/  Helm chart (gateway image + guardrail callback, Secret, HPA/PDB/SA, ServiceMonitor, config-check initContainer, NetworkPolicy, CoreDNS sinkhole)
docs/                   architecture + security + audit-schema + ops/* (install/configuration/admin-cli/upgrade/profiles/runbook/capacity) + rbac-matrix + harness-integration + x-corp-auth
scripts/install.sh      laptop installer (bash/zsh/fish, macOS/Linux)
tests/                  pytest, pytest-asyncio mode=auto (~1392 passed / 91 skipped; 3.14 graceful NER, full on 3.12/CI)
```

## Developer quickstart (laptop)

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/jLAM-ERR/corp-llm-gateway/main/scripts/install.sh | bash
```

What it does ([`scripts/install.sh`](scripts/install.sh)):

1. Detects shell (bash / zsh / fish), writes `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `CORP_GATEWAY_TOKEN_FILE`, and (for Claude Code) `ANTHROPIC_CUSTOM_HEADERS` into your rc file between `# >>> corp-llm-gateway >>>` markers.
2. Runs Keycloak device-flow OAuth and writes a 30-day corp token to `~/.corp-llm-gateway/token` (`0600`).
3. Smokes the gateway with a redactable string and verifies round-trip.

Re-running the installer is idempotent — it rotates the token and rewrites the rc block.

The optional `corp-llm-gateway` diagnostics CLI (used by *Verify* below) installs from the repo:

```bash
pipx install "git+https://github.com/jLAM-ERR/corp-llm-gateway.git"   # or: pip install "git+https://…"
```

### Verify

```bash
exec $SHELL -l           # pick up the new env
corp-llm-gateway status  # → token_present=yes, live=yes, healthy=yes
```

### Day-to-day use

Three integration patterns depending on your harness — full recipes in [`docs/harness-integration.md`](docs/harness-integration.md):

| Harness | Recommended | Fallback |
|---|---|---|
| Claude Code | env var (`ANTHROPIC_CUSTOM_HEADERS`, set by `install.sh`) | localhost proxy |
| Codex CLI | `~/.codex/config.toml` `[default.headers]` | localhost proxy |
| Cursor / Continue | app's custom-header settings field | localhost proxy |
| `curl`, raw scripts | `--header 'X-Corp-Auth: …'` | localhost proxy |

The localhost proxy (Pattern 3, `corp-llm-gateway-proxy`) is universal — it injects `X-Corp-Auth` per request and re-reads the token file every call, so token rotation takes effect immediately:

```bash
corp-llm-gateway-proxy --listen 127.0.0.1:9999 --upstream https://gateway.corp.lan
export ANTHROPIC_BASE_URL='http://127.0.0.1:9999'
export OPENAI_BASE_URL='http://127.0.0.1:9999/v1'
```

### Token rotation

Tokens expire every 30 days. With the default Pattern 1 setup, the value is read from disk **once at shell start** (`$(cat …)` snapshot) — so after rotation:

- **Pattern 1 / 2:** open a new shell (or restart the harness).
- **Pattern 3 (proxy):** nothing — the next request picks up the new token automatically.

To rotate manually before expiry, re-run `install.sh`.

### Try the demo

A parallel demo stack shows the full round-trip — redaction, audit pipeline lit up in Langfuse, fail-closed posture — on your laptop: `scripts/demo.sh up` (watch the flow with `scripts/demo.sh logs`). Setup, prompt set, and troubleshooting: [`docs/demo.md`](docs/demo.md).

## Operator quickstart (k8s)

### What gets deployed

The Helm chart ([`helm/corp-llm-gateway/`](helm/corp-llm-gateway/)) ships:

| Workload | Container(s) | Purpose |
|---|---|---|
| `Deployment/gateway` | `litellm` (proxy + guardrail) + `vector` (audit pipeline sidecar) | request path + audit egress |
| `Service/gateway` | — | ClusterIP fronting the deployment |
| `Ingress/gateway` | — | TLS termination at `ingress.host` (default `gateway.corp.lan`) |
| `ConfigMap/*-vector` | — | Vector pipeline + NEVER-fields VRL filter |
| `NetworkPolicy` (optional) | — | constrains egress to upstream + corp-internal CIDRs |
| CoreDNS sinkhole (optional) | — | blocks direct `api.anthropic.com` / `api.openai.com` resolution from the cluster |

External dependencies (not provisioned by the chart): Redis cluster, Postgres, corp vLLM endpoint, Vector sinks (Langfuse / S3 / SIEM).

### Install / upgrade

```bash
# staging
helm upgrade --install gw helm/corp-llm-gateway \
  -f values-staging.yaml --version v0.x.y -n corp-llm-gateway

# wait for readiness across all replicas
kubectl -n corp-llm-gateway rollout status deploy/gateway

# deep sanitization check, then promote to prod against values-prod.yaml
curl https://gateway-staging.corp.lan/healthz/sanitization
```

Rollback: `helm rollback gw <revision>` (Helm keeps the last 10). Full release flow: [`docs/ops/upgrade.md`](docs/ops/upgrade.md).

### Health checks

| Endpoint | Used by | Asserts |
|---|---|---|
| `/healthz/live` | k8s livenessProbe | process up |
| `/healthz/ready` | k8s readinessProbe | dependencies (Redis, Postgres, corp-LLM) reachable |
| `/healthz/sanitization` | post-deploy smoke | end-to-end pre→post round-trip with a redactable string |

### Configuration (Helm values)

Defaults in [`helm/corp-llm-gateway/values.yaml`](helm/corp-llm-gateway/values.yaml). Most-touched keys:

| Key | Default | What it controls |
|---|---|---|
| `replicaCount` | `3` | gateway pods (3 = redis-quorum-friendly) |
| `litellm.versionPin` | `1.40` | LiteLLM image tag — bump only after staging upgrade gate |
| `corpLlm.endpoint` | `""` | URL of the corp vLLM that powers the pre-pass redaction oracle |
| `corpLlm.authProvider` | `"noop"` | switch to a real provider when corp-LLM gains auth (config-only, no code change) |
| `guardrail.contentSizeThresholdBytes` | `102400` | M1-11 oversize-skip threshold |
| `guardrail.cacheA.ttlSeconds` | `36000` | content-keyed dedup TTL |
| `guardrail.cacheB.slidingTtlSeconds` | `3600` | per-conversation mapping TTL (sliding) |
| `audit.sinks.{langfuse,s3,siem}.enabled` | all `true` | toggle individual audit sinks |
| `token.ttlDays` / `token.revocationCacheSeconds` | `30` / `60` | corp-token validity / revocation-propagation upper bound |
| `failPolicy.*` | see file | per-component fail-closed / continue posture (M4 matrix) — the **source of truth**, no ad-hoc fail-open paths in code |
| `coreDnsSinkhole.enabled` / `networkPolicy.enabled` | `false` | egress lockdown (enabled in `values-prod.yaml`) |

Every value also has a TOML property-file fallback (`$CORP_LLM_GATEWAY_CONFIG_FILE` → `~/.corp-llm-gateway/config.toml` → `/etc/corp-llm-gateway/config.toml`, resolved after env vars). Full key reference: [`docs/ops/configuration.md`](docs/ops/configuration.md); template: [`config.example.toml`](config.example.toml).

### Admin CLI (`gateway-admin`)

Operator CLI, typically run via `kubectl exec` against the deployment. Gated on the `gateway:operator` JWT claim.

| Command group | Purpose |
|---|---|
| `gateway-admin team …` | team create / update / list + retention config |
| `gateway-admin token …` | issue / revoke / list corp tokens |
| `gateway-admin extensions …` | list / inspect / health / enable registered extensions |
| `gateway-admin config check` | validate resolved config against the typed settings registry |

Full reference: [`docs/ops/admin-cli.md`](docs/ops/admin-cli.md).

### Day-2 ops

Ongoing operations after install — incident playbook, fail-policy matrix, scaling, and routine admin tasks — live in the runbook: [`docs/ops/runbook.md`](docs/ops/runbook.md). Capacity sizing per rollout phase (alpha → GA at 1000 devs / 50 RPS aggregate): [`docs/ops/capacity.md`](docs/ops/capacity.md).

## Team rules (`replace.md`)

Each team maintains a `replace.md` file at `<rules-dir>/<team_id>.md`. These rules run **first** in the local cascade and **override** auto-detection — a term listed here is always replaced, regardless of what the detectors find.

Format — one rule per line, separator `=` (the legacy `→` U+2192 is still accepted); rules apply longest-first (invariant #5). Quote any value containing `=`:

```markdown
- `Project Polaris` = `[CONFIDENTIAL_PROJECT]`
- `acme-internal-crm.corp.lan` = `[INTERNAL_HOST]`
- `dr.smith@partnerlab.com` = `[PARTNER_CONTACT]`
```

Full spec and authoring tips: [`docs/replace-md-authoring.md`](docs/replace-md-authoring.md).

## Identity & token flow

**`X-Corp-Auth` token** — the corp token lives at `~/.corp-llm-gateway/token` (issued by `install.sh` via Keycloak device flow, 30-day TTL, `0600`). It is sent on every request for identity/team resolution and **stripped before egress** — never forwarded upstream, never logged. The value is read once at shell/harness start, except under the Pattern-3 proxy which re-reads it per request. Full lifecycle (storage, freshness per pattern, failure modes): [`docs/x-corp-auth.md`](docs/x-corp-auth.md).

**Conversation identity** — the gateway mints `conversation_id` per HTTP request (equal to the request UUID). Cache A (content-keyed dedup) works; Cache B (per-conversation mapping) is written but not yet reused across sibling requests, because no harness supplies a stable session ID. Behavior and how to wire a real session ID: [`docs/conversation-id.md`](docs/conversation-id.md).

Who can do what (devs / team leads / operators / security): [`docs/rbac-matrix.md`](docs/rbac-matrix.md).

## Development

Requires Python 3.12+.

```bash
pip install -e ".[dev]"
pre-commit install
PYTHONPATH=src .venv/bin/pytest tests/ -q     # ~1392 passed / 91 skipped, ~23s (3.14 graceful NER; full NER + RS256 crypto on 3.12/CI)
PYTHONPATH=src .venv/bin/ruff check src tests
```

Conventions, invariants, and "things NOT to do" are pinned in [`CLAUDE.md`](CLAUDE.md). CI is CI (`the CI config`).

## Built on

Open-source components this gateway assembles (Architecture B — best-of-breed):

- **Proxy & serving** — [LiteLLM](https://github.com/BerriAI/litellm) (multi-provider proxy + guardrail hooks) · [vLLM](https://github.com/vllm-project/vllm) (backs the corp pre-pass oracle)
- **Bilingual NER & morphology** — RU: [Natasha](https://github.com/natasha/natasha) · [Slovnet](https://github.com/natasha/slovnet) · [Navec](https://github.com/natasha/navec) · [Razdel](https://github.com/natasha/razdel) · [pymorphy3](https://pypi.org/project/pymorphy3/); EN: [spaCy](https://spacy.io) + [`en_core_web_md`](https://spacy.io/models/en). Alternatives ([Presidio](https://github.com/microsoft/presidio), [DeepPavlov](https://github.com/deeppavlov/DeepPavlov)) were evaluated and rejected for CPU latency
- **State & storage** — [Redis](https://redis.io) (mapping / dedup caches) · [PostgreSQL](https://www.postgresql.org) via [asyncpg](https://github.com/MagicStack/asyncpg) (token store)
- **Audit & observability** — [Vector](https://vector.dev) → [Langfuse](https://langfuse.com) + S3 + SIEM
- **Delivery & clients** — [Helm](https://helm.sh) (chart) · [CoreDNS](https://coredns.io) (egress sinkhole) · [httpx](https://www.python-httpx.org) (corp-LLM client)
