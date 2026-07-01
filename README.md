# corp-llm-gateway

Corporate LLM gateway. Sanitizes traffic between developer Claude Code instances and Anthropic / OpenAI before it leaves the corp boundary.

Replaces the per-laptop `data-sanitizer` Claude Code plugin (which only covered user prompts) with a centrally-enforced, auditable, multi-provider gateway.

## Status

v1 — pre-execution. See [`docs/plans/20260507-external-sanitizer-gateway-v1.md`](docs/plans/20260507-external-sanitizer-gateway-v1.md). Non-negotiable success criterion: **zero confirmed leak incidents** in the 90 days post-GA.

## Table of contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Features](#features)
- [Team rules (`replace.md`)](#team-rules-replacemd)
- [Repo layout](#repo-layout)
- [Developer quickstart (laptop)](#developer-quickstart-laptop)
  - [Install](#install)
  - [Verify](#verify)
  - [Day-to-day use](#day-to-day-use)
  - [Token rotation](#token-rotation)
- [Operator quickstart (k8s)](#operator-quickstart-k8s)
  - [What gets deployed](#what-gets-deployed)
  - [Install / upgrade](#install--upgrade)
  - [Health checks](#health-checks)
  - [Day-2 ops](#day-2-ops)
- [Demo (laptop)](#demo-laptop)
- [CLIs](#clis)
- [Configuration (Helm values)](#configuration-helm-values)
- [Conversation identity](#conversation-identity)
- [`X-Corp-Auth` token flow](#x-corp-auth-token-flow)
- [Documentation index](#documentation-index)
- [Development](#development)
- [Owner](#owner)

## Overview

A laptop harness (Claude Code, Codex, Cursor) talks HTTP to `gateway.corp.lan`. The gateway is a LiteLLM proxy with a custom guardrail (`corp_llm_gateway.litellm_hook.CorpLlmGuardrail`) registered as a callback. Every request is sanitized in `pre_call`, forwarded to Anthropic / OpenAI with the developer's BYOK key intact, de-sanitized in `post_call`, and audited. Two headers matter on the wire:

| Header | Source | Purpose |
|---|---|---|
| `X-Corp-Auth` | `~/.corp-llm-gateway/token` (laptop) | corp identity / team resolution; **stripped** before egress |
| `Authorization: Bearer …` | dev's Anthropic / OpenAI key | BYOK passthrough; forwarded **untouched** |

Full per-request data flow:

![corp-llm-gateway request/response data flow](docs/data-flow.png)

Source: [`docs/data-flow.puml`](docs/data-flow.puml) (re-render with `plantuml docs/data-flow.puml`).

## Architecture

Architecture B (assemble best-of-breed): single custom Python guardrail plugged into LiteLLM proxy; everything else (audit pipeline, auth, observability) is open-source operated.

```mermaid
flowchart TD
    Dev["Claude Code (laptop)"]
    Dev -->|"POST /v1/chat/completions<br/>X-Corp-Auth + Authorization:Bearer"| S0

    subgraph gw["LiteLLM proxy · CorpLlmGuardrail (corp k8s)"]
        S0["Stage 0 · payload classifier"]
        S0 -->|"config / log hit"| B0(["HTTP 422  block_reason"])
        S0 -->|pass| RULES

        subgraph lc["Local-first cascade — ~6 ms p50 on CPU"]
            RULES["1. replace.md rules  (per-team, length-sorted, OVERRIDES detection)"]
            --> RX["2. regex + checksum  (ИНН · КПП · ОГРН · БИК · JWT · IP · secrets)"]
            --> NER["3. dual-NER run-both-union  (Natasha RU + spaCy en_core_web_md EN)"]
            --> GZ["4. lemma-gazetteer  (products · ПОД-ФТ · markings)"]
            --> SEG["5. code-identifier splitter"]
        end

        SEG -->|"gazetteer hit"| ORC["LLM oracle — corp vLLM  (conditional · ~7–15 s)"]
        SEG -->|"no hit"| DLP
        ORC --> DLP["Stage 5 · DLP egress guard"]
        DLP -->|"canary / secret leak"| B5(["HTTP 422  block_reason"])
    end

    DLP -->|clean| UP["Anthropic / OpenAI  (Authorization:Bearer untouched)"]
    UP --> DS["post_call · StreamingDesanitizer  (placeholders longest-first)"]
    DS --> AUD["audit · Vector → Langfuse + S3 + SIEM  (NEVER-fields gate)"]
    DS -->|"originals restored"| Dev
```

Request lifecycle:

1. **Stage 0** — payload classifier: `.env`, kubeconfig, log-dump signatures → HTTP 422 `block_reason`; upstream never called.
2. **Local-first cascade** (deterministic, ~6 ms p50 on CPU):
   - `replace.md` per-team rules (length-sorted, OVERRIDES auto-detection)
   - regex + checksum: ИНН / КПП / ОГРН / БИК / СНИЛС / р-счёт, JWT, PEM, `sk-` / `AKIA` / `ghp_`, IPv4/6, internal hostnames
   - dual-NER run-both-union: Natasha/Slovnet (RU) + spaCy `en_core_web_md` (EN) — bilingual ФИО / org / geo
   - lemma-gazetteer: product code-names, regulated ПОД-ФТ terms, confidentiality markings
   - code-identifier splitter: `CompanynameabcService`-style camel/snake identifiers in code
3. **LLM oracle** (conditional fallback): invoked only on a deterministic gazetteer hit; adds Tier-2 coverage for unmarked know-how. Latency ~7–15 s vs ~6 ms local. Two-venv reality: Python 3.12 = full NER; Python 3.14 = graceful degradation (NER imports are lazy, `[ner]` optional extra).
4. **Stage 5 DLP egress guard**: independent second-layer re-scan of the sanitized outbound payload for canary strings and high-confidence secrets; blocks any survivor.
5. **post_call**: `StreamingDesanitizer` rebuilds originals from the per-conversation mapping (placeholders sorted longest-first — invariant #5).
6. **audit**: Vector → Langfuse + S3 + SIEM with NEVER-fields gate.

Full architecture in the [v1 plan](docs/plans/20260507-external-sanitizer-gateway-v1.md).

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

- **X-Corp-Auth + Postgres token store** — `AuthMiddleware` validates tokens against `PostgresTokenStore` (asyncpg); 60 s revocation propagation upper bound
- **`gateway:operator` RBAC** — admin CLI commands gated on JWT claim `gateway:operator`; verified via PyJWT against Keycloak realm roles
- **Audit pipeline** — rich `AuditEvent` schema (ALWAYS / CONDITIONAL field tiers) + NEVER-fields gate: the logger refuses records containing `mapping`, `original`, or `credentials`
- **SIEM sink** — Vector HTTP sink with inherited NEVER-gate + Helm alerts (`AuditVectorDropHigh`, `LeakAttemptDetected`)
- **Egress lockdown** — `NetworkPolicy` (pod egress constrained to upstream + corp CIDRs) + CoreDNS sinkhole (blocks direct `api.anthropic.com` / `api.openai.com` resolution from the cluster), both enabled in `helm/.../values-prod.yaml`

**Compliance:** ✅ 11 / 🟡 3 / ⚪ 1 vs the 15 ИБ requirements — see [`docs/requirements-compliance.md`](docs/requirements-compliance.md).

## Team rules (`replace.md`)

Each team maintains a `replace.md` file at `<rules-dir>/<team_id>.md`. These rules run **first** in the local cascade and **override** auto-detection — a term listed here is always replaced, regardless of what the detectors find.

Format — one rule per line:

```
- `ORIGINAL` → `REPLACEMENT`
```

The separator is `→` (U+2192), **not** ASCII `->`. Rules are applied longest-first (invariant #5). Example:

```markdown
- `Project Polaris` → `[CONFIDENTIAL_PROJECT]`
- `acme-internal-crm.corp.lan` → `[INTERNAL_HOST]`
- `dr.smith@partnerlab.com` → `[PARTNER_CONTACT]`
```

The demo's live rules file is `docker/demo-litellm/rules/demo-team.md`. Full spec and authoring tips: [`docs/replace-md-authoring.md`](docs/replace-md-authoring.md).

## Repo layout

```
src/corp_llm_gateway/   Python guardrail (LiteLLM custom hooks + sanitizer engine)
  auth/                 corp-LLM auth provider (Noop default; Bearer/mTLS/OIDC stubs)
  audit/                AuditEvent + Logger + Sinks + retention generator + NEVER-fields gate
  cli/                  gateway-admin (operators), corp-llm-gateway status (devs), proxy
  corp_llm/             httpx client speaking vLLM /v1/chat/completions
  detectors/            PIIDetector + ShadowDetector + RegexChecksumDetector + DualNerDetector
  healthz/              live / ready / sanitization deep-check
  payload/              size threshold + gzip + per-team quota
  rules/                replace.md parser + cached file loader
  sanitizer/            local-first engine + StreamingDesanitizer + DLP guard + orchestrator
  storage/              MappingStore (in-memory + Redis)
  team_config/          TeamConfig + store
  tokens/               schema.sql + AuthMiddleware + TokenIssuer
  litellm_hook.py       CorpLlmGuardrail — LiteLLM callback adapter
helm/corp-llm-gateway/  Helm chart (deployment, service, configmap, NetworkPolicy, CoreDNS sinkhole)
docs/                   plan + audit-schema + ops/* + rbac-matrix + data-flow + integration docs
scripts/install.sh      laptop installer (bash/zsh/fish, macOS/Linux)
tests/                  pytest, pytest-asyncio mode=auto (546 tests, ~16s)
```

## Developer quickstart (laptop)

### Install

```bash
curl -fsSL https://git.corp.lan/<group>/corp-llm-gateway/-/raw/master/scripts/install.sh | bash
```

What it does ([`scripts/install.sh`](scripts/install.sh)):

1. Detects shell (bash / zsh / fish), writes `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`, `CORP_GATEWAY_TOKEN_FILE`, and (for Claude Code) `ANTHROPIC_CUSTOM_HEADERS` into your rc file between `# >>> corp-llm-gateway >>>` markers.
2. Runs Keycloak device-flow OAuth and writes a 30-day corp token to `~/.corp-llm-gateway/token` (`0600`).
3. Smokes the gateway with a redactable string and verifies round-trip.

Re-running the installer is idempotent — it rotates the token and rewrites the rc block.

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

Localhost proxy (Pattern 3) is universal — it injects `X-Corp-Auth` per request and re-reads the token file every call, so token rotation takes effect immediately:

```bash
corp-llm-gateway-proxy --listen 127.0.0.1:9999 --upstream https://gateway.corp.lan
export ANTHROPIC_BASE_URL='http://127.0.0.1:9999'
export OPENAI_BASE_URL='http://127.0.0.1:9999/v1'
```

### Token rotation

Tokens expire every 30 days. With the default Pattern 1 setup, the value is read from disk **once at shell start** (`$(cat …)` snapshot) — so after rotation:

- **Pattern 1 / 2:** open a new shell (or restart the harness).
- **Pattern 3 (proxy):** nothing — the next request picks up the new token automatically.

To rotate manually before expiry, re-run `install.sh`. Full token-flow lifecycle, freshness model, and failure-mode mapping: [`docs/x-corp-auth.md`](docs/x-corp-auth.md).

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

# deep sanitization check
curl https://gateway-staging.corp.lan/healthz/sanitization

# promote to prod against values-prod.yaml
```

Rollback: `helm rollback gw <revision>` (Helm keeps the last 10). Full release flow + rollback in [`docs/ops/runbook.md`](docs/ops/runbook.md).

### Health checks

| Endpoint | Used by | Asserts |
|---|---|---|
| `/healthz/live` | k8s livenessProbe | process up |
| `/healthz/ready` | k8s readinessProbe | dependencies (Redis, Postgres, corp-LLM) reachable |
| `/healthz/sanitization` | post-deploy smoke | end-to-end pre→post round-trip with redactable string |

### Day-2 ops

Source of truth: [`docs/ops/runbook.md`](docs/ops/runbook.md) (incident playbook, fail-policy matrix, common operations like `gateway-admin team create`, `gateway-admin token revoke`, kubectl one-liners).

Capacity sizing per phase (Phase 0 alpha → Phase 3 GA at 1000 devs / 50 RPS aggregate): [`docs/ops/capacity.md`](docs/ops/capacity.md).

## Demo (laptop)

For a guided ~15-min walkthrough of the gateway end-to-end — request round-trip,
audit pipeline lit up in Langfuse, fail-closed posture — bring up the parallel
demo stack: `scripts/demo.sh up`. To watch the redaction flow as it happens, run
`scripts/demo.sh logs` (tails the LiteLLM container filtered to the
sanitize/desanitize flow + audit). Full setup, prompt set, and troubleshooting
in [`docs/demo.md`](docs/demo.md). The demo stack lives in `docker-compose.demo.yml`
and is independent of the CI compose at `docker-compose.yml`.

## CLIs

| Command | Audience | Purpose |
|---|---|---|
| `corp-llm-gateway status` | dev | laptop diagnostics — token present, gateway live, version, update check |
| `corp-llm-gateway-proxy` | dev | localhost header-injecting proxy (Pattern 3) |
| `gateway-admin` | operator | team CRUD, retention config, token issue / revoke |

Entry points are wired via `pyproject.toml`'s `[project.scripts]`. The `gateway-admin` CLI runs against the production deployment (typically via `kubectl exec`).

## Configuration (Helm values)

Defaults in [`helm/corp-llm-gateway/values.yaml`](helm/corp-llm-gateway/values.yaml). Most-touched keys:

| Key | Default | What it controls |
|---|---|---|
| `replicaCount` | `3` | gateway pods (3 = redis-quorum-friendly) |
| `litellm.versionPin` | `1.40` | LiteLLM image tag — bump only after staging upgrade gate |
| `corpLlm.endpoint` | `""` | URL of the corp vLLM that powers the pre-pass redaction |
| `corpLlm.authProvider` | `"noop"` | switch to a real provider when corp-LLM gains auth (config-only, no code change) |
| `guardrail.contentSizeThresholdBytes` | `102400` | M1-11 oversize-skip threshold |
| `guardrail.cacheA.ttlSeconds` | `36000` | content-keyed dedup TTL |
| `guardrail.cacheA.perTeamQuotaBytes` | `1 GiB` | per-team Cache A budget |
| `guardrail.cacheB.slidingTtlSeconds` | `3600` | per-conversation mapping TTL (sliding) |
| `audit.vector.bufferGb` | `5` | on-pod Vector disk buffer |
| `audit.sinks.{langfuse,s3,siem}.enabled` | all `true` | toggle individual audit sinks |
| `token.ttlDays` | `30` | corp-token validity |
| `token.revocationCacheSeconds` | `60` | upper bound on revocation propagation |
| `failPolicy.*` | see file | per-component fail-closed / continue posture (M4 matrix) |
| `coreDnsSinkhole.enabled` | `false` | block direct upstream resolution from the cluster |
| `networkPolicy.enabled` | `false` | constrain pod egress |

The fail-policy keys are the **source of truth** — no ad-hoc fail-open paths in code.

### Property file fallback (TOML)

Every env var the app reads (`CORP_LLM_AUTH_PROVIDER`, `CORP_LLM_BEARER_TOKEN`, `CORP_GATEWAY_URL`, `CORP_GATEWAY_TOKEN_FILE`, …) can also be supplied from a TOML file. Resolution order is: env var → file → caller default — so existing deployments are unchanged. The file is searched at the first existing path of:

1. `$CORP_LLM_GATEWAY_CONFIG_FILE`
2. `~/.corp-llm-gateway/config.toml` (laptop default)
3. `/etc/corp-llm-gateway/config.toml` (server default)

Keys are flat and use the env-var names verbatim:

```toml
CORP_GATEWAY_URL          = "https://gateway.corp.lan"
CORP_GATEWAY_TOKEN_FILE   = "~/.corp-llm-gateway/token"
CORP_LLM_AUTH_PROVIDER    = "bearer"
CORP_LLM_BEARER_TOKEN     = "..."
```

Full template with every supported key: [`config.example.toml`](config.example.toml). Loader source: `src/corp_llm_gateway/config.py`.

## Conversation identity

Today the gateway mints `conversation_id` per HTTP request (it equals the request UUID). Cache A (content-keyed dedup) works; Cache B (per-conversation mapping store) is written but never reused across sibling requests because no harness or proxy supplies a stable session ID yet. Full behavior, consequences, and how to wire a real session ID are in [`docs/conversation-id.md`](docs/conversation-id.md).

## `X-Corp-Auth` token flow

The corp token lives on disk at `~/.corp-llm-gateway/token` (issued by `install.sh` via Keycloak device flow, 30-day TTL, `0600`). The header is sent on **every** HTTP request from the harness, but the *value* is typically read **once** — at shell init (Pattern 1) or harness start (Pattern 2) — so token rotation usually requires a fresh shell. The optional localhost proxy (Pattern 3) re-reads the file per request and makes rotation take effect on the next call. Full lifecycle (storage, freshness per pattern, what the gateway does with the header, common failure modes) is in [`docs/x-corp-auth.md`](docs/x-corp-auth.md). Per-harness setup recipes remain in [`docs/harness-integration.md`](docs/harness-integration.md).

## Documentation index

| Doc | What's inside |
|---|---|
| [`docs/plans/20260507-external-sanitizer-gateway-v1.md`](docs/plans/20260507-external-sanitizer-gateway-v1.md) | v1 plan (current rev in header) — single source of architectural truth |
| [`docs/plans/20260630-bilingual-local-first-detection.md`](docs/plans/20260630-bilingual-local-first-detection.md) | local-first detection cycle plan (DP-0…DP-9, CP-1…CP-4) |
| [`docs/requirements-compliance.md`](docs/requirements-compliance.md) | ИБ requirements compliance matrix — ✅ 11 / 🟡 3 / ⚪ 1 vs 15 requirements |
| [`docs/adr/ADR-003-ner-orchestration.md`](docs/adr/ADR-003-ner-orchestration.md) | ADR: hand-roll dual-NER (Natasha RU + spaCy EN) over Presidio/DeepPavlov |
| [`docs/data-flow.puml`](docs/data-flow.puml) + [`docs/data-flow.png`](docs/data-flow.png) | end-to-end sequence diagram (PlantUML source + rendered PNG) |
| [`docs/harness-integration.md`](docs/harness-integration.md) | per-harness setup recipes (Claude Code, Codex, Cursor, …) |
| [`docs/x-corp-auth.md`](docs/x-corp-auth.md) | corp token lifecycle, per-pattern freshness, failure modes |
| [`docs/conversation-id.md`](docs/conversation-id.md) | `conversation_id` behavior today + how to wire a real session ID |
| [`docs/audit-schema.md`](docs/audit-schema.md) | audit event schema + ALWAYS / CONDITIONAL / NEVER field classification |
| [`docs/security.md`](docs/security.md) | sanitization coverage, audit-pipeline guarantees, SIEM / Langfuse handling, known config gaps |
| [`docs/replace-md-authoring.md`](docs/replace-md-authoring.md) | how to write per-team `replace.md` rules files |
| [`docs/rbac-matrix.md`](docs/rbac-matrix.md) | who can do what (devs / team leads / operators / security) |
| [`docs/remaining-steps.md`](docs/remaining-steps.md) | running checklist of v1 work left |
| [`docs/ops/runbook.md`](docs/ops/runbook.md) | release, rollback, incident playbook, common ops |
| [`docs/ops/capacity.md`](docs/ops/capacity.md) | sizing per rollout phase (alpha → GA) |
| [`docs/adr/`](docs/adr/) | architecture decision records |

## Development

Requires Python 3.12+.

```bash
pip install -e ".[dev]"
pre-commit install
PYTHONPATH=src .venv/bin/pytest tests/ -q     # 546 tests, ~16s
PYTHONPATH=src .venv/bin/ruff check src tests
```

Conventions, invariants, and "things NOT to do" are pinned in [`CLAUDE.md`](CLAUDE.md). Default branch is `master`. CI is CI (`the CI config`).

## Owner

corp-internal@corp.lan
