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

litellm resolves `litellm_settings.callbacks` as a **file path** relative to
the mounted config dir, not a package import — so the chart projects a
delegating shim (`corp_llm_gateway/bootstrap.py`) into the same ConfigMap as
`config.yaml` via `configMap.items`, and the litellm container boots without
further action. If you see `ImportError: Could not import guardrail from
corp_llm_gateway.bootstrap` at litellm startup, you're on a chart version
from before this fix.

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

## Developer laptop: Claude Code on an Anthropic subscription

This runs `claude` through the gateway with **no `ANTHROPIC_API_KEY` anywhere** —
the developer's own Max/Pro OAuth token pays for the upstream call.

It runs on the `anthropic-oauth` docker-compose overlay only. The Helm chart
above cannot serve it: its litellm ConfigMap routes `"*"` to the corp vLLM and
has no `anthropic/` route at all, so litellm's Anthropic OAuth branch is never
reached. See `configuration.md` for the full reason and the production-compose
case.

1. **Start the overlay** (it sets `CORP_LLM_FORWARD_ANTHROPIC_AUTH=1` and swaps
   in an Anthropic-only litellm config):

   ```bash
   cp -n .env.demo.example .env.demo     # CORP_LLM_ENDPOINT = the sanitization helper

   grep LITELLM_MASTER_KEY .env.demo     # must print nothing — see the note below

   docker compose \
     -f docker-compose.demo.yml \
     -f docker-compose.anthropic-oauth.yml \
     up -d --build redis postgres litellm

   curl -fsS http://127.0.0.1:4000/health/liveliness
   ```

2. **Point Claude Code at it.** Export your subscription token, then source the
   profile's env snippet — it is the single place the corp-identity header layout
   is written, and it unsets `ANTHROPIC_API_KEY` so a leftover key cannot shadow
   the subscription:

   ```bash
   export ANTHROPIC_AUTH_TOKEN='sk-ant-oat...'
   source docker/anthropic-oauth/claude-env.sh
   claude
   ```

   `ANTHROPIC_AUTH_TOKEN` makes `claude` send `Authorization: Bearer <token>`;
   the gateway lifts it onto the upstream Anthropic call. Corp identity travels
   separately on `X-Corp-Auth` and is stripped before egress, so the two
   credentials never collide.

3. **Stop just this profile's stack:**

   ```bash
   docker compose \
     -f docker-compose.demo.yml \
     -f docker-compose.anthropic-oauth.yml \
     stop litellm redis postgres
   ```

Notes:

- Only `sk-ant-oat…` OAuth tokens are accepted. A plain `sk-ant-api…` key is
  rejected with `401 E_PROVIDER_AUTH`: litellm would send it as `x-api-key`
  while the inbound `Authorization` is still merged in, putting two competing
  auth schemes on one upstream request.
- `401 E_MISSING_TOKEN` instead means `X-Corp-Auth` never arrived — check
  `echo "$ANTHROPIC_CUSTOM_HEADERS"` in the shell you launched `claude` from.
- The overlay deliberately sets no `LITELLM_MASTER_KEY`. With one, litellm
  consumes the inbound `Authorization` as a virtual key and rejects the request
  before `pre_call` runs — so there are also no litellm virtual keys, and no
  native budget or rate-limit enforcement, on this route.
- **`cp -n` keeps an existing `.env.demo`.** If you already have one from an
  older stack and it carries `LITELLM_MASTER_KEY`, the copy step above is a
  no-op and the demo stack passes that key straight into the litellm container
  via `env_file:`. The container then **refuses to start** and logs
  `invalid gateway configuration: - LITELLM_MASTER_KEY is set while a
  subscription-auth bridge is on …`. Symptom: `docker compose up` never becomes
  healthy and `curl http://127.0.0.1:4000/health/liveliness` gets a connection
  refused. Fix: delete the line from `.env.demo` (or re-copy from
  `.env.demo.example`) and bring the stack up again. The boot-time refusal is
  deliberate — without it you would instead get an unexplained `401` on every
  `claude` request. **Blanking the line is not enough**: `LITELLM_MASTER_KEY=`
  with no value is still a set master key to litellm (it keeps the empty string
  and enables proxy auth for any non-`None` value), so the gateway refuses that
  too. Delete the line.
