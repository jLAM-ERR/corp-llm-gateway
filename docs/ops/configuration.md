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

> **`CORP_METRICS_EXPORTER` is not shipped yet.** The metrics module (plan task
> B4) and its exporter-selection key are pending. `/metrics`, the
> `ServiceMonitor`, and the alert series (`corp_llm_gateway_blocked_requests_total`,
> `gateway_failure`) are referenced by the chart but not emitted until B4 lands.
> Do not set this key — it has no reader today.

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
| `CORP_LLM_LOG_LEVEL` | log level | `INFO` | no |

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

**Supported on the `anthropic-oauth` docker-compose overlay only.** Enabling it
anywhere else risks handing the developer's subscription token to the wrong
provider:

| Deployment | Why not |
|-----|-----|
| Helm chart | Its litellm ConfigMap routes `"*"` to the corp vLLM and has no `anthropic/` route, so litellm's OAuth branch is unreachable — and a `claude-…` alias on that wildcard would carry the token to the corp vLLM. |
| Production compose | `Authorization` there is already spoken for by litellm virtual keys, so the OAuth token has no header to travel on. Picking a second header is a governance decision that has not been made. |

The guardrail does check that the request looks Anthropic-routed, but it reads
only the **client-visible model alias**; litellm resolves the real deployment
after the hook runs. That check is defence-in-depth, not a routing guarantee. The
binding control is the deployment shape: a litellm config with no `model_name:
"*"` catch-all and no non-`anthropic/` route, which is what
`docker/anthropic-oauth/litellm-config.yaml` ships and
`tests/test_anthropic_oauth_profile.py` pins.

Both unsupported cases are blocked on the same open decision — which header
carries the litellm virtual key and which carries the OAuth token — not on
missing code.

Consequence to accept knowingly: that overlay has no litellm virtual keys, so
**subscription auth and native budget / rate-limit governance are mutually
exclusive today**. A deployment that needs both has to wait for the header-layout
decision.

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

### Providers (`providers/registry.py`)

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_ALLOW_V2_PROVIDERS` | allow non-v1 providers (Bedrock/Gemini/Azure) | `0` | no |

v1 allows `anthropic` / `openai` (upstream) and `corp-vllm` (oracle); any other
name is refused unless this is `1`.

### Test-data allowlist / demo

| Key | Purpose | Default | Required |
|-----|---------|---------|----------|
| `CORP_LLM_TESTDATA_ALLOWLIST` | inline never-redact test values | `""` | no |
| `CORP_LLM_TESTDATA_ALLOWLIST_FILE` | never-redact test-values file | `""` | no |
| `DEMO_TEAM_TOKEN` | demo-stack team token (docker compose) | `demo-team-token` | no |

### Nested tables (file-only, `config.get_table`)

`[extensions.<kind>.<name>]` and `[providers.<name>]` have no env-var form (env
carries scalars only). See `config.example.toml` for the shapes. `api_version`
in an extension table must match the core `EXTENSION_API_VERSION` or startup
refuses it (fail-closed).

## Keys read outside `settings.py`

These are read directly at call sites and are **not** in the `KEYS` registry
(a tracked follow-up to register them). They are real and honored — document and
set them, but note they are not covered by `config check`'s required/choice
validation yet:

- `CORP_ENV` — `config.py` (prod marker; F9 SSL guard)
- `CORP_GATEWAY_OIDC_AUDIENCE`, `CORP_GATEWAY_OIDC_ISSUER` — `auth/rbac.py`
- `CORP_PROFILE_REQUIRE_SIGNATURE` — `profiles/manifest.py` (gated no-op)
- `CORP_LLM_GATEWAY_CONFIG_FILE` — `config.py` (selects the config file; env-only
  by design, so it is not itself a config-file key)
