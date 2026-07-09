# Install

How to deploy the corp LLM gateway to a corp Kubernetes cluster with Helm.

The chart is `helm/corp-llm-gateway`. It runs the gateway image (LiteLLM proxy +
the `corp_llm_gateway.bootstrap.guardrail` callback) plus a Vector log-shipper
sidecar, and mounts detection in-process — there is no separate pre-pass pod.

## Prerequisites

- **Kubernetes**, CPU-only. Corp k8s has no GPU pods; the detection cascade
  (regex+checksum, dual-NER, gazetteer) runs on CPU. Do not add GPU node
  selectors — scale out, not up (see `capacity.md`).
- **Postgres** (HA pair). Backs the token store AND the team-config store, both
  keyed on `CORP_LLM_PG_DSN`. Apply the schema before first traffic
  (see `upgrade.md`). Without a DSN the stores fall back to in-memory — dev only,
  state is lost on restart.
- **Redis**. Backs the per-conversation mapping store (Cache B), required for the
  `post_call` desanitization. `REDIS_URL`; unset → in-memory (dev only).
- **corp-LLM endpoint** (`…/v1`). The vLLM oracle. `CORP_LLM_ENDPOINT` is
  **required** — it has no routable default; startup validation refuses to run
  without it.
- **Container registry + Helm repo**. The image is pulled from
  `corp-registry.corp.lan/corp-llm-gateway` (`values.image.repository`); set
  `imagePullSecrets` if the registry is private.
- **Internal CA bundle** (prod). The corp-LLM cert is signed by an internal CA;
  provide it via `caBundle` so TLS verification stays on (see below).

## The Secret contract

Sensitive env is injected into both containers via `envFrom.secretRef`. In real
clusters set `existingSecret` to a Vault / external-secrets-managed Secret (the
chart then renders no Secret of its own); otherwise fill `values.secret.*` from a
NON-committed values file or `--set`. Committed defaults are empty.

Keys the chart Secret carries (`values.secret`, `templates/secret.yaml`):

| Secret key | Consumed by | Notes |
|------------|-------------|-------|
| `CORP_LLM_PG_DSN` | token + team-config stores | required for real deploys |
| `REDIS_URL` | mapping store (Cache B) | required for real deploys |
| `CORP_LLM_AUTH_TOKEN` | LiteLLM `model_list` (legacy oracle key) | corp-LLM is auth-less today |
| `CORP_LLM_BEARER_TOKEN` | oracle auth provider (`auth.factory`) | only when `authProvider=bearer` |
| `CORP_LANGFUSE_PUBLIC_KEY` / `CORP_LANGFUSE_SECRET_KEY` | in-process `LangfuseSink` | only when `CORP_AUDIT_SINK=langfuse` |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Vector langfuse sink | audit fan-out |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | Vector S3 audit sink | audit fan-out |
| `CORP_GATEWAY_OIDC_KEY` | operator RBAC (RS256 public key) | verifies `gateway-admin` tokens |
| `CORP_GATEWAY_ADMIN_TOKEN` | operator JWT | optional; CLI `--token` overrides |

The internal-CA bundle rides a separate Secret (`caBundle.existingSecret`, key
`ca-bundle.pem`, or inline `caBundle.content`). When `caBundle.enabled`, the
deployment sets `CORP_LLM_CA_BUNDLE` (httpx oracle client) and `SSL_CERT_FILE`
(LiteLLM's aiohttp) at the mount path.

### Prod keys the chart does NOT template yet

`values-prod.yaml` flips `networkPolicy` and `coreDnsSinkhole` on, but it does
**not** set the security-relevant keys below, and `deployment.yaml` has no
generic env passthrough. Add them to the Secret map (they inject via
`envFrom`) or to a mounted `config.toml`. See `configuration.md` for why each
matters.

| Key | Set to | Without it |
|-----|--------|------------|
| `CORP_LLM_REQUIRE_NER` | `1` | NER fails **open** in prod (PERSON/ORG can egress) |
| `CORP_ENV` | `prod` | the `SSL_VERIFY=false` guard (F9) stays off |
| `CORP_GATEWAY_OIDC_AUDIENCE` | your aud | operator RBAC cannot verify → all mutations denied |
| `CORP_GATEWAY_OIDC_ISSUER` | your iss | same |

(Wiring these into `values-prod.yaml` is a tracked plan follow-up.)

## Install flow

1. **Apply the DB schema** (once per database) — see `upgrade.md`. Both
   `tokens/schema.sql` and `team_config/schema.sql` are idempotent.

2. **Provision the Secret.** Either create a Vault / external-secrets Secret and
   reference it, or supply a non-committed values file:

   ```
   # secrets.staging.yaml  (NOT committed)
   secret:
     CORP_LLM_PG_DSN: "postgresql://gateway:...@postgres:5432/gateway"
     REDIS_URL: "redis://cache.corp.lan:6379/0"
     CORP_GATEWAY_OIDC_KEY: "-----BEGIN PUBLIC KEY-----\n..."
     CORP_LLM_REQUIRE_NER: "1"
     CORP_ENV: "prod"
     CORP_GATEWAY_OIDC_AUDIENCE: "corp-llm-gateway"
     CORP_GATEWAY_OIDC_ISSUER: "https://keycloak.corp.lan/realms/corp"
   ```

3. **Validate config before serving traffic** (optional but recommended). Run
   `gateway-admin config check` against the target env — it validates every key
   and probes Postgres / Redis / corp-LLM reachability, exiting nonzero on
   failure. See `admin-cli.md`.

4. **Install to staging:**

   ```
   helm upgrade --install gw helm/corp-llm-gateway \
     -f helm/corp-llm-gateway/values-staging.yaml \
     -f secrets.staging.yaml
   ```

   `values-staging.yaml` sets 2 replicas, `image.tag=staging`,
   `existingSecret=corp-llm-gateway-staging-env`, HPA (2→10), the staging
   corp-LLM endpoint, and enables the `/metrics` ServiceMonitor scrape path.

5. **Wait for readiness** on all pods:

   ```
   kubectl -n corp-llm-gateway rollout status deploy/gw
   curl https://gateway-staging.corp.lan/healthz/ready
   curl https://gateway-staging.corp.lan/healthz/sanitization   # deep-check
   ```

6. **Promote to prod** with the same command against `values-prod.yaml` (plus
   the prod Secret). `values-prod.yaml` enables the NetworkPolicy egress lock
   and the CoreDNS sinkhole; layer it on top of `values.yaml` defaults.

## Served HTTP surfaces

The gateway image mounts these onto LiteLLM's ASGI app (probes target them):

- `GET  /healthz/live` — liveness
- `GET  /healthz/ready` — readiness (503 when unhealthy; reflects the deep-check)
- `GET  /healthz/sanitization` — sanitization deep-check
- `GET  /healthz/extensions` — registered-extension health (does not gate readiness)
- `POST /internal/issue-token` — developer onboarding token issuance
- `GET  /metrics` — Prometheus scrape (the series ship with the metrics module; see `configuration.md`)

## Rollback

`helm rollback gw <revision>` (`helm history gw`; Helm keeps the last 10). See
`runbook.md`.
