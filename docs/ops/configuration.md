# Configuration reference

Every config key the gateway reads. The authoritative registry is
`src/corp_llm_gateway/settings.py` (`KEYS`); this doc mirrors it plus the handful
of keys read directly at call sites that are not yet in that registry (flagged
below).

## Resolution order

Each key resolves through, in order:

1. environment variable
2. the TOML file at `$CORP_LLM_GATEWAY_CONFIG_FILE` → `~/.corp-llm-gateway/config.toml`
   → `/etc/corp-llm-gateway/config.toml` (first that exists)
3. the caller default

`gateway-admin config check` validates the resolved set at startup and fails
fast (see `admin-cli.md`). `config.example.toml` in the repo root is a
copy-paste template with every scalar key.

## Production must-set (security-relevant)

These change security behavior. Set them for any real deploy; several are not
templated by the Helm chart yet (inject via the Secret map or a mounted
`config.toml` — see `install.md`).

| Key | Set to | Why | In `settings.py`? |
|-----|--------|-----|-------------------|
| `CORP_LLM_ENDPOINT` | `https://corp-llm.corp.lan/v1` | Required. Unset → non-routable placeholder → lazy 503 on the first oracle call. | yes (required) |
| `CORP_LLM_REQUIRE_NER` | `1` | Off (default) → NER fails **open**: a self-disabled NER engine returns no findings and a PERSON/ORG can egress. On → 503 `E_NER_UNAVAILABLE` (fail-closed, F2). | yes |
| `CORP_ENV` | `prod` | Enables the F9 guard that refuses `SSL_VERIFY=false` on the raw-content oracle call. | **no** — read in `config.py`; follow-up to register |
| `CORP_GATEWAY_OIDC_AUDIENCE` | your `aud` | Required for RS256 operator-token verification. Missing → every RBAC-gated mutation is denied. | **no** — read in `auth/rbac.py`; follow-up to register |
| `CORP_GATEWAY_OIDC_ISSUER` | your `iss` | Same as above (`iss` claim). | **no** — read in `auth/rbac.py`; follow-up to register |
| `CORP_GATEWAY_OIDC_KEY` | RS256 public key | The verification key. Empty key → RBAC fails closed (denies). | yes |
| `CORP_LLM_OVERSIZE_POLICY` | `fail-closed` (default) | A >100 KB text leaf used to egress unsanitized (F1). Default now rejects it. | yes |
| `CORP_AUDIT_SINK` | `stdout` (prod fans out via Vector) | Selects the audit sink kind. `langfuse` makes three Langfuse keys required. | yes |
| `CORP_LLM_ORACLE_TRIGGER` | `gazetteer_hit` (default) | When the conditional oracle runs. Widen to `any_local_finding` to backstop local misses (latency cost). | yes |
| `CORP_PROFILE_REQUIRE_SIGNATURE` | leave unset | Gated no-op: setting it fails profile load closed (no PKI yet). | **no** — read in `profiles/manifest.py` |

> **`CORP_METRICS_EXPORTER` defaults to `noop`, which emits nothing.** The
> exporter is selected by that key (`noop` | `prometheus`); only `prometheus`
> renders `corp_llm_gateway_blocked_requests_total` and `gateway_failure` at
> `/metrics` for the `ServiceMonitor` to scrape, and it needs the `metrics`
> extra (`prometheus-client`). One process-wide exporter is shared by the
> guardrail, the route gate and the `/metrics` endpoint (`get_exporter()`), so
> a block counted anywhere is visible on the same scrape.

## Full key list

### Laptop CLIs (`corp-llm-gateway status` / `-proxy`)

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_GATEWAY_URL` | gateway base URL | `https://gateway.corp.lan` | no |
| `CORP_GATEWAY_TOKEN_FILE` | corp token path | `~/.corp-llm-gateway/token` | no |
| `CORP_GATEWAY_LATEST_URL` | latest-version check URL | (internal VERSION URL) | no |

### corp-LLM oracle

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_LLM_ENDPOINT` | corp vLLM base URL (`…/v1`) | — | **yes** |
| `CORP_LLM_MODEL` | oracle model name | `GLM-5.1-AWQ` | no |
| `CORP_LLM_AUTH_TOKEN` | legacy oracle bearer (read by `gateway-admin`) | `""` | no |

### Detection pipeline

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_LLM_RULES_DIR` | per-team `replace.md` dir | `/etc/corp-llm-gateway/rules` | no |
| `CORP_LLM_LOCAL_FIRST` | enable the local-first cascade | `1` | no |
| `CORP_LLM_GAZETTEER` | enable the gazetteer detector | `1` | no |
| `CORP_LLM_BLOCK_PAYLOADS` | Stage 0 payload classifier | `1` | no |
| `CORP_LLM_DLP_GUARD` | Stage 5 DLP egress guard | `1` | no |
| `CORP_LLM_DLP_CANARIES` | comma-separated canary regexes | `""` | no |
| `CORP_LLM_OVERSIZE_POLICY` | `fail-closed` \| `chunk` \| `deliver-flag` (F1) | `fail-closed` | no |
| `CORP_LLM_OVERSIZE_DELIVER_TEAMS` | teams allowed the `deliver-flag` path | `""` | no |
| `CORP_LLM_REQUIRE_NER` | fail closed when NER absent (F2) | `0` | prod: **yes** |
| `CORP_LLM_ORACLE_TRIGGER` | `gazetteer_hit` \| `any_local_finding` \| `sampled:<pct>` \| `always` (F3) | `gazetteer_hit` | no |
| `CORP_LLM_ORACLE_ENABLED` | call the corp-LLM oracle at all. `1` **requires** `CORP_LLM_ENDPOINT`; `0` **and** `CORP_LLM_LOCAL_FIRST=0` is a boot refusal (`NO_OP_SANITIZER_MESSAGE`) | `0` on the compose stack | no |
| `CORP_LLM_LOG_LEVEL` | log level | `INFO` | no |

### Corp NER service (remote detector)

Off by default. Not to be confused with `CORP_LLM_REQUIRE_NER`, which governs the
**in-process** RU/EN engines. Full walkthrough: `deployment-modes.md`.

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_NER_ENABLED` | append the remote corp NER detector to the cascade | `0` | no |
| `CORP_NER_ENDPOINT` | corp NER **base** URL (client appends `/v1/analyze`) | — | **when `CORP_NER_ENABLED=1`** |
| `CORP_NER_TIMEOUT_S` | per-call timeout, under the service's own 60 s | `30` | no |
| `CORP_NER_MAX_TEXTS` | texts per batch | `256` | no |
| `CORP_NER_MAX_INPUT_CHARS` | chars per batch; a longer single text fails **closed** | `200000` | no |
| `CORP_NER_CA_BUNDLE` | PEM chain for an internal-CA NER cert | unset | no |

`CORP_NER_ENABLED=1` without `CORP_NER_ENDPOINT` is a **boot refusal**
(`build_corp_ner()` raises `ConfigError`), not a silent skip. The detector is
network-backed, so it is excluded from `CODE` segments; local detectors are not.
On the compose stack the four tuning keys are passed **by bare name** — leave
them commented rather than empty, since an empty env var beats the config file.

### Header forwarding to upstream

Transport knobs, not detection-pipeline ones — they run downstream of the
cascade above, on the already-sanitized request.

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_LLM_STRIP_INBOUND_HEADERS` | strip inbound wire headers (`Host`, `User-Agent`, `Content-Length`, `Content-Type`, ...) before forwarding to upstream | `1` | no |

`CorpLlmGuardrail._pre_call_impl` (reached via `async_pre_call_hook`) sets
`data["headers"]` **unconditionally**, independent of litellm's own
`forward_client_headers_to_llm_api` gate — litellm forwards whatever ends up
in `data["headers"]` regardless of how it got there. This flag is
load-bearing wherever the guardrail runs in front of any litellm provider,
not a no-op reserved for a future config: see `compose/README.md` "Why not
BYOK" for a wire-verified writeup. It never touches `Authorization`.

It defaults **on** because it is a correctness knob as well as a
confidentiality one: with it off, the client's `Content-Length` is forwarded
beside litellm's own, longer, sanitized body, and the provider reads the
request truncated at the client's length — a `"stream": true` cut off that way
comes back non-streamed. Any request whose sanitized body grew (a placeholder
longer than the original it replaced) is corrupted on the wire. Set it to `0`
only to reproduce that.

### Subscription-auth bridges

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_LLM_FORWARD_CHATGPT_AUTH` | lift the inbound Codex OAuth bearer onto the upstream OpenAI Responses call | `0` | no |
| `CORP_LLM_FORWARD_ANTHROPIC_AUTH` | lift the inbound `sk-ant-oat…` bearer onto the upstream Anthropic call | `0` | no |
| `LITELLM_MASTER_KEY` | litellm's own virtual-key switch — **not** a gateway knob; registered only so `config check` can refuse it next to a bridge | unset | no |

Both bridges consume the **same** inbound `Authorization` bearer, so they are
**mutually exclusive**: setting both refuses to boot (`build_guardrail()` raises)
and fails `gateway-admin config check` with one shared message. The runtime check
matters because compose / demo / bare-litellm boots skip `settings.validate()`
entirely. Selecting a bridge per provider is a v2 follow-up.

`LITELLM_MASTER_KEY` is refused the same way while **either** bridge is on. With
a master key set, litellm reads the inbound `Authorization` as one of its own
virtual keys and answers `401` before `pre_call` runs, so the bridge could never
see the developer's token. **Presence** is what counts, not truthiness: litellm
keeps a blank `LITELLM_MASTER_KEY=` and enables proxy auth for any value that is
not unset, so remove the line rather than blanking it. A master key with both
bridges off is perfectly normal.

`CORP_LLM_FORWARD_ANTHROPIC_AUTH` accepts only `sk-ant-oat…` OAuth tokens;
anything else is `401 E_PROVIDER_AUTH`. A plain `sk-ant-api…` key would make
litellm emit `x-api-key` while the inbound `Authorization` is still merged in —
two competing auth schemes on one upstream request.

With the bridge on, `pre_call` also drops `metadata` and a top-level `user` from
the request. Both egress to Anthropic unsanitized otherwise, on the pass-through
and chat-completions routes alike. litellm's own accounting metadata is lost on
this route as a result — acceptable here because the route ships without virtual
keys and therefore without spend tracking. See [`../security.md`](../security.md)
§13 for the full forward/do-not-forward list.

**Supported on the production compose stack with `docker-compose.oauth.yml`, and
on the demo `anthropic-oauth` overlay.** Enabling it anywhere else risks handing
the developer's subscription token to the wrong provider:

| Deployment | Why not |
|-----|-----|
| Helm chart | Its litellm ConfigMap routes `"*"` to the corp vLLM and has no `anthropic/` route, so litellm's OAuth branch is unreachable — and a `claude-…` alias on that wildcard would carry the token to the corp vLLM. |

The guardrail does check that the request looks Anthropic-routed, but it reads
only the **client-visible model alias**; litellm resolves the real deployment
after the hook runs. That check is defence-in-depth, not a routing guarantee. The
binding control is the deployment shape: a litellm config with no `model_name:
"*"` catch-all and no non-`anthropic/` route, which is what
`docker/anthropic-oauth/litellm-config.yaml` ships and
`tests/test_anthropic_oauth_profile.py` pins.

Consequence to accept knowingly: there are no litellm virtual keys, so no native
budget / rate-limit governance. That is by design: litellm's management surface,
`/key/*` included, is refused at the route gate (`../security.md` §14), and
API-key mode survives only as a test posture.

Operationally, note that litellm keeps one deployment per distinct per-request
`api_key` — raw value included — in `Router.model_list` for the lifetime of the
proxy process, with no eviction. Restarting the process is the only way to clear
them. This applies to the Codex bridge as well; see
[`../security.md`](../security.md) §13.

### Backends

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_LLM_PG_DSN` | Postgres DSN (token + team stores); unset → in-memory | — | prod: **yes** |
| `REDIS_URL` | Redis URL (mapping store / Cache B); unset → in-memory | — | prod: **yes** |

Put `sslmode` in the DSN (`?sslmode=verify-full`), not only in `PGSSLMODE` or a pg
service file, so the boot probe can classify a declined TLS upgrade: it reads the
DSN alone, and a server that declines TLS then refuses the boot (exit 78) instead
of booting with a warning.

**PgBouncer in front of Postgres.** Both gateway pools (token store, team-config
store) send `tcp_keepalives_idle=10`, `tcp_keepalives_interval=5` and
`tcp_keepalives_count=3` as startup parameters, so the server drops a backend
lost behind a partition — and the subject lock its transaction holds — in about
25 s. PgBouncer refuses unknown startup parameters: add

```
ignore_startup_parameters = tcp_keepalives_idle,tcp_keepalives_interval,tcp_keepalives_count
```

to the `[pgbouncer]` section of `pgbouncer.ini`, then reload PgBouncer. Without
it every store connection fails, so every LLM request answers 503
(`E_STORE_UNAVAILABLE` / `E_PROFILE_UNAVAILABLE`).

With issuance on, the boot's schema probe connects with the same parameters, so
the gateway does not start at all: it exits 78 and logs

```
gateway config refused: issuance schema check: Postgres/PgBouncer rejected a startup parameter; add tcp_keepalives_idle,tcp_keepalives_interval,tcp_keepalives_count to ignore_startup_parameters (StartupParameterRejectedError)
```

A gateway that booted before PgBouncer began refusing them answers issuance with
503 `E_ISSUE_STORE_UNAVAILABLE`, and its readiness turns 503 with
`postgres_error:StartupParameterRejectedError`: readiness connects the way the
stores do, with the same startup parameters.

### TLS to corp-LLM

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_LLM_CA_BUNDLE` | PEM CA bundle path; verify corp-LLM TLS against it | — | no (prod: recommended) |
| `SSL_VERIFY` | `false` disables corp-LLM TLS verification | `true` | no |

`CORP_LLM_CA_BUNDLE` takes precedence over `SSL_VERIFY`. With `CORP_ENV=prod`,
`SSL_VERIFY=false` raises at startup (F9) — set a CA bundle instead.

### corp-LLM auth provider (`auth/factory.py`)

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_LLM_AUTH_PROVIDER` | `noop` \| `bearer` \| `mtls` \| `oidc` \| `apikey` | `noop` | no |
| `CORP_LLM_BEARER_TOKEN` | bearer token | — | when provider=`bearer` |
| `CORP_LLM_MTLS_CERT` / `CORP_LLM_MTLS_KEY` | client cert / key | — | when provider=`mtls` |
| `CORP_LLM_OIDC_ISSUER` / `CORP_LLM_OIDC_CLIENT_ID` / `CORP_LLM_OIDC_CLIENT_SECRET` | OIDC creds | — | when provider=`oidc` |
| `CORP_LLM_API_KEY_HEADER` | api-key header name | `X-Api-Key` | no |
| `CORP_LLM_API_KEY` | api key | — | when provider=`apikey` |

corp-LLM is auth-less today, so `noop` is the working default. Switching to real
auth is config-only.

### Audit sink (`audit/factory.py`)

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_AUDIT_SINK` | `stdout` \| `langfuse` \| `list` | `stdout` | no |
| `CORP_LANGFUSE_URL` | Langfuse URL | — | when sink=`langfuse` |
| `CORP_LANGFUSE_PUBLIC_KEY` / `CORP_LANGFUSE_SECRET_KEY` | Langfuse keys | — | when sink=`langfuse` |

### Operator RBAC (`auth/rbac.py`)

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_GATEWAY_RBAC` | enforce the `gateway:operator` claim; `0` bypasses (dev) | `1` | no |
| `CORP_GATEWAY_OIDC_KEY` | RS256 public verification key | `""` | prod: **yes** |
| `CORP_GATEWAY_OIDC_AUDIENCE` | expected `aud` (read directly; not in `settings.py`) | `""` | prod: **yes** |
| `CORP_GATEWAY_OIDC_ISSUER` | expected `iss` (read directly; not in `settings.py`) | `""` | prod: **yes** |
| `CORP_GATEWAY_ADMIN_TOKEN` | operator JWT when `--token` is omitted | `""` | no |
| `CORP_GATEWAY_OIDC_ALG` | **ignored** — verification is pinned to RS256 (F11) | `RS256` | no |

`CORP_GATEWAY_OIDC_ALG` is still in the registry but no longer honored: RBAC
verification is RS256-only. See `upgrade.md` for the HS256 breaking change.

### Developer token issuance (`POST /internal/issue-token`)

`scripts/install.sh` signs the developer in to Keycloak (device flow) and trades
the access token here for a corp token. Unset `CORP_GATEWAY_ISSUE_OIDC_ISSUER`
turns the route off (a local 404); set, the rest is validated by `config check`
and again at boot. Keycloak setup: `install.md`. Bounds and status codes:
[`../security.md`](../security.md) §14.

| Key | Purpose | Default | Range / required |
|-----|---------|---------|------------------|
| `CORP_GATEWAY_ISSUE_OIDC_ISSUER` | Keycloak realm URL (`https://…/realms/<realm>`); unset disables issuance | `""` | HTTPS when `CORP_ENV` is prod |
| `CORP_GATEWAY_ISSUE_OIDC_AUDIENCE` | `aud` the access token must carry | `""` | **required** with the issuer; must differ from `CORP_GATEWAY_OIDC_AUDIENCE` |
| `CORP_GATEWAY_ISSUE_OIDC_CLIENT_ID` | the device-flow client; the token's `azp` must equal it | `""` | **required** with the issuer |
| `CORP_GATEWAY_ISSUE_OIDC_JWKS_URL` | JWKS URL | `{issuer}/protocol/openid-connect/certs` | HTTPS when `CORP_ENV` is prod |
| `CORP_GATEWAY_ISSUE_OIDC_TEAM_CLAIM` | claim holding the groups | `groups` | — |
| `CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP` | ordered group → `team_id` TOML table; first mapped group wins | — | **required** with the issuer; **config file only** (env carries scalars) |
| `CORP_GATEWAY_ISSUE_OIDC_USER_CLAIM` | claim used as `user_id` (falls back to `sub`) | `preferred_username` | — |
| `CORP_GATEWAY_ISSUE_TOKEN_TTL_DAYS` | lifetime of an issued corp token | `30` | 1-3650 |
| `CORP_GATEWAY_ISSUE_MAX_ACTIVE` | live tokens per `(iss, sub)`; the oldest is revoked past it | `2` | 1-100 |
| `CORP_GATEWAY_ISSUE_MIN_INTERVAL_SECONDS` | minimum gap between issuances per `(iss, sub)`; revoking does not reset it | `600` | 1-2592000 |
| `CORP_GATEWAY_ISSUE_MAX_INFLIGHT` | concurrent issuance requests per pod (429 `E_ISSUE_INFLIGHT` past it) | `4` | 1-1000 |
| `CORP_GATEWAY_ISSUE_RATE_PER_MINUTE` | issuance requests per minute per pod (429 `E_ISSUE_THROTTLED` past it) | `30` | 1-100000 |
| `CORP_GATEWAY_ISSUE_STORE_TIMEOUT_SECONDS` | one bound over verifier, team lookup and token store (503 `E_ISSUE_STORE_TIMEOUT` past it) | `10` | 5-300 |

Issuance **requires** `CORP_LLM_PG_DSN` (the per-subject policy serialises across
replicas in Postgres), the `oidc` and `postgres` extras, and a `corp_tokens`
table carrying the issuance columns — re-run `tokens/schema.sql` first
(`upgrade.md`). `CORP_LLM_CA_BUNDLE`, when set, is also the CA the JWKS fetch
verifies Keycloak against, so it must be a readable PEM bundle.

`CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP` has no env form: it lives in the config file,
for example:

```toml
CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP = { "/devs/payments" = "payments", "/devs/core" = "core" }
```

Both deploy targets mount that file at `/etc/corp-llm-gateway/config.toml` and
set `CORP_LLM_GATEWAY_CONFIG_FILE` to it. Helm renders it from the `issuance.*`
values (`teamMap` is an ordered list of `{group, team}`; `install.md`,
"Developer onboarding"). Compose mounts `compose/gateway/config.toml` through
the `docker-compose.issuance.yml` overlay (`compose/README.md`, "Developer token
issuance"). `gateway-admin token issue` stays as break-glass.

Every mapped `team_id` must already exist (`gateway-admin team create`); an
unknown one answers 403 `E_ISSUE_UNKNOWN_TEAM`.

### Server entrypoint (`asgi.py` / `serve.py`)

`python -m corp_llm_gateway.serve` is the only supported launch command — it is
the image `ENTRYPOINT`, and compose and Helm run it. The `litellm` CLI and
`litellm.proxy.proxy_server:app` must never be the served target again: both
serve litellm's routers with no route gate in front. See
[`../security.md`](../security.md) §14.

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_LLM_LITELLM_CONFIG` | path to litellm's proxy config YAML | `/etc/litellm/config.yaml` | no |
| `CORP_LLM_SERVE_HOST` | address uvicorn binds | `0.0.0.0` | no |
| `CORP_LLM_SERVE_PORT` | port uvicorn binds | `4000` | no |
| `CORP_LLM_ALLOW_LITELLM_DEBUG` | **test-only**: arm even with litellm's DEBUG output on (see below); refused when `CORP_ENV` is prod/production | `0` | no |

The entrypoint refuses to start (**exit 78**, `EX_CONFIG`) when
`CORP_LLM_LITELLM_CONFIG` is missing, not a file, not named `.yaml`/`.yml`,
unreadable, empty, not a YAML mapping, or when it configures
`general_settings.pass_through_endpoints` or `general_settings.database_url`.
litellm's own lifespan skips a missing config **in silence**, which starts the
proxy with no guardrail callback at all — that is the fail-open this check
closes. `gateway-admin config check` applies the same content checks, but a
config file that is simply absent is not a `config check` problem (laptops
mount none).

The shipped litellm configs (compose `config.yaml` / `config.oauth.yaml`, Helm
`configmap-litellm.yaml`) pin `general_settings.supported_db_objects: ["models"]`:
litellm loads only models from its database, never `policies` or `guardrails`
(without the key it loads every object type). A policy row there could put a
litellm pipeline around the gateway's guardrail; the route gate already refuses
every `/policies*` and `/guardrails/*` route, a request body with a top-level
`policies` key (403 `E_ROUTE_BLOCKED`, `block_reason=route_gate_body_policies`) and a
body that is not JSON (415, `route_gate_body_not_json`), so the pin is defence in depth. Keep it in any config of your own.

**Two DSN sources litellm's CLI reads are deliberately NOT carried over.** The
entrypoint's Prisma schema step reads `DATABASE_URL` and `DIRECT_URL` from the
environment only:

- `general_settings.database_url` in litellm's YAML — **refused** at boot
  (exit 78). The CLI exported it to `DATABASE_URL` before its Prisma sequence;
  this entrypoint does not, so litellm would connect to a database whose schema
  was never set up.
- the `DATABASE_HOST` / `DATABASE_USERNAME` / `DATABASE_PASSWORD` /
  `DATABASE_NAME` / `DATABASE_SCHEMA` composition (litellm's
  `proxy/utils.py`) — **not read**. Same failure mode, no boot-time signal, so
  set `DATABASE_URL` instead.

### Route gate (`route_gate/`)

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH` | extra PASSTHROUGH routes, `"METHOD /path"`, comma- or newline-separated | `""` | no |

This is the gate's **only** knob, and it can only widen the table with
PASSTHROUGH entries. It can never admit a route as REWRITTEN, never override a
REFUSE, and there is no off switch. A malformed item is a boot-time config
problem (`validate()` rejects it), not a silent widening.

Use it for an operator route that provably sends no user text anywhere — a
sidecar status endpoint, an extra probe path. Each item admits **one exact
`(method, path)` pair**; there is no prefix form, on purpose, so widening cannot
open a tree by accident.

```
CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH="GET /internal/ops-status"
```

That exactness is also why litellm's admin **UI** cannot be re-opened with this
key: `/ui`, `/swagger`, `/docs` and `/openapi.json` are mounted sub-apps and
FastAPI internals, each serving many paths the `ast` collector cannot see, so
they get no table entry and are refused as unlisted. The JSON admin API
(`/key/*`, `/team/*`, …), spend, login/SSO, the public catalogue and the
non-probe `/health/*` routes are pinned route by route as **REFUSE**, and an
extra naming any refused route — or a `HEAD` on a path whose `GET` is refused —
is a config error: `config check` reports it and the gateway exits 78 at boot.
There is no supported litellm admin surface; operators use `gateway-admin`
(`docs/security.md` §14, "The management surface is refused").

`gateway-admin config check --routes` prints the effective table — row counts
per verdict, the REWRITTEN routes, and every extra (see `admin-cli.md`).

### In-flight cap (`route_gate/inflight.py`)

Per pod. Only an armed REWRITTEN request counts; the body is read and bounded
before a slot is taken. Sizing: `capacity.md`. Behaviour:
[`../security.md`](../security.md) §14.

| Key | Purpose | Default | Range |
|-----|---------|---------|-------|
| `CORP_LLM_MAX_INFLIGHT` | concurrent rewritten requests, a slot held for the whole stream; 429 `E_CAPACITY` past it | `64` | 0-10000; `0` = off, **refused** when `CORP_ENV` is prod/production |
| `CORP_LLM_CANCEL_GRACE_SECONDS` | unwind budget after a client disconnect; a slot is held at most 2 × this after one | `5` | above 0, at most 60 |
| `CORP_LLM_BODY_READ_SECONDS` | time for the whole body to arrive, before a slot is taken; 408 `E_BODY_TIMEOUT` past it | `30` | above 0, at most 300 |
| `CORP_LLM_MAX_DRAINING` | requests reading a body at once; 429 `E_CAPACITY` unread past it | 4 × `CORP_LLM_MAX_INFLIGHT` | at least `CORP_LLM_MAX_INFLIGHT`, at most 40000; `0` only when the cap is off |
| `CORP_LLM_MAX_DRAINING_BYTES` | body bytes buffered at once (being read, or held for an admitted request until it ends); 429 `E_CAPACITY` past it — the memory knob | `536870912` (512 MiB) | 26214400 (25 MiB) to 17179869184 (16 GiB) |

The Helm chart (`values.yaml` `config:`) and the compose stack pass all five;
`CORP_LLM_MAX_DRAINING` is left empty/unset there so it follows the cap. The
25 MiB per-body cap (422 `oversize:blocked`) is fixed, not a key. litellm's own
`global_max_parallel_requests` does nothing in this deployment.

litellm's logging worker is started once in the lifespan, outside any request:
started lazily inside the first request, a disconnect of that request would
cancel it and stop every later success/failure callback, audit included.

### Providers (`providers/registry.py`)

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_ALLOW_V2_PROVIDERS` | allow non-v1 providers (Bedrock/Gemini/Azure) | `0` | no |

v1 allows `anthropic` / `openai` (upstream) and `corp-vllm` (oracle); any other
name is refused unless this is `1`.

### Profiles and exporters

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_PROFILE_ROOT` | profile bundle root dir; unset → the shipped defaults (`profiles.md`) | `""` | no |
| `CORP_PROFILE_REQUIRE_SIGNATURE` | fail closed unless a profile is signed (gated no-op, no PKI yet) | `0` | no |
| `CORP_METRICS_EXPORTER` | `noop` \| `prometheus` (see the note at the top) | `noop` | no |
| `CORP_TRACING_EXPORTER` | reserved; `noop` only | `noop` | no |

### Test-data allowlist / demo / dev

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_LLM_TESTDATA_ALLOWLIST` | inline never-redact test values | `""` | no |
| `CORP_LLM_TESTDATA_ALLOWLIST_FILE` | never-redact test-values file | `""` | no |
| `DEMO_TEAM_TOKEN` | demo-stack team token (docker compose) | `demo-team-token` | no |
| `CORP_LLM_DEV_TEAM_TOKEN` | DEV-ONLY: seeds an in-memory `X-Corp-Auth` token for team `local-dev`; ignored with a warning when `CORP_LLM_PG_DSN` is set or `CORP_ENV` is prod | `""` | no |

### Nested tables (file-only, `config.get_table`)

`[extensions.<kind>.<name>]` and `[providers.<name>]` have no env-var form (env
carries scalars only). See `config.example.toml` for the shapes. `api_version`
in an extension table must match the core `EXTENSION_API_VERSION` or startup
refuses it (fail-closed).

## What the entrypoint does with bad config

`python -m corp_llm_gateway.serve` checks config before it serves. `gateway-admin
config check` runs the same resolvers, so a clean `config check` means the boot
will not refuse on these (it does not probe the `corp_tokens` schema — only the
boot does).

**Exit 78 (`EX_CONFIG`), no traffic served:**

- litellm's config missing, unreadable, not a YAML mapping, or configuring
  `pass_through_endpoints` or `general_settings.database_url`;
  `CORP_LLM_SERVE_PORT` not a port;
- issuance on (`CORP_GATEWAY_ISSUE_OIDC_ISSUER` set) but partially configured,
  out of range, sharing the operator audience, HTTP in prod, without
  `CORP_LLM_PG_DSN`, without the `oidc` / `postgres` extras, or with an
  unreadable or non-PEM `CORP_LLM_CA_BUNDLE`;
- issuance on and Postgres refusing the DSN (credentials, database name,
  syntax), the TLS handshake (including a DSN `sslmode` the server declines),
  or `SELECT` on `corp_tokens`; `corp_tokens` without the issuance columns, or
  `corp_tokens_oidc_jti_key` missing, not UNIQUE on `oidc_jti` alone, or INVALID
  (`upgrade.md` has the remedy);
- issuance on and PgBouncer (or Postgres) rejecting the keepalive startup
  parameters: set `ignore_startup_parameters` ("Backends");
- any in-flight key out of range, or `CORP_LLM_MAX_INFLIGHT=0` in prod;
- `CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH` malformed or naming a refused route.

- `CORP_LLM_ALLOW_LITELLM_DEBUG=1` with `CORP_ENV` prod/production.

Exit 70 (`EX_SOFTWARE`), after litellm's startup, one log line
`arm refused (<problem>)` per problem (`route_gate/arm_checks.py`):

- `guardrail_absent`: litellm started without a `CorpLlmGuardrail` callback;
- `apply_guardrail`, `scan_raw_request`, `run_in_parallel`: the guardrail is set
  up so litellm would skip its pre-call hook or discard its rewrite;
- `litellm_debug_logging`, `litellm_set_verbose`: litellm's DEBUG output is on
  (`LITELLM_LOG=DEBUG`, `DETAILED_DEBUG`, `--detailed_debug`,
  `litellm_settings.set_verbose`). litellm logs the original request before any
  pre-call hook runs. `config check` reports the env vars and `set_verbose` too.
  `CORP_LLM_ALLOW_LITELLM_DEBUG=1` lets these two through outside prod, for tests.
- `response_compressor`: litellm's app carries a response-compressing middleware
  (litellm 1.101.0 adds none, and no config key turns one on, so `config check`
  has nothing to report). The gateway restores the originals in a response at
  its ASGI layer, in front of litellm's app, and passes an encoded response
  through unrestored — a compressor would hand clients placeholders.

**Warn and boot:**

- issuance on and Postgres unreachable at boot (08xxx connection errors, 57P0x
  shutdown/starting up, 53300 too many connections, a socket error): the schema
  check is skipped with a warning. Until a re-check sees the schema current,
  `/healthz/ready` answers 503 (the store, then `issuance_schema: …` naming the
  remedy) and `POST /internal/issue-token` answers **503 `E_ISSUE_SCHEMA`**.
  Readiness and issuance attempts both re-check, at most one query per 15 s
  between them, and stop once the schema passes — so after applying
  `tokens/schema.sql` the next issuance past the interval succeeds even where
  nothing polls `/healthz/ready` (compose);
- `CORP_LLM_MAX_INFLIGHT=0` outside prod: the cap is off, with a warning;
- `CORP_LLM_DEV_TEAM_TOKEN` set with a DSN or in prod: ignored, with a warning.

### Fixed bounds (not configurable)

| Bound | Value | Where |
|-------|-------|-------|
| request body per rewritten request | 25 MiB (422 `oversize:blocked`) | `route_gate/inflight.py` `MAX_BODY_BYTES` |
| `Retry-After` on 429 `E_CAPACITY` | 1 s | `route_gate/middleware.py` |
| issuance request body | 0 bytes accepted; read stops at 1 KiB / 2 s | `healthz/server.py` |
| issuance transaction | `lock_timeout` 5 s, `statement_timeout` 8 s (503 `E_ISSUE_BUSY`) | `tokens/postgres_store.py` |
| JWKS fetch | 3 s, 64 KiB, no redirects; 60 s cooldown on an unknown `kid` | `tokens/oidc_verifier.py` |
| auth lookup on the request path | 5 s per statement, 6 s overall (503 `E_STORE_UNAVAILABLE`) | `tokens/postgres_store.py`, `litellm_hook.py` |
| team-config read on the request path | 5 s (503 `E_PROFILE_UNAVAILABLE`) | `team_config/postgres_store.py` |
| cancel of an abandoned statement | 0.5 s, then the connection is terminated | `pg_session.py` `CANCEL_BUDGET_S` |
| returning a connection to the pool | 2 s + 1 s grace, then it is dropped | `pg_session.py` |
| server keepalives on pooled connections | 10 s / 5 s / 3 probes (~25 s) | `pg_session.py` |
| cancel-audit records kept for a retry | 4096 requests, counts only | `litellm_hook.py` |

## Test-only environment

Read by the test suite, never by the gateway:

| Variable | Effect |
|----------|--------|
| `CORP_TEST_PG_DSN` | Postgres the store contract and issuance race tests use; unset they skip locally, and fail when `CI=true` |
| `CORP_REQUIRE_PROXY_CAPTURE` | `1` turns the docker/image skips of the container suites into failures (set in CI) |
| `CORP_GATEWAY_IMAGE` | a prebuilt image for the route-gate container suite; unset, the suite builds `corp-llm-gateway:route-gate-test-<digest of the build inputs>` (`python -m tests.integration.gateway_image`) |
| `NO_PROXY=127.0.0.1,localhost` | needed on a machine with a system HTTP proxy, or the served-stack tests talk to the proxy instead of the local server |

## Keys read outside `settings.py`

Every `CORP_*` key the gateway reads is in the `KEYS` registry, so
`config check` validates it — `CORP_ENV`, `CORP_GATEWAY_OIDC_AUDIENCE`,
`CORP_GATEWAY_OIDC_ISSUER` and `CORP_PROFILE_REQUIRE_SIGNATURE` included. One
key is deliberately outside it:

- `CORP_LLM_GATEWAY_CONFIG_FILE` — `config.py`. It selects the config file, so
  it is read from the environment only and is not itself a config-file key. The
  Helm chart (`issuance.enabled`) and the compose issuance overlay set it.

`DATABASE_URL`, `DIRECT_URL` and `JSON_LOGS` are litellm's own variables. The
entrypoint reads them from the environment the way litellm does ("Server
entrypoint" above; `litellm_config.py`), not through the config file.
