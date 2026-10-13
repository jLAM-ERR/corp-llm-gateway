# Deployment modes

Which providers to run, how developers authenticate, and how to turn the two
optional network dependencies (the corp-LLM oracle, the corp NER service) on and
off. Russian mirror: `deployment-modes.ru.md`.

There is one production stack, `compose/` (Helm for k8s: `install.md`). It runs
one gateway container per **provider**, and you pick the providers per host.
Every gateway runs the same production code path: `bootstrap.guardrail`, the
route gate, the Postgres token store, the Redis mapping store and the same
audit chain. What differs is the litellm route and the upstream credential.

| Provider (profile) | Service | Port on `127.0.0.1` | litellm config | Upstream credential |
|---|---|---|---|---|
| `anthropic` | `gateway-anthropic` | `GATEWAY_ANTHROPIC_PORT` (4000) | `config.anthropic.yaml`: `claude-*` → `anthropic/` | the developer's Anthropic subscription token (`sk-ant-oat…`), lifted by `CORP_LLM_FORWARD_ANTHROPIC_AUTH=1` |
| `codex` | `gateway-codex` | `GATEWAY_CODEX_PORT` (4001) | `config.codex.yaml`: `"*"` → `openai/` at `https://chatgpt.com/backend-api/codex` | the developer's ChatGPT subscription token, lifted by `CORP_LLM_FORWARD_CHATGPT_AUTH=1` |
| `corp` | `gateway-corp` | `GATEWAY_CORP_PORT` (4002) | `config.corp.yaml`: `corp-*` → `hosted_vllm/` at `CORP_LLM_ENDPOINT` | the route's own `CORP_LLM_AUTH_TOKEN` (empty for an auth-less vLLM); no bridge |

On every provider the developer sends `X-Corp-Auth: <corp token>`, the team
identity the gateway checks in `pre_call`. No provider API key is held anywhere,
and there are no litellm virtual keys.

**Mode A (litellm master key and virtual keys) was removed in 1.1.0**, together
with litellm's own database. Any `LITELLM_MASTER_KEY` refuses to boot (below).
The 1.0 "Mode B" is what the `anthropic` provider runs now. Upgrading a 1.0
host: `upgrade.md`, "1.0 → 1.1.0".

There is also a **demo-only** OAuth stack — `docker-compose.demo.yml` +
`docker-compose.anthropic-oauth.yml` at the repo root. It exists to try the
subscription bridge on a laptop and is **not** a deployment target: it runs
`_demo_guardrail`, whose token store is in memory with a hardcoded
`demo-team-token`, and it has no audit pipeline. Do not point it at a server.

---

## Picking the providers

`COMPOSE_PROFILES` in `compose/.env` is the only switch: at least one of
`anthropic`, `codex`, `corp`, comma-separated. Some developers run two at once,
for example `COMPOSE_PROFILES=anthropic,codex` for cross-review.

```
cd compose
cp .env.example .env
chmod 0600 .env
# fill in the required secrets and set COMPOSE_PROFILES, then:
docker compose up -d
```

From a laptop, against a server prepared by `scripts/deploy/bootstrap-server.sh`,
the providers come from the same line in the server's `.env`; the script has no
flag for them:

```
scripts/deploy/deploy.sh --host user@server up
```

- **No provider is a refusal.** The `profile-guard` one-shot, which `postgres`
  waits for, exits 64 and names `COMPOSE_PROFILES` when the line has no exact
  provider token (`corporate` is not `corp`), so `up` fails instead of serving
  nothing. `deploy.sh up` refuses the same stack with `no provider`.
- **`--profile` on the command line is not supported.** `profile-guard` reads
  `COMPOSE_PROFILES`, and the boot-time unit
  (`scripts/deploy/corp-llm-gateway.service`, a bare `docker compose up -d`)
  reads the same `.env`, so a deploy and a reboot start the same providers.
- **Dropping a provider:** remove it from the line and run `deploy.sh up`; it
  removes that gateway's container. A bare `docker compose up -d` does not, and
  the old gateway keeps its port.
- **The front door** (`nginx` or `nginx-ports` on the same line) proxies to one
  gateway, `NGINX_GATEWAY_UPSTREAM` (default `gateway-anthropic`), whose provider
  must be enabled, or nginx exits 64. The other providers stay on their
  loopback ports or an SSH tunnel (`compose/README.md`, "HTTPS front door").
- **There is no `--mode`.** `deploy.sh --mode` (any value) is refused, naming
  1.1.0.

## Why one gateway per provider

Both subscription bridges read the same inbound `Authorization` bearer, so one
process can run at most one of them (`build_guardrail()` refuses both,
`settings.FORWARD_AUTH_EXCLUSIVE_MESSAGE`). Each provider's litellm config is
also the binding control on where the developer's token goes:

- **`anthropic`** has one route, native `anthropic/`, with no `gpt-*`, no
  `corp-*` and no `"*"` catch-all. The guardrail gates the OAuth lift on the
  client-visible model alias, but litellm resolves the actual deployment
  *after* the hook runs, so the alias gate alone cannot prove where a request
  lands. An Anthropic-only routing table can. Do not add a second route.
- **`codex`** has one catch-all route whose `api_base` is the literal ChatGPT
  Codex URL, not an environment value, so no setting can point the token at
  another host.
- **`corp`** has one `corp-*` route to the corp vLLM and no bridge. The
  client's `Authorization` never reaches the vLLM, and neither does any other
  client header: only a subscription bridge carries client values upstream (the
  token, plus its header allowlist by exact name and value format), and `corp`
  has none (`../security.md` §13). Clients of this port still
  should not send a subscription token here; it has no use on a corp host.

The corp vLLM oracle works on every provider: it is reached through the
gateway's own HTTP client (`CORP_LLM_ENDPOINT`), not through a litellm route.

## The master key is refused

`LITELLM_MASTER_KEY` set to anything refuses to boot, with or without a bridge:
`build_guardrail()` stops the startup (exit 3) and names the cause
(`settings.MASTER_KEY_REFUSED_MESSAGE`); `gateway-admin config check` reports
it too. litellm's proxy auth is gone with Mode A; the gateway authenticates every
client with `X-Corp-Auth`. With a bridge on, a master key would also make litellm
read the inbound `Authorization` as one of its own virtual keys and answer 401
*before* `pre_call` runs, so the subscription token would never reach the bridge.
A `general_settings.master_key` or `environment_variables.LITELLM_MASTER_KEY` in a
litellm config of your own is refused at startup too (exit 78).

> **Trap:** a blank `LITELLM_MASTER_KEY=` line **counts as set** — litellm keeps
> the empty value and enables proxy auth for anything that is not `None`. Delete
> the line entirely. The production compose stack passes no `LITELLM_MASTER_KEY`
> to a gateway (each gateway's environment is an explicit list, with no
> `env_file:`), so a line in `compose/.env` never reaches one. When the refusal
> fires anyway, the key comes from the gateway's config file
> (`CORP_LLM_GATEWAY_CONFIG_FILE`, e.g. the issuance overlay's
> `gateway/config.toml`), a custom image or an environment override (a Helm
> value, a compose override file). On the demo stack it is usually a leftover key
> in a git-ignored `.env.demo`, which that stack does pick up via `env_file:`.

### What you give up, and what to close

There are **no per-developer virtual keys**, so no spend accounting through the
LiteLLM UI. Identity, sanitization and audit do not depend on them: developers
send `X-Corp-Auth: <corp token>`, which the gateway validates in `pre_call`
against the Postgres token store, and a request without a valid one is refused
(`MissingTokenError` / `InvalidTokenError`). The corp token is per developer and
works on every provider of the host (one shared `gateway` database):
`scripts/install.sh` mints it through Keycloak at `POST /internal/issue-token`
once issuance is configured (`install.md`, "Developer onboarding"), and
`gateway-admin token revoke --user` revokes it. On this compose stack issuance
is the optional `docker-compose.issuance.yml` overlay, applied to every enabled
gateway; it mounts `gateway/config.toml` with the team map (`compose/README.md`,
"Developer token issuance"). On Helm it is the `issuance.*` values (`install.md`,
"Developer onboarding"). Without it, corp tokens come from
`gateway-admin token issue` (break-glass).

**Postgres behind PgBouncer.** The gateway's pools send TCP keepalive startup
parameters; PgBouncer refuses them unless `ignore_startup_parameters` lists them
(`configuration.md`, "Backends"). Without that, every store call fails and
every LLM request answers 503; readiness connects the same way, so the pod
leaves rotation. With issuance on, the gateway does not start: it
exits 78, and the log line names `ignore_startup_parameters` and the three
parameters to list.

**Litellm's own management endpoints are refused, on every provider.** Without a
master key litellm accepts any caller as an internal user
(`user_api_key_auth.py:1622-1633` in 1.101.0), so much of `/key/*`, `/model/*`,
`/user/*`, `/policies*`, `/guardrails*` and the UI would otherwise answer anyone
who reaches the port. The route gate refuses all of them (`403
E_ROUTE_BLOCKED`) before litellm sees the request, on every path in — loopback,
SSH tunnel or the nginx front door. The LLM routes are still gated by the
gateway's `X-Corp-Auth` check in `pre_call`. See `../security.md` §14.

The front door (`COMPOSE_PROFILES=…,nginx` or `…,nginx-ports`,
`compose/README.md`, "HTTPS front door (nginx)") **additionally** answers the
whole surface with 404 at the edge, unconditionally: its exact-path allow-list
admits only `POST /v1/messages`, `POST /v1/chat/completions`,
`POST /v1/responses`, `GET /v1/models`, `GET /healthz/live` and
`POST /internal/issue-token`. That is defence in depth — the refusal above holds
without nginx — and administration stays on the SSH tunnel to each gateway's
loopback port.

---

## Toggle: the corp-LLM oracle

The oracle is the **conditional fallback** at the end of the local-first cascade
(ADR-003). It is called only on a deterministic gazetteer hit, not on every
request.

**Off (default).** `compose/.env.example` ships `CORP_LLM_ORACLE_ENABLED=0`. In
this state no oracle call is ever attempted and `CORP_LLM_ENDPOINT` is not needed
for detection.

**On.**

```
CORP_LLM_ORACLE_ENABLED=1
CORP_LLM_ENDPOINT=https://<corp-vllm-host>/v1   # required once the oracle is on
```

Three things that bite:

- **Enabling without a reachable endpoint fails requests closed on *every*
  route**, not just `corp-*` — a gazetteer hit falls through to a non-routable
  placeholder host. Point `CORP_LLM_ENDPOINT` at a live vLLM first.
- **The `corp` provider's `corp-*` route needs `CORP_LLM_ENDPOINT` regardless of this flag.**
  Without it, `corp-*` requests fail per-request rather than failing config load.
- **Do not turn off both the oracle and the local-first cascade.**
  `build_guardrail()` raises `ConfigError(NO_OP_SANITIZER_MESSAGE)` at boot if
  `CORP_LLM_ORACLE_ENABLED=0` **and** `CORP_LLM_LOCAL_FIRST=0` — the gateway
  refuses to run as a no-op sanitizer. `CORP_LLM_LOCAL_FIRST` is unset on this
  stack and defaults to `1`, so the shipped posture is safe; just don't set it
  to `0` without enabling the oracle.

**When it fires** is `CORP_LLM_ORACLE_TRIGGER`: `gazetteer_hit` (default) |
`any_local_finding` | `always` | `sampled:<pct>`. A team's profile can widen it
via `oracle_mode`; the effective value is the **broader** of the two — neither
can narrow the other.

## Toggle: the corp NER service

A remote NER detector appended to the local cascade. **Network-backed**, so it is
excluded from `CODE` segments (it would ship source code to an external service);
all local detectors keep scanning `CODE`.

**Off (default).** `CORP_NER_ENABLED=0`. The stack behaves exactly as if the
feature did not exist: no detector, no readiness probe.

**On.** Both keys are required together:

```
CORP_NER_ENABLED=1
CORP_NER_ENDPOINT=http://<ner-host>:<port>      # BASE url; the client appends /v1/analyze
```

- **Enabled without an endpoint is a boot refusal**, not a silent skip —
  `build_corp_ner()` raises `ConfigError`, and `gateway-admin config check`
  reports the same problem.
- **It needs a source build.** The published `GATEWAY_IMAGE_TAG` (`1.0.0-rc.6`,
  commit `1f170ea`) is an *ancestor* of the branch that added corp NER, so on
  that image these variables do nothing. Use
  `docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`.
  That overlay builds the `ru-en` NER profile on purpose — the Dockerfile default
  (`base`) ships no EN model, and with `CORP_LLM_REQUIRE_NER=1` that image 503s
  on every request.
- Optional tuning, passed by **bare name** (leave the lines commented rather than
  empty — an empty env var wins over the config file):
  `CORP_NER_TIMEOUT_S` (30), `CORP_NER_MAX_TEXTS` (256),
  `CORP_NER_MAX_INPUT_CHARS` (200000), `CORP_NER_CA_BUNDLE` (unset).
- The NER call carries **raw user content**, so its TLS verification is never
  disabled. For an internal CA, point `CORP_NER_CA_BUNDLE` at the chain.

**Flipping either toggle is safe for Cache A.** The cache key folds a fingerprint
of the effective detector policy, so pods with different `CORP_NER_ENABLED`
values derive disjoint keys and cannot serve each other's entries. No cache flush
is needed — see `upgrade.md`.

Do not confuse `CORP_NER_ENABLED` (this remote service) with
`CORP_LLM_REQUIRE_NER` (the **in-process** RU/EN engines), which stays `1` in
production either way.
