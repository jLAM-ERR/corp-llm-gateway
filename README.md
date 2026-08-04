# corp-llm-gateway

**English** · [Русский](README.ru.md)

Corporate LLM gateway. Sanitizes traffic between developer Claude Code instances and Anthropic/OpenAI before it leaves the corp boundary.

## Status

**GA-ready.** Landed: the local-first detection cascade, a full security-hardening pass (11 repro-first leak-surface fixes — oversize, NER fail-open, OpenAI `tool_calls`, segmenter coverage, header stripping, dev-proxy, TLS/RBAC), the country / division / regulatory-regime **profile-plugin** layer (declarative bundles + in-tree detector registry + cross-jurisdiction cache isolation), and the operational surfaces (composition root, real `gateway-admin`, production Helm chart, pluggable metrics, served healthz, ops docs). Non-negotiable success criterion: **zero confirmed leak incidents** in the 90 days post-GA.

**Newest:** a **production docker-compose deploy target** for non-k8s hosts ([`compose/`](compose/)) — the full data plane plus self-hosted Langfuse v3 and the Vector audit pipeline, with two mutually exclusive auth modes (corp API keys, or a developer's own Anthropic **subscription** forwarded via OAuth), one-command server bootstrap and deploy scripts, and an optional **corp NER service** detector. Start at [`docs/ops/deployment-modes.md`](docs/ops/deployment-modes.md).

## Table of contents

- [Overview](#overview)
- [Features](#features)
- [Architecture](#architecture)
- [Repo layout](#repo-layout)
- [Developer quickstart (laptop)](#developer-quickstart-laptop)
- [Deployment targets](#deployment-targets)
- [Run on a server (docker compose)](#run-on-a-server-docker-compose)
- [Run locally (docker compose)](#run-locally-docker-compose)
- [Operator quickstart (k8s)](#operator-quickstart-k8s)
- [Team rules (`replace.md`)](#team-rules-replacemd)
- [Identity & token flow](#identity--token-flow)
- [Extending the gateway](#extending-the-gateway)
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
- **Corp NER service** (optional, off) — a remote NER detector appended to the local cascade (`CORP_NER_ENABLED` + `CORP_NER_ENDPOINT`). Network-backed, so it is the one detector excluded from `CODE` segments — it would ship source code to an external service; all local detectors keep scanning code. Enabled without an endpoint is a boot refusal, never a silent skip

### Blocking

- **Stage 0 pre-egress block** — `.env`, kubeconfig, nginx.conf, log-dump signatures → HTTP 422 with `block_reason`; upstream is never called
- **Stage 5 DLP egress guard** — independent second-layer re-scan of the sanitized payload for canary strings and high-confidence secrets; blocks any survivor

### Auth & compliance

- **X-Corp-Auth + Postgres token store** — `AuthMiddleware` validates tokens against `PostgresTokenStore` (asyncpg); 60 s revocation-propagation upper bound
- **Two upstream credential modes** — corp API keys (developers hold a per-person LiteLLM virtual key: revocation + spend accounting) or **subscription passthrough**, where the developer's own Anthropic OAuth bearer is forwarded untouched and no corp `ANTHROPIC_API_KEY` exists at all. Mutually exclusive, chosen at deploy time; sanitization, team identity and audit are identical in both — [`docs/ops/deployment-modes.md`](docs/ops/deployment-modes.md)
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
  corp_ner/             httpx client + factory for the optional remote corp NER service (/v1/analyze)
  detectors/            PIIDetector + RegexChecksumDetector + DualNerDetector (RU+EN) + CorpNerDetector; fail-closed on missing NER
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
compose/                production single-host deploy target — data plane (litellm + redis + postgres) + self-hosted
                        Langfuse v3 + Vector audit pipeline; docker-compose.oauth.yml (subscription mode) and
                        docker-compose.build.yml (build from source) overlays
examples/compose/       lightweight local sanitizing proxy (one container, oracle off) — not a deploy target
docs/                   architecture + security + audit-schema + ops/* (install/configuration/admin-cli/deployment-modes/deploy-handoff/upgrade/profiles/runbook/capacity) + rbac-matrix + harness-integration + x-corp-auth
scripts/install.sh      laptop installer (bash/zsh/fish, macOS/Linux)
scripts/deploy/         server bootstrap (bootstrap-server.sh + systemd unit) + deploy.sh (push/upgrade a host)
tests/                  pytest, pytest-asyncio mode=auto (~2274 passed / 107 skipped; 3.14 graceful NER, full on 3.12/CI)
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

To run Codex against a ChatGPT subscription (OAuth, no OpenAI API key) instead of a static provider key, use the separate Responses profile: [`docs/chatgpt-codex.md`](docs/chatgpt-codex.md).

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

## Deployment targets

Four things in this repo run the gateway. Only the first two are deploy targets.

| Target | Where | Use when |
|---|---|---|
| **Single host, docker compose** | [`compose/`](compose/) | production on a non-k8s host — full data plane + self-hosted Langfuse + audit pipeline |
| **Kubernetes** | [`helm/corp-llm-gateway/`](helm/corp-llm-gateway/) | production on a cluster — see [Operator quickstart](#operator-quickstart-k8s) |
| Local sanitizing proxy | [`examples/compose/`](examples/compose/) | one container in front of Anthropic/OpenAI on a laptop, oracle off — **not** a deploy target |
| Laptop demo | `docker-compose.demo.yml` (`scripts/demo.sh`) | show the round-trip and the audit trail — in-memory tokens, hardcoded team token, no audit pipeline |

## Run on a server (docker compose)

[`compose/`](compose/) is the production target for hosts without Kubernetes. It ships the **data plane** (`litellm` — the guardrail-fronted proxy — plus `redis` for Cache B and `postgres` for tokens/team config), **self-hosted Langfuse v3** (`langfuse-web`/`-worker`, ClickHouse, MinIO, its own Redis) and the **audit pipeline** (`vector`, tailing the gateway's stdout into Langfuse, with the NEVER-fields VRL gate byte-identical to the Helm chart's).

### Pick an auth mode first

Two modes, both production, **mutually exclusive** — decide before you write `.env`. They run the same stack, the same cascade and the same audit chain; only the upstream credential differs.

| | **Mode A** — corp API keys | **Mode B** — subscription (OAuth) |
|---|---|---|
| Stack | `docker compose up -d` | `-f docker-compose.yml -f docker-compose.oauth.yml` |
| Upstream credential | the gateway's `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | the developer's own Anthropic OAuth bearer, forwarded untouched |
| Developer sends | `Authorization: Bearer <litellm virtual key>` | `Authorization: Bearer <sk-ant-oat…>` |
| Team identity | `X-Corp-Auth: <team token>` | same |
| `LITELLM_MASTER_KEY` | **required** | **must be absent** (a blank line counts as set — delete it) |
| Routes served | `claude-*`, `gpt-*`, `corp-*` | `claude-*` only, and that is a load-bearing control, not a simplification |
| Per-developer revocation / spend | yes, via the LiteLLM Admin UI | no |

Full matrix, every failure mode, and why the two cannot coexist: [`docs/ops/deployment-modes.md`](docs/ops/deployment-modes.md) · RU: [`docs/ops/deployment-modes.ru.md`](docs/ops/deployment-modes.ru.md).

### Quickstart

```bash
# day 0, on the server (installs docker if missing, creates /opt/corp-llm-gateway
# and a 0600 .env, then exits 1 so you fill the .env in — that exit is expected)
sudo scripts/deploy/bootstrap-server.sh

# stage the token-store schema — postgres init scripts only run on an empty volume
cp src/corp_llm_gateway/tokens/schema.sql compose/postgres/initdb/01-schema.sql

cd compose && docker compose up -d          # Mode A
docker compose ps                           # healthy in ~30-60s; langfuse ~2 min on a fresh volume
curl -fsS http://127.0.0.1:4000/health/liveliness
```

Skipping the schema step is a silent trap, not a visible failure: every service reports healthy and `/health/liveliness` answers, but every real request 500s because `corp_tokens` / `team_config` don't exist.

Secrets with no default (`GATEWAY_IMAGE_TAG`, `POSTGRES_PASSWORD`, the `LANGFUSE_*` infrastructure secrets, `CORP_LANGFUSE_PUBLIC_KEY` / `CORP_LANGFUSE_SECRET_KEY`) make `docker compose up` refuse to start naming the variable, rather than booting half-configured. Full annotated template: [`compose/.env.example`](compose/.env.example).

### Deploy from an operator laptop

```bash
scripts/deploy/deploy.sh --host user@server up               # Mode A
scripts/deploy/deploy.sh --host user@server --mode oauth up  # Mode B
```

It stages the SQL schema, syncs `compose/`, pulls, brings the stack up and waits for healthchecks. The local `.env` is never uploaded and the server's `.env` is never read, printed or overwritten; keys and certificates are excluded from the sync. Other subcommands: `down` (volumes survive, asks first), `restart`, `logs`, `status`; useful flags `--dry-run`, `--yes`, `--dir`, `--force-unlock`. **Pass the same `--mode` on every later run against that host** — `logs`/`status`/`down` resolve the stack through the same file list, and a run without it reports on (or recreates) a different stack.

For boot-time autostart in Mode B, also uncomment `COMPOSE_FILE=docker-compose.yml:docker-compose.oauth.yml` in `.env`: the systemd unit runs a bare `docker compose up -d`, which would otherwise resolve the base file alone.

### The two optional network dependencies

Both are **off by default**, and turning either on is safe for Cache A — the cache key folds a fingerprint of the effective detector policy, so pods with different settings derive disjoint keys. No flush needed.

| Toggle | Off (default) | On |
|---|---|---|
| `CORP_LLM_ORACLE_ENABLED` | no oracle call is ever attempted; `CORP_LLM_ENDPOINT` unneeded for detection | requires a reachable `CORP_LLM_ENDPOINT` — enabling it without one fails requests closed on **every** route, not just `corp-*` |
| `CORP_NER_ENABLED` | no detector, no readiness probe — as if the feature did not exist | requires `CORP_NER_ENDPOINT` (base URL; the client appends `/v1/analyze`), and a **source build** — the published image tag predates this work |

```bash
# run this branch's code instead of the published tag (builds the ru-en NER profile
# on purpose: the Dockerfile default ships no EN model and would 503 every request)
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Don't confuse `CORP_NER_ENABLED` (the remote service) with `CORP_LLM_REQUIRE_NER` (the in-process RU/EN engines), which stays `1` in production either way.

### Know before going live

- **No TLS in front of the stack yet.** The only published port is `127.0.0.1:4000`; the nginx front door is a later revision. Until then developers reach it through an SSH tunnel, not across the network.
- **In Mode B, litellm's management endpoints are unauthenticated** (`/key/*`, `/model/*`, `/user/*`, the UI) — its proxy auth is skipped entirely without a master key, which is exactly what the mode requires. The LLM routes are still gated by the guardrail's `X-Corp-Auth` check. Loopback-only today; this must be closed at nginx before the port is exposed. Mode A doesn't have this gap.
- **No untrusted `docker run` on that host.** Vector selects audit records by a public container label, so anyone who can start a container there can forge audit records. That is a deployment-model requirement, not advice.
- **Audit is buffered but not fail-closed** — a documented deviation from the `vectorBufferFull` default in [`docs/security.md`](docs/security.md) §8. A stalled audit path does not stop egress; durability is bounded by docker log rotation, not by Vector's disk buffer.

**Full stack reference** (routing, virtual keys, why BYOK isn't available here, Langfuse, the audit pipeline, TLS to the corp vLLM, recovery procedures): [`compose/README.md`](compose/README.md) · RU: [`compose/README.ru.md`](compose/README.ru.md). **Step-by-step handoff** for whoever does the deploy: [`docs/ops/deploy-handoff.md`](docs/ops/deploy-handoff.md) · RU: [`docs/ops/deploy-handoff.ru.md`](docs/ops/deploy-handoff.ru.md).

## Run locally (docker compose)

No corp vLLM, no Kubernetes: `CORP_LLM_ORACLE_ENABLED=0` runs the gateway as
a local sanitizing proxy in front of Anthropic/OpenAI directly, using the
published GHCR image. The local-first cascade (regex+checksum, bilingual
NER, gazetteer, splitter) still runs on every request — only the LLM
oracle's refinement pass is skipped. BYOK is a shared gateway-side key in
this mode, not per-developer (native anthropic/openai routing can't forward
a client's own key — see the compose README for the full finding).

Requires the published image at `≥ v1.0.0-rc.5` (the first tag with the
oracle switch) — or build locally: `docker build -f Dockerfile.gateway
--build-arg NER_PROFILE=en -t corp-llm-gateway:local .`

```bash
cd examples/compose && cp .env.example .env   # fill in a dev token + provider key(s)
docker compose up -d
```

This is a laptop convenience, not a deployment target — it has no audit pipeline and no Langfuse. For a server, use [`compose/`](#run-on-a-server-docker-compose).

Full walkthrough, the BYOK finding, and the oracle re-enable path:
[`examples/compose/README.md`](examples/compose/README.md).

## Operator quickstart (k8s)

The cluster target. For a single non-k8s host, see [Run on a server](#run-on-a-server-docker-compose) — same guardrail, same audit chain, different packaging.

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

Each team maintains a `replace.md` file at `<rules-dir>/<team_id>.md`. These rules run **first** in the local cascade; a rule match and a detector/NER/oracle finding compete on the same span — whichever is longer wins, and a rule wins a tie only when its span is identical to a finding's.

Format — one rule per line, separator `=` (the legacy `→` U+2192 is still accepted). Matching is a plain case-insensitive substring test (previously case-sensitive), for a single-word source or a multi-word phrase alike — there is no identifier-boundary requirement, so `kdir = [X]` also matches the `kdir` inside `mkdir`, not just `KdirService`. On overlap, the **longer span wins**, whether it's a rule or a NER/oracle finding — a rule no longer automatically overrides a longer overlapping finding (it still wins on an identical span). The case-insensitive matching is a behavior change from an earlier release; see [`docs/replace-md-authoring.md`](docs/replace-md-authoring.md) before assuming an existing rule still matches only where it used to. Quote any value containing `=`:

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

## Extending the gateway

Extensions are **in-tree and declarative** — a data bundle (a profile) layered over the core plus a closed set of security-reviewed algorithms selected **by name**. The gateway never loads third-party code on the egress path (air-gapped, CODEOWNERS-audited, hash-sealed, fail-closed), so adding capability is a small reviewable change, not a runtime plugin.

| Extension | Style | You add |
|---|---|---|
| **Detector** | in-tree name registry | `detectors/<name>.py` (`PIIDetector`) + one `DETECTOR_REGISTRY` line + select by name in a profile |
| **Provider** | in-tree name registry | a `ProviderSpec` in `register_builtins` (v1 = anthropic/openai/corp-vllm; v2 behind `CORP_ALLOW_V2_PROVIDERS`) |
| **Audit sink / metrics** | config factory | an ABC impl + one factory-dict entry; select via `CORP_AUDIT_SINK` / `CORP_METRICS_EXPORTER` |
| **Auth provider** | config factory | a `_PROVIDER_FACTORIES` entry; select via `CORP_LLM_AUTH_PROVIDER` |
| **Profile bundle** (country / division / regime) | declarative data | author `profile.toml` + term files, reseal — see [`docs/ops/profiles.md`](docs/ops/profiles.md) |

Worked example — **add a detector**: (1) `src/corp_llm_gateway/detectors/my_rule.py` implementing `PIIDetector` (`async detect(text) -> list[Finding]`); (2) re-export in `detectors/__init__.py`; (3) one line in `DETECTOR_REGISTRY` (`profiles/registry.py`); (4) a contract test in `tests/detectors/`; (5) select it in a profile's `detectors = [...]` and reseal.

Full per-surface guide (sinks, providers, the extensions registry, safety rules, CODEOWNERS): [`docs/extending.md`](docs/extending.md).

## Development

Requires Python 3.12+.

```bash
pip install -e ".[dev]"
pre-commit install
PYTHONPATH=src .venv/bin/pytest tests/ -q     # ~2274 passed / 107 skipped, ~76s (3.14 graceful NER; full NER + RS256 crypto on 3.12/CI)
PYTHONPATH=src .venv/bin/ruff check src tests
```

Conventions, invariants, and "things NOT to do" are pinned in [`CLAUDE.md`](CLAUDE.md). CI is GitHub Actions (`.github/workflows/`).

## Built on

Open-source components this gateway assembles (Architecture B — best-of-breed):

- **Proxy & serving** — [LiteLLM](https://github.com/BerriAI/litellm) (multi-provider proxy + guardrail hooks) · [vLLM](https://github.com/vllm-project/vllm) (backs the corp pre-pass oracle)
- **Bilingual NER & morphology** — RU: [Natasha](https://github.com/natasha/natasha) · [Slovnet](https://github.com/natasha/slovnet) · [Navec](https://github.com/natasha/navec) · [Razdel](https://github.com/natasha/razdel) · [pymorphy3](https://pypi.org/project/pymorphy3/); EN: [spaCy](https://spacy.io) + [`en_core_web_md`](https://spacy.io/models/en). Alternatives ([Presidio](https://github.com/microsoft/presidio), [DeepPavlov](https://github.com/deeppavlov/DeepPavlov)) were evaluated and rejected for CPU latency
- **State & storage** — [Redis](https://redis.io) (mapping / dedup caches) · [PostgreSQL](https://www.postgresql.org) via [asyncpg](https://github.com/MagicStack/asyncpg) (token store)
- **Audit & observability** — [Vector](https://vector.dev) → [Langfuse](https://langfuse.com) + S3 + SIEM
- **Delivery & clients** — [Helm](https://helm.sh) (chart) · [Docker Compose](https://docs.docker.com/compose/) (single-host deploy target) · [CoreDNS](https://coredns.io) (egress sinkhole) · [httpx](https://www.python-httpx.org) (corp-LLM client)

## License

Copyright (c) 2026 Artem Likhomanenko.

The gateway **core** (this repository) is licensed under the
[Apache License 2.0](LICENSE) — free for all use, including
commercial. **Enterprise plugins, prebuilt enterprise distributions,
and commercial support** are separate proprietary offerings — see
[`LEGAL/COMMERCIAL-LICENSING.md`](LEGAL/COMMERCIAL-LICENSING.md).
Contributions are accepted under [`LEGAL/CLA.md`](LEGAL/CLA.md).
