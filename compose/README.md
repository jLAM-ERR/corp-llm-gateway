# Production compose stack

A production deploy target for non-k8s hosts, alongside `helm/corp-llm-gateway/`
(the k8s target). This directory ships:

- the **data plane** — `litellm` (the guardrail-fronted proxy) + `redis`
  (Cache B, the per-conversation mapping store) + `postgres` (the
  token/team-config store, plus litellm's own virtual-key/UI database);
- **self-hosted Langfuse v3** — `langfuse-web`, `langfuse-worker`,
  `langfuse-postgres`, `clickhouse`, `minio`, `minio-init` and
  `langfuse-redis`, publishing no host port (see "Langfuse" below).

The production Vector config and the optional nginx front door land in later
revisions of this stack — see
`docs/plans/20260802-production-compose-corp-ner.md` for the full build order.

## Quickstart

```
cd compose
cp .env.example .env
chmod 0600 .env
# edit .env: GATEWAY_IMAGE_TAG, POSTGRES_PASSWORD, LITELLM_MASTER_KEY,
# UI_USERNAME, UI_PASSWORD, at least one of ANTHROPIC_API_KEY/OPENAI_API_KEY,
# and the Langfuse secrets (LANGFUSE_POSTGRES_PASSWORD,
# LANGFUSE_CLICKHOUSE_PASSWORD, MINIO_ROOT_PASSWORD, LANGFUSE_NEXTAUTH_SECRET,
# LANGFUSE_SALT, LANGFUSE_ENCRYPTION_KEY)
cp ../src/corp_llm_gateway/tokens/schema.sql postgres/initdb/01-schema.sql
docker compose up -d
docker compose ps                              # wait ~30-60s (start_period) for "healthy";
                                               # langfuse-web/worker take ~2 min on a fresh
                                               # volume while migrations run
curl http://localhost:4000/health/liveliness
```

Skipping the `01-schema.sql` staging step is a silent trap, not an obvious
failure: `docker compose ps` reports everything healthy and
`/health/liveliness` answers, but every real request 500s because
`corp_tokens`/`team_config` don't exist. See
`compose/postgres/initdb/README.md` for what each init script does and how
to re-stage after an upgrade.

`GATEWAY_IMAGE_TAG`, `POSTGRES_PASSWORD`, `LITELLM_MASTER_KEY`, `UI_USERNAME`
and `UI_PASSWORD` have no default — `docker compose up` refuses to start with
a clear "set X in .env" error rather than silently booting half-configured.

## Building from this branch

The `GATEWAY_IMAGE_TAG` default (`.env.example`) is a published rc that
predates whichever branch you're reading this on — features landing here
(e.g. `CORP_LLM_STRIP_INBOUND_HEADERS`, Workstream B's corp NER
integration) are silently absent from that tag until a new rc is cut and
`GATEWAY_IMAGE_TAG` is bumped to it. To run the current branch's code
instead of the published tag:

```
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

**This is a dev/staging convenience, not a substitute for a release.**
`GATEWAY_IMAGE_TAG` must point at an rc containing Workstream B's corp NER
work before this stack is production-ready — treat that as a release gate.

## Upstream routing

`litellm/config.yaml` routes by model-name prefix: `claude-*` → native
`anthropic/`, `gpt-*` → native `openai/`, `corp-*` → `hosted_vllm/` at
`CORP_LLM_ENDPOINT`. Provider credentials for the first two come from
`ANTHROPIC_API_KEY`/`OPENAI_API_KEY` in `.env`, held by the gateway, not the
developer — see "Virtual keys" for why.

`CORP_LLM_ENDPOINT` is optional, but leaving it unset does **not** disable
anything by itself: `CORP_LLM_ORACLE_ENABLED=0` (the `.env.example` default)
is what keeps the corp-LLM oracle off. With the oracle enabled and no
endpoint, a gazetteer hit falls through to `bootstrap.py`'s non-routable
placeholder host and fails the request closed — on **every** route, not
just `corp-*`. Set `CORP_LLM_ORACLE_ENABLED=1` only once `CORP_LLM_ENDPOINT`
points at a reachable corp vLLM. The `corp-*` route itself always needs the
endpoint regardless of the oracle flag; without it, `corp-*` requests fail
per-request rather than failing config load.

## Virtual keys

Developers authenticate with a **LiteLLM virtual key**
(`Authorization: Bearer sk-...`), issued from the litellm Admin UI or API and
consumed at the proxy — not forwarded, not logged. This is why
`LITELLM_MASTER_KEY` + `DATABASE_URL` + `STORE_MODEL_IN_DB=True` +
`UI_USERNAME`/`UI_PASSWORD` are set on this instance: virtual keys ARE the
developer credential here, giving per-developer revocation and traffic
accounting through the Admin UI. There is no second admin-only instance —
that pattern existed only to keep the master key away from a BYOK data
plane, and BYOK against Anthropic/OpenAI directly isn't possible (next
section), so the tradeoff that motivated it no longer applies.

`X-Corp-Auth` is **unchanged** and unrelated to virtual keys — it still
carries team → profile → rules → audit identity through the guardrail
(`AuthMiddleware`, untouched by this rework). Clients send **both** headers
on every request:

- `Authorization: Bearer <litellm virtual key>` — who may call this proxy at
  all, and litellm's own spend/rate-limit accounting.
- `X-Corp-Auth: <corp team token>` — which team's profile/rules/audit
  identity applies inside the guardrail.

## Why not BYOK

Two independent findings, both narrower than an earlier revision of this
README claimed — read carefully, the mechanisms are different.

**(a) Native `anthropic/`/`openai/` credentials never come from the client.**
Those providers build their upstream credential from the configured
`api_key` (`ANTHROPIC_API_KEY`/`OPENAI_API_KEY`) and never read the inbound
`Authorization` header. Verified twice: against litellm 1.85.0 (the pinned
image) and independently against 1.90.1; see `examples/compose/README.md`
"BYOK in local mode (SPIKE finding)".

**(b) Our own guardrail forwards inbound wire headers regardless of
litellm's own gate — that's what `CORP_LLM_STRIP_INBOUND_HEADERS` is for.**
litellm's `forward_client_headers_to_llm_api` gate (`litellm/proxy/
litellm_pre_call_utils.py`, `add_litellm_data_for_backend_llm_call` /
`add_headers_to_llm_call_by_model_group`) is correctly unset in
`litellm/config.yaml` — litellm itself does not copy client headers into
`data["headers"]`. But `CorpLlmGuardrail._pre_call_impl` (reached via
`async_pre_call_hook`) sets `data["headers"]` **unconditionally**,
independent of that gate, and litellm forwards whatever ends up in
`data["headers"]` to the upstream HTTP call regardless of how it got
there. Wire-captured
against a stand-in upstream: `host`, `user-agent`, `accept`, `connection`,
`content-type`, `content-length` and an arbitrary probe header all reached
the upstream call, on **both** `corp-*` and `claude-*` routes, with the
strip flag off; none did with it on. So `CORP_LLM_STRIP_INBOUND_HEADERS` is
load-bearing on this stack, not a reserved no-op — it's set `1` in
`docker-compose.yml`/`.env.example`. It never touches `Authorization` (see
`litellm_hook.py` `_WIRE_HEADERS_TO_DROP` — BYOK passthrough, invariant #3,
is preserved for deployments that DO forward it).

BYOK (the developer's own key forwarded untouched — CLAUDE.md invariant #3)
is nonetheless **not available on this compose stack at all**, on any
route: developers authenticate with the litellm virtual key described
above, and the `corp-*`/`hosted_vllm/` route builds its upstream request
from the configured `api_key`/model params the same way `anthropic/` and
`openai/` do, not from a forwarded client `Authorization`. Full stop.

## TLS to the corp vLLM

See `compose/certs/README.md`. Two different HTTP clients in the `litellm`
container read TLS trust differently: our own `CorpLlmClient` (httpx) reads
`CORP_LLM_CA_BUNDLE` from `.env` (bare-name form, so an unset value is
absent from the container entirely, not set to an empty string) — scoped to
calls to the corp vLLM only. litellm's own clients (native `anthropic/`,
`openai/` AND `hosted_vllm/`, all aiohttp) read `SSL_CERT_FILE`, which is
**process-global**: pointing it straight at the corp CA would silently
replace the trust store for `api.anthropic.com`/`api.openai.com` too. So
`SSL_CERT_FILE` is not set from `.env` at all — the container's entrypoint
builds a combined bundle (certifi's public roots, plus `corp-ca-bundle.pem`
appended if mounted) at boot and `SSL_CERT_FILE` always points at that.

## Two UIs — which one answers which question

The stack ships two web UIs. They do not overlap; reaching for the wrong one
is the usual reason an operator concludes "the gateway has no data".

| Question | Where |
|---|---|
| Who has a virtual key, and is it still valid? | LiteLLM UI |
| How much has a developer/team spent, and what are their rate limits? | LiteLLM UI |
| Which models does this proxy expose, and is a route healthy? | LiteLLM UI |
| What did request `<id>` look like end to end — latency, token counts, upstream error? | Langfuse |
| Was that request sanitized, and how many redactions did it carry (`redaction_count`, `finding_label_counts`)? | Langfuse |
| Why was a request blocked (`block_reason`), and which team was it? | Langfuse |
| What is the audit trail for the last 90 days? | Langfuse |

Short version: **LiteLLM = keys, models, spend. Langfuse = request-level
traces and the audit trail.** Neither ever holds original user content —
audit records pass the NEVER-fields gate (`audit/invariants.py`) plus
Vector's VRL gate before they reach Langfuse (invariant #2).

## Langfuse

Self-hosted Langfuse v3, ported from `docker-compose.demo.yml` and hardened:

- **every secret comes from `.env`** — the demo ships literal
  `demo-secret-key-change-in-production` / `demo-salt-change-in-production` /
  an all-zero `ENCRYPTION_KEY` / `minioadmin`. Here `docker compose up`
  refuses to start until each is set;
- **no host port is published by any Langfuse service** (the demo publishes
  `3000` for the UI and `9001` for the MinIO console). Langfuse holds
  request-level traces and the audit trail, so it is reachable only through
  the nginx profile (C1) or an SSH tunnel;
- **named volumes** for all four stateful services (`langfuse-postgres-data`,
  `langfuse-clickhouse-data`, `langfuse-clickhouse-logs`,
  `langfuse-minio-data`, `langfuse-redis-data`);
- **scoped `environment:`** per service, as everywhere else in this file — no
  `env_file:`, which would broadcast `POSTGRES_PASSWORD`/`UI_PASSWORD`/
  provider keys into ClickHouse, MinIO and Langfuse alike.

`langfuse-redis` is a **separate** Redis instance, not the gateway's. The
gateway's `redis` is Cache B — CLAUDE.md calls it *required* for `post_call`
desanitization — and `redis/redis.conf` caps it at `maxmemory 512mb` with
`noeviction`. Sharing it (as the demo does) means a Langfuse ingestion
backlog can exhaust that budget and make Cache B writes fail with `OOM`: an
observability backlog would break live desanitization. Both instances run
`noeviction`; Langfuse's queue holds not-yet-persisted events that nothing
can reconstruct.

`minio-init` is a one-shot job that creates the event-upload bucket, because
**Langfuse v3 does not create it itself**. Without it, `langfuse-web` and
`langfuse-worker` come up healthy and then fail every ingestion write.

ClickHouse gets `langfuse/clickhouse-config.xml`, which deletes `query_log`,
`query_thread_log`, `query_views_log` and `text_log`. Those system tables
keep raw INSERT text — i.e. a second copy of trace content, on ClickHouse's
own 30-day retention, in a store no NEVER-fields gate watches (invariant #1).

### Reaching the UI

Once `--profile nginx` (C1) lands: `langfuse.<domain>` → `langfuse-web:3000`,
and `LANGFUSE_PUBLIC_URL` must be that public origin.

Until then, an SSH tunnel. Nothing is published on the host, so tunnel to the
container address (the docker bridge is routable from the host on Linux):

```
# on the server
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
  "$(docker compose ps -q langfuse-web)"

# from your laptop, with that address
ssh -N -L 3000:<container-ip>:3000 <user>@<server>
# then open http://localhost:3000
```

`LANGFUSE_PUBLIC_URL` (→ `NEXTAUTH_URL`) must match the origin the **browser**
uses. Its default `http://localhost:3000` is right for the tunnel and wrong
behind nginx; the two cannot be correct at the same time, so change it when
nginx lands.

### First login

`LANGFUSE_DISABLE_SIGNUP=true` by default: on a fresh instance whoever
reaches the UI first would otherwise claim the admin account. Provision the
first user (and the org, project and ingestion API keys) with the
`LANGFUSE_INIT_*` block in `.env.example` — those keys are what the Vector
audit sink authenticates with. With signup disabled **and** no init user
there is no way to log in; that combination is the one mistake to avoid.

## Postgres

Two databases on the one `postgres` service: `$POSTGRES_DB` (default
`gateway` — our tokens/team_config schema) and `litellm` (litellm's own
virtual-key store + UI accounts, created by the committed
`compose/postgres/initdb/00-create-litellm-db.sh`). See
`compose/postgres/initdb/README.md` for how `$POSTGRES_DB`'s schema is
staged (git-ignored — never committed, so it cannot drift from
`src/corp_llm_gateway/tokens/schema.sql`).

## Loopback bind

The `litellm` port publishes on `127.0.0.1`, not `0.0.0.0`. Until the nginx
profile (`--profile nginx`) lands there is no TLS in front of this stack, and
virtual keys + `X-Corp-Auth` would otherwise cross a server-class network in
cleartext. It is the only published port in the stack: no Langfuse service
publishes one at all (see "Langfuse").

## Durability

Every stateful service uses a named volume, so `docker compose down` (without
`-v`) keeps mappings, tokens/team-config, virtual keys and the whole Langfuse
trace/audit history across a restart — unlike `docker-compose.demo.yml`'s
laptop-walkthrough posture. `litellm`
itself has no named volume; it is stateless (config/certs are read-only bind
mounts) and its state already lives in `redis`/`postgres` above. Redis is
configured `noeviction` (`compose/redis/redis.conf`): Cache B is CLAUDE.md's
*required* per-conversation mapping store for `post_call` desanitization, so
an under-memory Redis must fail loudly (`OOM` errors) rather than silently
evict live mappings and return placeholder text to a developer.

## Upgrading from an earlier deployment

If you already ran this stack before the litellm virtual-key rework (no
`compose/postgres/initdb/00-create-litellm-db.sh`), your `postgres` volume
has no `litellm` database and the `litellm` container fails at boot —
`docker-entrypoint-initdb.d` scripts only run once, against an empty volume.
See `compose/postgres/initdb/README.md` "Upgrading a pre-existing
deployment" for the recreate / manual `CREATE DATABASE` fix.

## Environment posture

`CORP_ENV=production` arms the F9 guard (`config.py` `is_prod()`): an
operator's `SSL_VERIFY=false` on the corp-LLM **oracle** call is refused
rather than silently disabling TLS verification there. F9 only reaches that
guard through `build_corp_llm_client()`, which runs only when
`CORP_LLM_ORACLE_ENABLED=1` — off by default on this stack — so treat it as
a narrow oracle-only protection, not a blanket one (see "TLS verification is
always on" below). `CORP_LLM_REQUIRE_NER=1` makes a self-disabled NER engine
fail closed (`NerUnavailableError`) instead of silently returning no
findings and letting a PERSON/ORG egress (F2). Both are set by default in
`.env.example`; do not clear them for a real deploy.

**TLS verification is always on.** `SSL_VERIFY` is hardcoded `true` in
`docker-compose.yml` and is **not** an `.env` key — litellm's own
`get_ssl_verify()` reads that same variable directly, at *higher* priority
than `SSL_CERT_FILE`, for every upstream provider (`anthropic/`, `openai/`,
`hosted_vllm/`), with no `CORP_ENV` guard on that read. An operator-settable
`SSL_VERIFY=false` here would silently disable certificate verification
stack-wide, not just for the oracle. See `docs/security.md` "Known
gaps/follow-ups" — widening F9 to cover litellm's read is a `src` follow-up,
out of scope for this compose stack.

**`CORP_LLM_ORACLE_ENABLED` and `CORP_LLM_LOCAL_FIRST` are coupled.**
`bootstrap.build_guardrail()` raises `ConfigError(NO_OP_SANITIZER_MESSAGE)`
at boot if both are off — the gateway refuses to run as a no-op sanitizer.
This stack leaves `CORP_LLM_LOCAL_FIRST` unset (its `settings.py` default is
`"1"`), so the default posture is safe; don't set it to `0` in `.env`
without also setting `CORP_LLM_ORACLE_ENABLED=1`, or the container fails to
boot with no other warning.

## Operator CLI (gateway-admin)

`gateway-admin team create` / `token issue` and other RBAC-gated mutations
need either `CORP_GATEWAY_ADMIN_TOKEN` (an operator JWT) or
`CORP_GATEWAY_RBAC=0` (dev bypass) — **neither is set in the `litellm`
service's environment today**. Export one of them on the machine running
`gateway-admin` before issuing tokens or creating teams against this
deployment; see `docs/ops/admin-cli.md`.
