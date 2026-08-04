# Production compose stack

A production deploy target for non-k8s hosts, alongside `helm/corp-llm-gateway/`
(the k8s target). This directory ships:

- the **data plane** — `litellm` (the guardrail-fronted proxy) + `redis`
  (Cache B, the per-conversation mapping store) + `postgres` (the
  token/team-config store, plus litellm's own virtual-key/UI database);
- **self-hosted Langfuse v3** — `langfuse-web`, `langfuse-worker`,
  `langfuse-postgres`, `clickhouse`, `minio`, `minio-init` and
  `langfuse-redis`, publishing no host port (see "Langfuse" below);
- the **audit pipeline** — `vector`, which tails the gateway's stdout and
  forwards audit records to Langfuse (see "Audit pipeline" below).

The optional nginx front door lands in a later revision of this stack — see
`docs/plans/20260802-production-compose-corp-ner.md` for the full build order.

## Quickstart

```
cd compose
cp .env.example .env
chmod 0600 .env
# edit .env: GATEWAY_IMAGE_TAG, POSTGRES_PASSWORD, LITELLM_MASTER_KEY,
# UI_USERNAME, UI_PASSWORD, at least one of ANTHROPIC_API_KEY/OPENAI_API_KEY,
# the Langfuse secrets (LANGFUSE_POSTGRES_PASSWORD,
# LANGFUSE_CLICKHOUSE_PASSWORD, MINIO_ROOT_PASSWORD, LANGFUSE_NEXTAUTH_SECRET,
# LANGFUSE_SALT, LANGFUSE_ENCRYPTION_KEY) and the Langfuse PROJECT API keys the
# audit pipeline posts with (CORP_LANGFUSE_PUBLIC_KEY, CORP_LANGFUSE_SECRET_KEY
# — see "Audit pipeline")
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

`GATEWAY_IMAGE_TAG`, `POSTGRES_PASSWORD`, `LITELLM_MASTER_KEY`, `UI_USERNAME`,
`UI_PASSWORD`, `CORP_LANGFUSE_PUBLIC_KEY` and `CORP_LANGFUSE_SECRET_KEY` have
no default — `docker compose up` refuses to start with a clear "set X in .env"
error rather than silently booting half-configured.

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
observability backlog would break live desanitization.

Splitting the instance removes the shared-quota coupling but **not the host
one**, so `langfuse-redis` is capped twice
(`LANGFUSE_REDIS_MAXMEMORY` / `LANGFUSE_REDIS_MEM_LIMIT` in `.env.example`):

- `--maxmemory 256mb` — Redis's own data cap;
- a `512mb` container memory limit — the RSS backstop. Redis RSS runs *above*
  `maxmemory` (allocator fragmentation, client output buffers, and the fork
  copy-on-write of an AOF rewrite), which is why the container limit is ~2x
  the data cap rather than equal to it.

Without both, an uncapped queue grows until the kernel OOM killer picks a
victim, and the victim can just as easily be `litellm`, `postgres` or Cache B
— i.e. observability load becomes a request-path outage. Raise the two
together; the defaults hold a six-figure job backlog against a steady state
of a few MB. Both keys are optional in `.env` (the defaults live in
`docker-compose.yml`), but **never set either to `0`** — `0` is "unlimited"
to both Redis and docker, it renders without a warning, and it puts the host
exposure straight back.

`minio-init` is a one-shot job that creates the event-upload bucket, because
**Langfuse v3 does not create it itself**. Without it, `langfuse-web` and
`langfuse-worker` come up healthy and then fail every ingestion write.

ClickHouse gets `langfuse/clickhouse-config.xml`, which deletes `query_log`,
`query_thread_log`, `query_views_log` and `text_log`. Those system tables
keep raw INSERT text — i.e. a second copy of trace content, on ClickHouse's
own 30-day retention, in a store no NEVER-fields gate watches (invariant #1).

### When the Langfuse queue fills

`langfuse-redis` runs `noeviction` (hardcoded in `docker-compose.yml`, not an
`.env` key). That is a **data-loss decision, not a sizing one**: at the cap
Langfuse rejects new ingestion writes loudly instead of silently deleting
queue entries for events it already accepted. Each entry is a pointer to an
event blob already uploaded to MinIO under `events/`, so an eviction would not
destroy the payload — but nothing re-enqueues it, so the trace would never
reach ClickHouse and never appear in the audit view. A loud rejection you can
retry beats a silent gap in the audit trail.

**The request path is not affected.** Nothing in `pre_call`/`post_call` talks
to this Redis: the gateway's audit sink defaults to `stdout`
(`CORP_AUDIT_SINK` is unset on this stack — see `audit/factory.py`), and
Vector forwards from there and retries. Those retries are not free by default:
they work because the Langfuse sink in `vector/vector.yaml` is configured with
a **disk buffer** (`when_full: block`, 1 GiB, on the `vector-data` volume) and
unlimited retries with backoff. With the stock in-memory buffer, or with
`when_full: drop_newest`, the 5xx below would silently discard audit records at
the Vector layer instead — the same gap in the audit trail `noeviction` exists
to prevent, moved one hop upstream. See "Audit pipeline" for the full path.
Even on a direct-to-Langfuse sink the
`audit_sink` fail policy is `continue` (the M4 fail-policy matrix) and a
failed emit is contained by the safety net in `litellm_hook.audit()` — it
never reaches the client body and never blocks desanitization. So a full
Langfuse queue costs observability data, never live traffic.

What it looks like:

- `langfuse-redis` stays **healthy** — `redis-cli ping` is a read and still
  answers `PONG` at the cap;
- `langfuse-web` logs ingestion errors and its `/api/public/ingestion`
  endpoint starts returning 5xx;
- traces stop appearing in the UI while requests keep succeeding.

Check and fix:

```
# is it actually at the cap?
docker compose exec langfuse-redis redis-cli info memory | grep -E 'used_memory_human|maxmemory_human'
docker compose exec langfuse-redis redis-cli info keyspace

# is the consumer alive? a full queue is usually a stalled worker, not a small cap
docker compose ps langfuse-worker
docker compose logs --tail=100 langfuse-worker
```

Fix the consumer first (`langfuse-worker` unhealthy, or ClickHouse/MinIO
down) — the queue drains on its own once it recovers, and the AOF on
`langfuse-redis-data` means a restart does not lose it. Only raise
`LANGFUSE_REDIS_MAXMEMORY` **and** `LANGFUSE_REDIS_MEM_LIMIT` (keeping the
~2x ratio) if the backlog is genuine sustained volume, then
`docker compose up -d langfuse-redis`. Never "fix" it with `FLUSHALL`: that
discards accepted-but-unprocessed audit events, which is the exact failure
`noeviction` exists to prevent.

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
`LANGFUSE_INIT_*` block in `.env.example` — that key pair is what the Vector
audit sink authenticates with, copied into `CORP_LANGFUSE_PUBLIC_KEY` /
`CORP_LANGFUSE_SECRET_KEY`. With signup disabled **and** no init user
there is no way to log in; that combination is the one mistake to avoid.

Chicken-and-egg on a first boot: `vector` needs the project keys, and the
project does not exist yet. The clean way out is headless init — set
`LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `_SECRET_KEY` to values you generate and
copy the same two values into `CORP_LANGFUSE_PUBLIC_KEY` /
`CORP_LANGFUSE_SECRET_KEY`. If you provision through the UI instead, put a
placeholder in the two `CORP_LANGFUSE_*` keys first (they have no default —
`docker compose config` fails while they are empty, whatever `--scale` you
pass), create the real keys, then update `.env` and
`docker compose up -d vector`. Audit records emitted with the wrong key are not
lost: Langfuse answers 401, Vector retries into its disk buffer, and the log
files it reads from are still on disk.

## Audit pipeline

```
gateway stdout (StdoutSink)  ->  docker json-file log  ->  vector  ->  langfuse
```

`CORP_AUDIT_SINK` is unset on this stack, so `audit/factory.py`'s default
`stdout` sink applies: every `AuditEvent` is one JSON line on the `litellm`
container's stdout. `vector` tails that and posts to Langfuse's
`/api/public/ingestion`, authenticating with the Langfuse **project** API keys
`CORP_LANGFUSE_PUBLIC_KEY` / `CORP_LANGFUSE_SECRET_KEY` (not the
`LANGFUSE_*` infrastructure secrets — see "First login" for where they come
from). The service is always on and not profile-gated: audit is not optional,
and a mistake here loses audit records.

**Config:** `vector/vector.yaml`, ported from `docker/demo-vector/vector.yaml`.
The `never_fields_gate` and `audit_only` transforms are copied **verbatim** from
it (and match `helm/corp-llm-gateway/templates/configmap.yaml`) — they are
defence-in-depth for CLAUDE.md invariant #2, dropping any record that carries a
mapping/original/credential key before it can reach a sink, and any record that
is not AuditEvent-shaped. Do not paraphrase or restructure them.

**No docker socket.** The demo mounts `/var/run/docker.sock` and uses Vector's
`docker_logs` source; `docker-compose.demo.yml` warns against reusing that,
because the socket is the docker daemon's control plane — read access to it is
host root, since a container holding it can start a privileged sibling with `/`
bind-mounted. Here Vector reads a **read-only bind of the container log
directory** (`DOCKER_CONTAINERS_DIR`, default `/var/lib/docker/containers`)
with a `file` source: data in, no control plane, no write path. Three
consequences worth knowing:

- the glob is `*/*-json.log`, so the sibling `config.v2.json` / `hostconfig.json`
  files — which hold every container's environment — are never read;
- the source sees **every** container's logs, not just `litellm`: the log
  directory carries container ids, not names, so there is nothing to filter on
  at that layer. `audit_only` is the scoping mechanism — a record reaches a sink
  only if it parses as JSON and has both `request_id` and `redaction_count`,
  which nothing but our own `StdoutSink` emits;
- it requires docker's default **`json-file` logging driver**. Under `local`,
  `journald` or a remote driver there are no `*-json.log` files and the audit
  pipeline goes quiet. Check with
  `docker info --format '{{.LoggingDriver}}'` before a real deploy.

Vector runs with a read-only root filesystem and all capabilities dropped. It
stays root (the image default) because the log directory is mode `0710
root:root`; there is no `user:` override to add.

**Delivery is durable.** Read checkpoints and the sink's disk buffer live on the
`vector-data` named volume, so a Vector restart resumes at the exact byte it
stopped at — records written while it was down are delivered, not skipped, and
none are re-sent. The Langfuse sink buffers to disk with `when_full: block` and
retries indefinitely with backoff; see "When the Langfuse queue fills" for why
that combination is load-bearing rather than a default. If the disk buffer does
fill, Vector back-pressures the file source and stops reading — the records stay
in the docker log files and are picked up from the checkpoint once Langfuse
recovers.

**Long records are reassembled.** The `json-file` driver splits any output line
longer than 16 KiB across several records, and only the last one ends in a
newline. Unmerged, both halves would fail to parse and `audit_only` would drop
them — a silent hole for exactly the largest audit records (a request with a
long `placeholder_list`). The `merge_partial_lines` reduce rejoins them, which
is what Vector's `docker_logs` source does for itself via `auto_partial_merge`.

**S3 and SIEM sinks ship OFF.** `vector/sinks-s3.yaml` (durable archive) and
`vector/sinks-siem.yaml` (forwarder) are separate config files, loaded only when
`CORP_AUDIT_S3_ENABLED=1` / `CORP_AUDIT_SIEM_ENABLED=1`. Vector merges every
`--config` file into one topology, so both attach to the same `audit_only`
transform and inherit the NEVER-fields gate. The corp SIEM endpoint and its auth
scheme are still an open item, so that file has no endpoint default: enabling it
without `CORP_AUDIT_SIEM_URL` makes Vector refuse to start rather than post audit
records somewhere unintended. The same is true of `CORP_AUDIT_S3_BUCKET` /
`CORP_AUDIT_S3_REGION`.

One Vector quirk when editing those files: **Vector interpolates `${VAR}`
references inside YAML comments too**, so a commented-out example naming an
unset variable fails config load. Describe such options in prose instead.

Validate a change before deploying it:

```
docker run --rm -v "$PWD/vector:/etc/vector:ro" \
  -e CORP_LANGFUSE_PUBLIC_KEY=x -e CORP_LANGFUSE_SECRET_KEY=x \
  timberio/vector:0.53.0-alpine validate --no-environment /etc/vector/vector.yaml
```

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
publishes one at all (see "Langfuse"), and `vector`'s API is bound to
`127.0.0.1` *inside* its own container, purely so its healthcheck can reach it.

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
