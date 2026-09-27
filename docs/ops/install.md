# Install

How to deploy the corp LLM gateway to a corp Kubernetes cluster with Helm.

The chart is `helm/corp-llm-gateway`. It runs the gateway image (LiteLLM proxy +
the `corp_llm_gateway.bootstrap.guardrail` callback) plus a Vector log-shipper
sidecar, and mounts detection in-process — there is no separate pre-pass pod.

For **non-k8s hosts** use the compose stack in `compose/` instead. Either way,
read `deployment-modes.md` (RU: `deployment-modes.ru.md`) first: it covers the
two mutually exclusive auth modes (API keys vs subscription/OAuth) and how to
toggle the corp-LLM oracle (`CORP_LLM_ORACLE_ENABLED`) and the corp NER service
(`CORP_NER_ENABLED`).

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

### Non-secret keys: the `config:` map

Non-secret keys go in `values.config` (or a `-f` values file's `config:`): the
chart renders each entry as an env var on the gateway container **and** the
config-check initContainer. `values.yaml` already sets
`CORP_LLM_STRIP_INBOUND_HEADERS` and the five in-flight cap keys
(`CORP_LLM_MAX_INFLIGHT`, `CORP_LLM_CANCEL_GRACE_SECONDS`,
`CORP_LLM_BODY_READ_SECONDS`, `CORP_LLM_MAX_DRAINING`,
`CORP_LLM_MAX_DRAINING_BYTES`); the issuance scalars go here too. Secrets
never do. `values-prod.yaml` sets the keys below, the two OIDC ones as
placeholders the DRI must replace. See `configuration.md` for why each
matters.

| Key | Set to | Without it |
|-----|--------|------------|
| `CORP_LLM_REQUIRE_NER` | `1` | NER fails **open** in prod (PERSON/ORG can egress) |
| `CORP_ENV` | `prod` | the `SSL_VERIFY=false` guard (F9) stays off |
| `CORP_GATEWAY_OIDC_AUDIENCE` | your aud | operator RBAC cannot verify → all mutations denied |
| `CORP_GATEWAY_OIDC_ISSUER` | your iss | same |

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
   kubectl -n corp-llm-gateway rollout status deploy/gw-corp-llm-gateway
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
- `GET  /metrics` — Prometheus scrape (the series ship with the metrics module; see `configuration.md`)

- `POST /internal/issue-token` — developer token issuance (see below); a local
  404 while `CORP_GATEWAY_ISSUE_OIDC_ISSUER` is unset. Never forwarded to litellm.

Every litellm admin, auth, spend, public, UI and non-probe health route answers
`403 E_ROUTE_BLOCKED` ([`../security.md`](../security.md) §14). Probes use
`/healthz/*` only. Behind the compose HTTPS front door only `GET /healthz/live`
and `POST /internal/issue-token` of these are reachable from outside; the rest
answer on the server's loopback port (`compose/README.md`, "HTTPS front door
(nginx)").

## Developer onboarding: Keycloak issuance

Developers get their `X-Corp-Auth` corp token from `scripts/install.sh`: it signs
them in to Keycloak with the device flow (RFC 8628) and trades the access token
at `POST /internal/issue-token` for a 30-day corp token. `gateway-admin token
issue` is the break-glass path only (`admin-cli.md`).

### Keycloak realm and client

Use a client of its own — **not** the operator RBAC client whose audience is
`CORP_GATEWAY_OIDC_AUDIENCE`. The gateway refuses a token whose `aud` carries the
operator audience, and refuses to boot when the two audiences are equal.

1. **Client** (e.g. `corp-gateway-cli`): OpenID Connect, **public** (client
   authentication off — the installer holds no secret), **OAuth 2.0 Device
   Authorization Grant** enabled, standard and direct-access flows off unless
   another tool needs them. Its id is `KEYCLOAK_CLIENT_ID` on laptops and
   `CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID` on the gateway; the token's `azp` must
   equal it.
2. **Audience mapper** on that client (mapper type *Audience*, *Add to access
   token* on): included custom audience = `CORP_GATEWAY_ISSUE_OIDC_AUDIENCE`
   (e.g. `corp-gateway-issuance`).
3. **Groups mapper** (mapper type *Group Membership*, token claim name `groups`
   — or whatever `CORP_GATEWAY_ISSUE_OIDC_TEAM_CLAIM` says — *Add to access
   token* on). With *Full group path* on, the values look like
   `/devs/payments`; the keys of `CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP` must match
   the form you choose.
4. Keep the default `basic` client scope: it puts `sub` in the access token
   (Keycloak 25+). The gateway requires `exp`, `iat`, `iss`, `aud`, `sub`,
   `jti` and `azp`, RS256 only.
5. One Keycloak group per team, each listed in the team map; every mapped
   `team_id` must already exist (`gateway-admin team create`). A developer in no
   mapped group gets 403 `E_ISSUE_NO_TEAM`; in several, the first in the map's
   order wins.

### Gateway side

Set the issuance keys (`configuration.md`, "Developer token issuance").
`CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP` is a TOML table with no env form, so both
deploy targets carry the keys in a config file mounted read-only at
`/etc/corp-llm-gateway/config.toml`:

- **Helm:** the `issuance.*` values. `enabled: true` renders them into that
  file, mounts it on the gateway and the `config-check` initContainer, and sets
  `CORP_LLM_GATEWAY_CONFIG_FILE`. `issuer`, `audience`, `clientId` and `teamMap`
  are required. `teamMap` is a list, so its order survives — the first listed
  group the user belongs to wins:

  ```yaml
  issuance:
    enabled: true
    issuer: https://keycloak.corp.lan/realms/dev
    audience: corp-gateway-issuance
    clientId: corp-gateway-cli
    teamMap:
      - group: "/devs/payments"
        team: payments
      - group: "/devs/core"
        team: core
  ```

  The chart refuses to render with a `CORP_GATEWAY_ISSUE_*` key under `config:`
  (an env var would shadow the file), and with `networkPolicy.enabled` but not
  `networkPolicy.keycloak.enabled`. A changed `issuance.*` rolls the pods.
- **Compose:** the `docker-compose.issuance.yml` overlay with
  `compose/gateway/config.toml` (`compose/README.md`, "Developer token
  issuance").

Before the first issuance:

- Postgres is required (`CORP_LLM_PG_DSN`), and `tokens/schema.sql` must have
  been re-run on it (`upgrade.md`); the boot exits 78 otherwise;
- the gateway fetches the realm JWKS, so on Helm with the NetworkPolicy on set
  `networkPolicy.keycloak.{enabled,cidr,port}` (443 by default); an internal CA
  goes in `caBundle` (`CORP_LLM_CA_BUNDLE`);
- run `gateway-admin config check` — it reports a partial issuance config, the
  missing extras and a bad CA bundle; the `corp_tokens` schema is checked at
  boot only.

### The developer installer

`scripts/install.sh` reads:

- `KEYCLOAK_ISSUER` + `KEYCLOAK_CLIENT_ID` — the realm URL and the public client.
  The issuer must be `https://` (plain `http` only for `localhost` /
  `127.0.0.1`); a trailing slash is dropped; the client id is required whenever
  the issuer is set. Export both:

  ```bash
  curl -fsSL https://raw.githubusercontent.com/jLAM-ERR/corp-llm-gateway/main/scripts/install.sh \
    | KEYCLOAK_ISSUER='https://keycloak.corp.lan/realms/corp' KEYCLOAK_CLIENT_ID='corp-gateway-cli' bash
  ```

  Without `KEYCLOAK_ISSUER` the installer writes the rc block only, issues no
  token, prints how to get one and exits 0.
- `ANTHROPIC_AUTH_TOKEN` — the developer's subscription token; it drives the
  smoke test at the end, which is skipped with a message when it is unset.
- `CORP_GATEWAY_URL` (default `https://gateway.corp.lan`). On a compose
  deployment it is the front door's origin once a profile is on:
  `https://gateway.<domain>` under `nginx`, `https://<address>:<NGINX_PORT>`
  under `nginx-ports`. The installer's `POST /internal/issue-token` goes through
  nginx, which admits it (header only, a body over 1 KiB is 413, at most
  `NGINX_ISSUE_RATE` calls per minute per address) and hands it to the gateway.
  `corp-llm-gateway status` needs only `GET /healthz/live`, which nginx admits
  too. The installer has no CA option of its own: for a certificate from a CA
  the laptop does not trust, its `curl` reads `CURL_CA_BUNDLE`, and
  `corp-llm-gateway status` reads `SSL_CERT_FILE`.
- `CORP_GATEWAY_TOKEN_FILE` (default `~/.corp-llm-gateway/token`) — must be an
  absolute path with no quote or newline; the token is written mode 0600, and
  when the path is a symlink the installer writes through it to its target.

A second run within `CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS` (default 10 min)
is refused with 403 `E_ISSUE_RATE`; a third live token revokes the oldest
(`CORP_GATEWAY_ISSUE_MAX_ACTIVE`, default 2). Revoke a developer's tokens with
`gateway-admin token revoke --user <preferred_username>`.

## Rollback

`helm rollback gw <revision>` (`helm history gw`; Helm keeps the last 10). See
`runbook.md`.

## Developer laptop: Claude Code on an Anthropic subscription

This runs `claude` through the gateway with **no `ANTHROPIC_API_KEY` anywhere** —
the developer's own Max/Pro OAuth token pays for the upstream call.

The steps below use the **demo** overlay, which is the quickest way to try it on
a laptop. For a server, use the production stack in subscription mode instead —
`compose/` + `docker-compose.oauth.yml`, which keeps the Postgres token store and
the audit pipeline the demo stack does not have. See
[`deployment-modes.md`](deployment-modes.md) "Mode B"; everything about the
client side below applies unchanged to it.

**Helm does not support this route.** It routes `"*"` to the corp vLLM and has no
`anthropic/` route at all, so litellm's Anthropic OAuth branch is never reached —
and that wildcard is the one shape where a `claude-…` alias could carry the
subscription token to the wrong upstream.

There is no litellm virtual-key governance (budgets, rate limits, quotas) in any
stack: litellm's management surface, `/key/*` included, is refused at the route
gate, so no virtual key can be issued ([`../security.md`](../security.md) §14).
API-key mode survives only as a test posture. See `configuration.md` for the operator view and
[`../security.md`](../security.md) §13 for what the bridge does and does not
forward.

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
- **`CORP_LLM_FORWARD_CHATGPT_AUTH` must be off.** Both bridges read the same
  inbound `Authorization` bearer, so they are mutually exclusive: with both set
  the gateway refuses to boot and `gateway-admin config check` fails. If you have
  the Codex flag in `~/.corp-llm-gateway/config.toml` or in your environment,
  turn it off before starting this overlay — the overlay itself only sets the
  Anthropic one.
- The overlay deliberately sets no `LITELLM_MASTER_KEY`. With one, litellm
  consumes the inbound `Authorization` as a virtual key and rejects the request
  before `pre_call` runs — so there are also no litellm virtual keys, and no
  native budget or rate-limit enforcement, on this route.
- **Restart the proxy to clear retained tokens.** litellm treats a per-request
  `api_key` as a clientside credential and keeps one deployment per distinct
  token — raw value included — in `Router.model_list` for the lifetime of the
  process. Nothing evicts it. If a subscription token is rotated or believed
  compromised, restart the `litellm` container; that is the only mitigation. The
  same is true of the ChatGPT Codex overlay — this is not new behavior, just a
  second place it applies. Details in [`../security.md`](../security.md) §13.
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
