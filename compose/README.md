# Production compose stack

A production deploy target for non-k8s hosts, alongside `helm/corp-llm-gateway/`
(the k8s target). Russian mirror: `README.ru.md`. This directory ships:

- the **data plane** — `litellm` (the guardrail-fronted proxy) + `redis`
  (Cache B, the per-conversation mapping store) + `postgres` (the
  token/team-config store, plus litellm's own virtual-key/UI database);
- **self-hosted Langfuse v3** — `langfuse-web`, `langfuse-worker`,
  `langfuse-postgres`, `clickhouse`, `minio`, `minio-init` and
  `langfuse-redis`, publishing no host port (see "Langfuse" below);
- the **audit pipeline** — `vector`, which tails the gateway's stdout and
  forwards audit records to Langfuse (see "Audit pipeline" below);
- the optional **HTTPS front door** — `nginx` or `nginx-ports`, an nginx that
  publishes the gateway and Langfuse over HTTPS. Off unless the server's `.env`
  sets `COMPOSE_PROFILES` (see "HTTPS front door (nginx)" below).

## Which mode am I deploying?

Read `docs/ops/deployment-modes.md` (RU: `deployment-modes.ru.md`) first. It
covers the two **mutually exclusive** auth modes — API keys vs subscription/OAuth
— and exactly how to turn the corp-LLM oracle and the corp NER service on and
off. Both run **this** stack. Short version:

- **Subscription mode — the production mode.** `docker compose -f
  docker-compose.yml -f docker-compose.oauth.yml up -d`. Requires that no
  `LITELLM_MASTER_KEY` exist at all, serves `claude-*` only, and forwards the
  developer's own Anthropic subscription token upstream. `scripts/deploy/deploy.sh`
  deploys this mode by default. Developers get their `X-Corp-Auth` token from
  `scripts/install.sh` (Keycloak login → `POST /internal/issue-token`) once
  issuance is on: add the `docker-compose.issuance.yml` overlay (see "Developer
  token issuance" below). `gateway-admin token issue` stays as break-glass.
- **API-key mode (the compose default) — a test posture only.** `docker compose
  up -d`. `LITELLM_MASTER_KEY` is required and requests carry a LiteLLM virtual
  key, but there is **no way to issue one**: the route gate refuses `/key/*`, the
  admin UI and the rest of litellm's management surface
  (`docs/security.md` §14). The master key exists for the container tests
  (`tests/integration/test_route_gate_container.py`), which seed a key straight
  into litellm's database. Do not deploy this mode for developers. Everything
  below about virtual keys describes this test posture; the rest of this file
  (audit, NER, oracle, Vector, volumes, TLS) is identical in both.
  The root-level `docker-compose.anthropic-oauth.yml` is a **demo-only** variant
  of the same idea over `docker-compose.demo.yml` — not a deployment target.
- **Oracle:** `CORP_LLM_ORACLE_ENABLED` (default `0`), needs `CORP_LLM_ENDPOINT`
  when `1`. **Corp NER:** `CORP_NER_ENABLED` (default `0`), needs
  `CORP_NER_ENDPOINT` when `1`.

## Quickstart

```
cd compose
cp .env.example .env
chmod 0600 .env
# edit .env: GATEWAY_IMAGE_TAG, POSTGRES_PASSWORD, LITELLM_MASTER_KEY,
# UI_USERNAME, UI_PASSWORD, whichever of ANTHROPIC_API_KEY/OPENAI_API_KEY
# match the routes you actually use (neither, if only corp-*),
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

`GATEWAY_IMAGE_TAG`, `POSTGRES_PASSWORD`, `CORP_LANGFUSE_PUBLIC_KEY` and
`CORP_LANGFUSE_SECRET_KEY` have no default — `docker compose up` refuses to
start with a clear "set X in .env" error rather than silently booting
half-configured.

`LITELLM_MASTER_KEY`, `UI_USERNAME` and `UI_PASSWORD` are required too, but the
check lives in the litellm entrypoint rather than in compose interpolation,
because it has to be **mode-aware**: subscription mode needs all three absent.
Missing them in API-key mode is still a boot refusal naming the variable — it
just happens a second later, when the container starts.

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

The overlay builds the **`ru-en`** NER profile (`NER_PROFILE` build arg), not
`Dockerfile.gateway`'s `base` default: `base` ships no `en_core_web_md`, and with
`CORP_LLM_REQUIRE_NER=1` (the default here) a missing EN engine turns every
request into a 503 `E_NER_UNAVAILABLE`. Do not drop that arg.

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

**API-key mode only, and that mode is a test posture.** In subscription mode
there is no master key, so there are no virtual keys; the developer's OAuth
bearer is the credential and `X-Corp-Auth` remains the team identity. See
`docs/ops/deployment-modes.md`.

In API-key mode litellm checks a **LiteLLM virtual key**
(`Authorization: Bearer sk-...`) at the proxy — not forwarded, not logged —
which is why `LITELLM_MASTER_KEY` + `DATABASE_URL` + `STORE_MODEL_IN_DB=True` +
`UI_USERNAME`/`UI_PASSWORD` are set on this instance. **Nothing can issue one.**
The route gate answers `403 E_ROUTE_BLOCKED` on `/key/*`, the admin UI and every
other litellm management route, in both modes, so there is no per-developer
revocation or spend accounting through litellm (`docs/security.md` §14,
"The management surface is refused"). Per-developer revocation is the corp
token: `gateway-admin token revoke`.

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

## Corp NER service (optional, off)

A remote NER detector appended to the local-first cascade
(`detectors/corp_ner.py`, wired by `bootstrap.build_corp_ner()`). It is **off
by default** — `CORP_NER_ENABLED=0` — and while it is off the stack behaves
exactly as it did before the feature existed: no detector, no readiness probe.
Not to be confused with `CORP_LLM_REQUIRE_NER` ("Environment posture"), which
governs the **in-process** RU/EN NER engines and stays `1` either way.

Turning it on needs both `CORP_NER_ENABLED=1` and `CORP_NER_ENDPOINT` (the
service **base** URL; the client appends `/v1/analyze`). Enabled without an
endpoint is a **boot refusal**, not a silent skip — `build_corp_ner()` raises
`ConfigError`, and `gateway-admin config check` reports the same problem.

Four tuning keys are passed to the container **by bare name**, which is the
reason `.env.example` ships them commented rather than empty:

| key | code default | notes |
|---|---|---|
| `CORP_NER_TIMEOUT_S` | `30` | under the service's own 60s |
| `CORP_NER_MAX_TEXTS` | `256` | texts per batch (service limit) |
| `CORP_NER_MAX_INPUT_CHARS` | `200000` | chars per batch; a longer single text fails **closed** |
| `CORP_NER_CA_BUNDLE` | unset | PEM chain for an internal-CA NER cert |

`CORP_NER_TIMEOUT_S=` (empty) is **not** the same as leaving the line
commented. An empty env var wins over the config file in `config.get()`, so it
shadows any value in a mounted `/etc/corp-llm-gateway/config.toml` and makes
`gateway-admin config check` print `''` where the effective default is `30`.
Bare-name passthrough keeps "unset in `.env`" meaning "not configured here",
which is exactly what the required-when-enabled check on `CORP_NER_ENDPOINT`
is written to see. Same reasoning as `CORP_LLM_CA_BUNDLE` above.

The NER call carries **raw user content**, so its TLS verification is never
disabled — there is no `SSL_VERIFY`-style escape hatch for it. Point
`CORP_NER_CA_BUNDLE` at an internal CA chain instead (the `compose/certs`
mount already available to the container works).

Note the published `GATEWAY_IMAGE_TAG` predates this work — see "Building from
this branch".

## Which tool answers which question

The stack ships one web UI, Langfuse. litellm's own UI is refused at the route
gate; reaching for it is the usual reason an operator concludes "the gateway has
no data".

| Question | Where |
|---|---|
| Who holds a corp token, and is it still valid? | `gateway-admin token list` (the LiteLLM UI is refused at the route gate) |
| Which models does this proxy expose? | `GET /v1/models` |
| What did request `<id>` look like end to end — latency, token counts, upstream error? | Langfuse |
| Was that request sanitized, and how many redactions did it carry (`redaction_count`, `finding_label_counts`)? | Langfuse |
| Why was a request blocked (`block_reason`), and which team was it? | Langfuse |
| What is the audit trail for the last 90 days? | Langfuse |

Short version: **the LiteLLM UI is not served** — the route gate refuses it
with the rest of litellm's management surface (`docs/security.md` §14).
**Langfuse = request-level traces and the audit trail.** Neither ever holds original user content —
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
  the HTTPS front door (see "HTTPS front door (nginx)") or an SSH tunnel;
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

That last sentence is the whole trade-off, and it is a documented deviation from
`docs/security.md` §8's default for `vectorBufferFull` — read "Audit buffering
is not fail-closed" below before you rely on it.

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

**Through the front door** (a profile on, see "HTTPS front door (nginx)"):
`https://langfuse.<GATEWAY_DOMAIN>` under `nginx`,
`https://<address>:<NGINX_LANGFUSE_PORT>` under `nginx-ports`. nginx forwards
it to `langfuse-web:3000`, and `LANGFUSE_PUBLIC_URL` must be exactly that
origin.

**Without it**, an SSH tunnel. Langfuse publishes nothing on the host, so tunnel
to the container address (the docker bridge is routable from the host on Linux):

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
behind nginx; the two cannot be correct at the same time, so set the one in
use. With a profile on, nginx refuses to start unless it is `https://` (exit 69).

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
`CORP_LANGFUSE_SECRET_KEY`. Everything is correct before the first container
starts and there is nothing else to do.

If you provision through the UI instead, **do not let `vector` run before the
real keys are in `.env`.** Langfuse answers a wrong key with `401`, and Vector's
HTTP sink does **not** retry an auth failure — 401 is not in its retriable set
(408/429/5xx), so every audit record posted with a placeholder key is logged as
`Events dropped` and gone. The disk buffer does not help: the record has already
left it. The sequence is therefore:

```
# 1. placeholder values in the two CORP_LANGFUSE_* keys — they have no default,
#    so `docker compose config` fails while they are empty
# 2. bring the stack up WITHOUT the audit forwarder
docker compose up -d --scale vector=0
# 3. create the project API keys in the UI, put the real values in .env
# 4. now start vector
docker compose up -d
```

**What step 4 does and does not recover.** `vector` has no read checkpoint yet,
and its source is `read_from: beginning`, so its first run replays every log file
the glob matches, from byte 0: the `litellm` container's active `-json.log` **and
its numbered rotations** (`-json.log.1`, `.2`, …, which the glob covers for
exactly this reason). What it cannot recover is anything docker has already
discarded — records rotated past `LITELLM_LOG_MAX_FILE` are gone from the host
and no replay reaches them. In practice steps 2–3 are minutes of low traffic and
sit far inside one `LITELLM_LOG_MAX_SIZE` file, so nothing is lost; the claim is
bounded by log retention, not unconditional. See "Audit buffering is not
fail-closed".

The same hazard applies after first boot: if the project keys are rotated in
Langfuse and `.env` is not updated, `vector` keeps posting and keeps dropping.
`docker compose logs vector | grep "Events dropped"` is the check that surfaces
it.

### Recovering records vector dropped with a wrong key

This is the case the workflow above exists to prevent, and it needs a deliberate
recovery — restarting `vector` does **not** perform one. The file source
checkpoints on *read*, not on delivery (end-to-end acknowledgements are off), so
a record that Langfuse rejected with `401` was already counted as consumed and
the checkpoint moved past it. On restart Vector resumes after those bytes and
they are never re-read.

To replay them, delete the file source's checkpoint. It lives in `data_dir`
(`/var/lib/vector`, on the `vector-data` volume) under the source's component
name, and holds only a content fingerprint plus a byte offset per file — no log
content:

```
# 1. fix CORP_LANGFUSE_PUBLIC_KEY / CORP_LANGFUSE_SECRET_KEY in .env FIRST.
#    Replaying with the same wrong key just drops everything a second time.
docker compose stop vector

# 2. drop the read checkpoint (the disk buffer under /var/lib/vector/buffer is
#    NOT touched — those events are still queued for delivery)
docker compose run --rm --no-deps --entrypoint sh vector \
  -c 'rm -f /var/lib/vector/container_logs/checkpoints.json'

# 3. read_from: beginning now applies again
docker compose up -d vector
docker compose logs vector | grep "Events dropped"   # expect nothing new
```

Two things to know before you run it:

- **It replays everything still on disk, not just the dropped window.** Records
  Langfuse already accepted are posted again. Each carries the same
  `body.id` (the gateway's `request_id`), and Langfuse's ingestion API is an
  upsert on that id, so the expected result is an overwrite rather than a
  duplicate trace — confirm on a small window before running it against a busy
  instance.
- **It only reaches bytes docker still has.** Rotated-away records are
  unrecoverable, full stop: the gateway wrote them to stdout and kept no copy,
  so once the log file holding them is gone there is nothing left to replay
  from. That is the real reason the first-boot workflow starts with
  `--scale vector=0` instead of relying on recovery, and the reason
  `LITELLM_LOG_MAX_SIZE` × `LITELLM_LOG_MAX_FILE` is a sizing decision rather
  than a default to leave alone.

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
with a `file` source: data in, no control plane, no write path. Four
consequences worth knowing:

- the glob is `*/*-json.log` plus `*/*-json.log.[0-9]` and its two-digit form, so
  the sibling `config.v2.json` / `hostconfig.json` files — which hold every
  container's environment — are never read. **Rotated logs are read too.** Docker
  renames `<id>-json.log` to `<id>-json.log.1` on rotation; a glob covering only
  the active file would lose
  every record that rotated away while Vector was down or behind. Re-reading is
  safe because Vector identifies a file by content fingerprint, not path — a
  renamed file keeps its checkpoint, so already-shipped bytes are not re-sent.
  The patterns stop at two digits on purpose: `…-json.log.*` would also match
  `…-json.log.1.gz`, and Vector's file source cannot decompress. The service
  therefore pins `compress: "false"` — see the last bullet;
- the source sees **every** container's logs, not just `litellm`, because the
  log directory is keyed by container id and the glob cannot be narrowed. The
  scoping is done one transform later — see "Container identity boundary";
- it needs the **`json-file` logging driver**, which is why the `litellm`
  service pins `logging.driver: json-file` rather than inheriting the host
  default. Under a daemon-wide `local`, `journald` or remote driver the other
  containers produce no `*-json.log` files, but `litellm` — the only one this
  pipeline reads — still does;
- **pinning the driver costs the daemon's rotation policy, so the service sets
  its own.** Docker merges daemon-level `log-opts` into a container *only when
  the container's driver equals the daemon's default driver* (moby
  `daemon/logs.go`, `mergeAndVerifyLogConfig` — the copy loop sits inside
  `if cfg.Type == daemon.defaultLogConfig.Type`). On a `local`-default host the
  pinned service would therefore inherit nothing, and `json-file`'s own
  `max-size` default is `-1`: no rotation at all, an unbounded audit log on the
  same disk as Cache B and Postgres. `logging.options` sets `max-size` /
  `max-file` explicitly (`LITELLM_LOG_MAX_SIZE` / `LITELLM_LOG_MAX_FILE`,
  default `100m` × `10` ≈ the 1 GiB disk buffer). Removing them does **not**
  hand the job back to the daemon;
- **the same merge runs the other way for `compress`, so the service pins it
  off.** On a host whose daemon default *is* `json-file` and carries
  `compress=true`, the merge copies that option into any container that has not
  named it — docker then writes rotations as `…-json.log.1.gz`, which Vector's
  glob does not match and Vector could not read if it did. Every record that
  rotated while Vector was down or back-pressured would be lost silently.
  Measured on a docker 29.7.1 daemon defaulting to json-file +
  `max-size=1k`/`max-file=4`/`compress=true`: a container pinning only the two
  size options rotated to `…-json.log.{1,2,3}.gz`; adding `compress: "false"`
  gave plain `…-json.log.{1,2,3}`. It is not an `.env` key — widening the
  Vector glob to `…-json.log.*` is not an alternative fix, it just matches
  files Vector still cannot decompress.

**Container identity boundary.** `docker-compose.yml` puts a
`com.corp-llm-gateway.audit-source: gateway-stdout` label on the `litellm`
service and names that label in the service's `logging.options.labels`. That
second half is what makes docker's json-file driver copy the label into every
log record it writes for that container:

```
{"log":"...","stream":"stdout",
 "attrs":{"com.corp-llm-gateway.audit-source":"gateway-stdout"},"time":"..."}
```

Vector's first transform, `gateway_container_only`, admits a line only if that
`attrs` stamp is present — before the application content is unwrapped, and
before anything downstream sees it. So feeding the audit pipeline requires
control over container creation on this host, not merely the ability to print a
line. All three pieces (label, log option, filter) are one mechanism; remove any
one and the pipeline either goes silent or loses its boundary.

**It is a misconfiguration guard, not a security boundary — and the difference
is a production requirement.** The label name and its value are public, printed
in this file and in the config; nothing about them is secret or verified.
Anyone who can run `docker run` on this host can start a container with the same
`--label com.corp-llm-gateway.audit-source=gateway-stdout` and the same
`--log-opt labels=com.corp-llm-gateway.audit-source`, print `AuditEvent`-shaped
JSON, and have it pass this filter, the NEVER-fields gate and `audit_only` —
forging audit records in Langfuse. The filter raises the bar from "can write a
log line" to "can start a container here", and no further.

So: **no untrusted `docker run` on the host that runs this stack.** Treat
membership of the `docker` group (and any CI runner, agent or sidecar with
socket access) as equivalent to write access to the audit trail, and restrict it
accordingly. This is a limitation of reading every container's logs; closing it
properly needs a private channel only `litellm` can write — a dedicated
bind-mounted audit file or unix socket — which is a change to the gateway's
audit sink in `src/`, not to this stack. Recorded in `docs/security.md` §8.2.

`audit_only` stays what it always was: a **schema** gate that keeps
non-`AuditEvent` lines (litellm's own JSON wrappers, uvicorn access logs) out.
It is not a trust boundary — `request_id` and `redaction_count` are two ordinary
keys, and before this filter existed any co-located container that logged a JSON
line carrying them was forwarded into the audit store.

Vector runs with a read-only root filesystem and all capabilities dropped. It
stays root (the image default) because the log directory is mode `0710
root:root`; there is no `user:` override to add.

**Delivery is durable against transient failure.** Read checkpoints and the
sink's disk buffer live on the `vector-data` named volume, so a Vector restart
resumes at the exact byte it stopped at — records written while it was down are
delivered, not skipped, and none are re-sent. The Langfuse sink buffers to disk
with `when_full: block` and retries indefinitely with backoff; see "When the
Langfuse queue fills" for why that combination is load-bearing rather than a
default. If the disk buffer does fill, Vector back-pressures the file source and
stops reading — the records stay in the docker log files and are picked up from
the checkpoint once Langfuse recovers.

"Transient" is the operative word. Vector's HTTP sink retries `408`, `429` and
`5xx`; every other `4xx` is final and the record is dropped with an
`Events dropped` error in `docker compose logs vector`. In practice that means a
**wrong or rotated `CORP_LANGFUSE_*` key** (`401`) is silent audit loss, not a
retry — see "First login". Vector 0.53 has no setting that changes which status
codes are retriable, and its `request:` block ignores unknown keys without
complaining, so an invented option there would validate and do nothing.

### Audit buffering is not fail-closed

`docs/security.md` §8 lists `vectorBufferFull` as **fail-closed (503) by
default, with `audit_buffer_full=continue` available as a per-team opt**. **This
stack takes the `continue` opt, and cannot do otherwise.** Say it plainly: a
stalled audit path here does **not** stop requests from egressing.

Why: audit delivery is out-of-process. `CORP_AUDIT_SINK` is unset, so the
gateway's only audit action is writing a line to its own stdout, which always
succeeds. Vector reads that log file afterwards, from a different container.
There is no signal path from Vector's buffer state back into `pre_call` /
`post_call`, so nothing in the request path can see the stall, and `when_full:
block` only stops Vector *reading* — it never reaches the gateway. A real
request-path fail-closed needs a health/buffer gate inside
`src/corp_llm_gateway/`; it does not exist yet.

That is a deliberate deviation from the matrix default, not an oversight, and it
is the price of not making the gateway process depend on the audit forwarder's
health. The residual risk: while Langfuse is unreachable, accepted audit records
live **only in the `litellm` container's docker json-file logs** (plus whatever
already made it into Vector's disk buffer). Log retention — not the disk buffer —
is therefore what actually bounds durability. Vector's glob does follow the
numbered rotations (`-json.log.1`, `.2`, …), so a single rotation no longer
discards anything; but a file rotated past `LITELLM_LOG_MAX_FILE` is deleted by
docker, and those audit records are gone permanently.

Two things an operator must configure before going live:

1. **Size the docker log retention** for the `litellm` container against the
   longest Langfuse outage you intend to survive, at your own audit volume. The
   knobs are `LITELLM_LOG_MAX_SIZE` / `LITELLM_LOG_MAX_FILE` (default `100m` ×
   `10`), which the service sets on its own `logging.options`. **Do not set them
   in the daemon's `log-opts` and expect them to apply** — docker merges
   daemon-level log-opts into a container only when the container's driver
   equals the daemon's default, and this service pins `json-file` because the
   pipeline needs it. On a host defaulting to `local` or `journald` the daemon
   policy is silently ignored for this container. The product
   `max-size × max-file` must exceed the audit bytes produced during that
   window; `CORP_VECTOR_LANGFUSE_BUFFER_BYTES` should be sized for the same
   window (the defaults match at ~1 GiB).
2. **Alert on a stalled or lossy audit path.** Nothing does this for you — the
   `vector` healthcheck only proves the process is alive, and `langfuse-redis`
   answers `PING` while full. The three checks that surface it:

```
# buffered but undelivered — grows while langfuse is unreachable
docker compose exec vector du -sh /var/lib/vector/buffer

# dropped outright — a wrong key, or any other non-retriable response
docker compose logs vector | grep "Events dropped"

# how much rotation headroom is left? one `-json.log` plus N `-json.log.<n>`
# files; at LITELLM_LOG_MAX_FILE the next rotation DELETES the oldest, and with
# it any audit record vector has not read yet
docker compose exec vector ls -l \
  "$(dirname "$(docker inspect -f '{{.LogPath}}' "$(docker compose ps -q litellm)")")"

# how far behind is vector? one {fingerprint, position} per file it has read —
# offsets only, never log content
docker compose exec vector cat /var/lib/vector/container_logs/checkpoints.json
```

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

The `litellm` port publishes on `127.0.0.1`, not `0.0.0.0`, with or without
the front door. It speaks plain HTTP, and subscription tokens, virtual keys and
`X-Corp-Auth` must not cross a server-class network in cleartext: developers
reach the gateway across the network only through the HTTPS front door, which
talks to `litellm` over the compose network. With no profile on it is the only
published port in the stack: no Langfuse service publishes one at all (see
"Langfuse"), and `vector`'s API is bound to `127.0.0.1` *inside* its own
container, purely so its healthcheck can reach it. The admin path — the SSH
tunnel to this port — stays as it is.

## HTTPS front door (nginx)

Off by default. With `COMPOSE_PROFILES` unset the stack is exactly the one
above: no nginx, `litellm` on loopback only, and no `NGINX_*` key is read. With
a profile on, an nginx (`nginx:1.27-alpine`) publishes the gateway and Langfuse
over HTTPS. Its configuration is `compose/nginx/`: `entrypoint.sh` validates the
keys, renders one listener from `templates/` and runs `nginx -t` before it
serves; `nginx.conf` is static. Every key, with its default, is in
`.env.example` ("nginx front door").

### Turning it on

One line in the server's `.env` is the only switch:

```
COMPOSE_PROFILES=nginx        # or nginx-ports; never both
```

`deploy.sh` has no flag for it and never sets it, and the boot-time unit runs a
bare `docker compose up -d` that reads the same `.env`: a deploy and a reboot
start the same services. `deploy.sh up` refuses a `.env` that enables both
profiles before it pulls anything, and fails at once, naming the service, when
the front door exits or restarts — `deploy.sh logs nginx` (or `nginx-ports`)
shows the entrypoint's one-line reason.

| Profile | Routing | Published (host → container) | Needs |
|---|---|---|---|
| `nginx` | by name: `gateway.<GATEWAY_DOMAIN>` and `langfuse.<GATEWAY_DOMAIN>` on one port | `${NGINX_BIND_ADDR}:${NGINX_PORT:-443}` → `8080` | DNS records for both names, pointing at this host or at the load balancer in front of it |
| `nginx-ports` | by port, no DNS: the gateway on `NGINX_PORT`, Langfuse on `NGINX_LANGFUSE_PORT`, whatever name the client uses | the line above plus `${NGINX_BIND_ADDR}:${NGINX_LANGFUSE_PORT:-8443}` → `8081` | the IP address or local name clients dial |

Langfuse gets an origin of its own under both profiles: Langfuse v3 cannot be
hosted on a sub-path.

`NGINX_BIND_ADDR` defaults to `127.0.0.1`, so a profile with nothing else set
answers on loopback only. Set it to the address of the NIC the clients
(`terminate`) or the load balancer (`behind-proxy`) reach. An IPv6 address must
be bracketed (`[fd00::1]`). There is no "all interfaces" default on purpose: an
empty host IP makes Compose publish on every interface.

### The two TLS modes

The public endpoint is always HTTPS. There is no plain-HTTP public listener and
no `:80 → :443` redirect. `NGINX_TLS_MODE` is **required and has no default**:
unset, empty or misspelled, nginx does not start (exit 64). The two modes
differ only in who terminates TLS:

| `NGINX_TLS_MODE` | nginx speaks | TLS terminated by | Certificate on this host |
|---|---|---|---|
| `terminate` | TLS 1.2 / 1.3 | nginx | yes — `compose/nginx/certs/` |
| `behind-proxy` | plain HTTP, on the internal hop only | the admins' load balancer | no |

**Which one:** your admins already publish HTTPS in front of this host and
forward HTTP to it internally → `behind-proxy`. You own the edge, or the load
balancer re-encrypts to this host → `terminate`.

- **`terminate`** — nginx is the public TLS endpoint. `NGINX_TLS_CERT` and
  `NGINX_TLS_KEY` name the certificate and key in `compose/nginx/certs/`. Every
  answer carries `Strict-Transport-Security: max-age=31536000`, the edge's own
  429 included. Under `nginx`, a TLS handshake for any other name (or none) is
  rejected. Every client that reaches the port is served; there is no peer
  check.
- **`behind-proxy`** — nginx serves only the peers in `NGINX_TRUSTED_PROXIES`
  and closes the connection with no response (nginx's `444`) for everyone else:
  the bind address picks a NIC, it is not access control. `NGINX_BIND_ADDR` may
  not be the unspecified address in any spelling (`0.0.0.0`, `::`, `[::]`, …;
  exit 68) — that would be a plain-HTTP listener on every interface. No HSTS
  here: the terminator owns that header.

`NGINX_TRUSTED_PROXIES` is a space-separated list of IPs or CIDRs, prefix at
least `/8` (IPv4) or `/16` (IPv6): the terminator's address **as nginx sees it
inside the container**. It does two jobs:

- **It stops source-IP spoofing.** Only a listed peer may set the client address
  through `X-Forwarded-For` (`real_ip`, one hop). A wide range would let any
  client choose its own address, and that address keys the edge limits for
  requests without a corp token. That is why `/0`-style prefixes are refused.
- **In `behind-proxy` it is access control.** It is required there, and a wrong
  value is a hard outage, not a degradation: every request gets `444`. The
  access log records each refused peer — `"realip_remote_addr"` with
  `"from_trusted_proxy":"0"` and status `444` — and that address is the value to
  list. At least one entry must be IPv4 (exit 66 otherwise): the listeners are
  IPv4.

In `terminate` the list is optional (a load balancer that re-encrypts). Either
way nginx sends `X-Forwarded-Proto: https`, the constant, to both upstreams; the
inbound header is never read.

`LANGFUSE_PUBLIC_URL` must be the public `https://` origin whenever a profile is
on — **in `behind-proxy` too**: nginx speaks HTTP there, but the browser uses
the load balancer's HTTPS origin, and that is what NextAuth checks. nginx
refuses to start otherwise (exit 69). See "Reaching the UI".

### Certificates (`terminate`)

Bring your own; `compose/nginx/certs/README.md` is the reference (SANs, file
modes, a corp-CA example). In short:

- **SANs:** one certificate for `gateway.<GATEWAY_DOMAIN>` and
  `langfuse.<GATEWAY_DOMAIN>` under `nginx`; under `nginx-ports`, the IP address
  or local name clients dial (an IP SAN for an address).
- **Files:** the full chain (leaf first), and the key unencrypted and `0600`,
  named by `NGINX_TLS_CERT` / `NGINX_TLS_KEY` as bare file names.
- **Installed on the server**, in `nginx/certs/` of the deploy directory
  (`/opt/corp-llm-gateway/nginx/certs/` by default). `deploy.sh` never syncs
  anything there except the README, and git ignores the rest.
- **Rotation:** replace both files, then `docker compose restart nginx` (or
  `nginx-ports`). nginx reads the certificate at start, and the entrypoint
  checks the new files first. nginx does not warn before a certificate expires.
- **Self-signed, for pilots and tests only:**
  `scripts/deploy/make-selfsigned-certs.sh --domain <domain> [SAN …]` makes a
  throwaway CA and a leaf signed by it. Verify with
  `curl --cacert compose/nginx/certs/selfsigned-ca.crt …`, never with `-k`.
  Claude Code reads an extra CA from `NODE_EXTRA_CA_CERTS`.
- **ACME / Let's Encrypt is out of scope:** it needs inbound port 80 from the
  internet or API control of the public DNS zone, plus one more container.

### What nginx admits

An exact-path allow-list, the same under both TLS modes, both routings and both
auth modes (`nginx/templates/snippets/gateway-locations.inc.template` is the
only copy). Each entry is one exact path and the one method it admits:

| Method + path | What |
|---|---|
| `POST /v1/messages` | Anthropic Messages — Claude Code |
| `POST /v1/chat/completions` | OpenAI Chat Completions |
| `POST /v1/responses` | OpenAI Responses — Codex; HTTP only |
| `GET /v1/models` | model listing; no body |
| `GET /healthz/live` | the gateway's own liveness probe; never rate-limited |
| `POST /internal/issue-token` | developer token issuance (`scripts/install.sh`); a body over 1 KiB is 413 — the gateway refuses any body anyway |

**Every other path is 404 from nginx**, before a byte of the body is read and
without reaching the gateway. Another method on an admitted path is 403
(`limit_except`), and `HEAD` is admitted wherever `GET` is (`limit_except GET`
cannot refuse it). The route gate refuses the same routes inside the
image (`docs/security.md` §14), so this is defence in depth, and nginx never
admits a route the gate refuses (`tests/compose/test_nginx_allowlist_routes.py`).
Worth knowing by name:

- **`/v1/embeddings`, `/v1/completions`, `/v1/moderations`, `/v1/audio/*`** —
  the guardrail does not rewrite these bodies, so a public door that admitted
  them would be a door for unsanitized text.
- **`/v1/messages/count_tokens` and `/v1/responses/input_tokens`** — litellm
  serves them without the guardrail: the raw prompt would reach the provider's
  token counter unsanitized and unaudited. Token counting is advisory for
  Claude Code, and the gateway refuses both routes on the tunnel too (403).
- **The Responses WebSocket transport** (`/v1/responses` with `Upgrade`) —
  frames after the handshake never pass the guardrail. nginx never forwards an
  upgrade to the gateway, and the `POST`-only rule refuses the handshake.
- **Polling a `background: true` response** — `POST /v1/responses` with
  `background: true` is accepted, but `GET /v1/responses/{id}` (and cancel,
  `input_items`, `/v1/responses/compact`) is 404. Desanitization is keyed to
  the request that created the response, so a later poll would return
  placeholders, not originals.
- **litellm's admin surface and UI, `/health*`, `/healthz/ready`,
  `/healthz/sanitization`, `/healthz/extensions` and `/metrics`** — these stay
  on the SSH tunnel to `127.0.0.1:${GATEWAY_PORT:-4000}`, as before.

Past a per-token limit nginx answers 429 `E_RATE_LIMITED` itself:
`docs/ops/capacity.md`, "Edge limits". Streams are not buffered (SSE passes
through as the gateway sends it), and bodies up to 25 MiB pass: the gateway's
100 KiB figure is a per-text-leaf threshold, not a body cap.

### Logs

One JSON access-log line per request on nginx's stdout
(`docker compose logs nginx`, or `nginx-ports`): the peer and client address,
`from_trusted_proxy`, host, method, path (`$uri`), status, upstream status and
time. It carries no `Authorization`, no `X-Corp-Auth`, no body and no query
string. The error log is set to `crit`: at `error` and `warn` nginx appends the
full request line, query string included, to its messages. Diagnose a 502/504
from the access log's `status` and `upstream_status`. The healthcheck's own
requests are not logged.

### Troubleshooting

The entrypoint writes one line naming the key it refused, then exits:

| Exit | Cause |
|---|---|
| 64 | `NGINX_TLS_MODE` is not exactly `terminate` or `behind-proxy`; `GATEWAY_DOMAIN` is missing or not a lowercase DNS name with at least two labels (under `nginx`); an edge-limit key (`NGINX_TOKEN_RATE`, `NGINX_TOKEN_BURST`, `NGINX_TOKEN_CONN`, `NGINX_ISSUE_RATE`) is not a whole number from 1 to 999999; or the service's routing argument is not `host` / `port` (an edited compose file) |
| 65 | `terminate`: `NGINX_TLS_CERT` or `NGINX_TLS_KEY` is not a bare file name (`A-Z a-z 0-9 . _ -`), or that file in `compose/nginx/certs/` is missing, not a regular file, or empty |
| 66 | an `NGINX_TRUSTED_PROXIES` entry is not an IP/CIDR with a prefix of at least `/8` (IPv4) or `/16` (IPv6); or, in `behind-proxy`, the list is empty or has no IPv4 entry |
| 67 | a template is missing, or names a variable the entrypoint does not render — a damaged `nginx/templates/`, not a setting |
| 68 | `behind-proxy` with `NGINX_BIND_ADDR` set to the unspecified address in any spelling |
| 69 | `LANGFUSE_PUBLIC_URL` is not `https://<host>[:port][/]` — a `:` with no port, a path, credentials, a bracketed IPv6 literal or a host that is neither a DNS name nor a valid IPv4 address is refused; or, under `nginx`, it names a host other than `langfuse.<GATEWAY_DOMAIN>` |

Any other non-zero exit is `nginx -t` rejecting the rendered config; its own
message is in the same log. Facts that surprise operators:

- **The listeners are IPv4** (`listen 8080`). An IPv6-only
  `NGINX_TRUSTED_PROXIES` could never match a peer, so `behind-proxy` refuses it
  (exit 66); `::ffff:a.b.c.d` never matches an IPv4 peer either.
- **An untrusted peer can still get a `400`.** nginx answers a malformed request
  (no `Host`, an oversize header) with its own 400 before the trusted-peer
  check runs. It tells the peer that nginx listens there — a fingerprint, never
  content. By design.
- **`%3F` in a path reaches the log decoded.** The access log records `$uri`,
  which nginx percent-decodes: `GET /x%3Fcode%3DSECRET` is logged as
  `"uri":"/x?code=SECRET"`. "No query string in the log" holds for a literal
  `?` only. Only a client that encodes a secret into its own path can cause it;
  a header or a body never reaches the log.
- **An absolute-form request line picks the server.**
  `POST http://gateway.<domain>/v1/messages` with a different `Host:` is routed
  by the name in the request line, so a `Host` naming neither origin does not
  always get no response. The allow-list applies all the same — no bypass — and
  the gateway receives the name nginx routed on.
- **HSTS:** every `terminate` answer carries it, the edge's 429 included; no
  `behind-proxy` answer does.
- **A replaced certificate** takes effect only after
  `docker compose restart nginx` (or `nginx-ports`).

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

## In-flight cap

The `litellm` service passes the route gate's in-flight cap keys
(`docs/ops/capacity.md`, `docs/ops/configuration.md`). Set any of them in `.env`
to change it; the commented block in `.env.example` shows the defaults.

| Key | Default here | What it bounds |
|-----|--------------|----------------|
| `CORP_LLM_MAX_INFLIGHT` | `64` | concurrent LLM requests on this host, a slot held for the whole stream; 429 `E_CAPACITY` past it. `0` = off, refused at boot under `CORP_ENV=production` |
| `CORP_LLM_CANCEL_GRACE_SECONDS` | `5` | unwind budget after a client disconnect; a slot is back within 2 × this |
| `CORP_LLM_BODY_READ_SECONDS` | `30` | time for the whole body to arrive, before a slot is taken; 408 `E_BODY_TIMEOUT` past it |
| `CORP_LLM_MAX_DRAINING` | unset → 4 × the cap | requests reading a body at once; passed by bare name so it follows the cap |
| `CORP_LLM_MAX_DRAINING_BYTES` | `536870912` (512 MiB) | body bytes buffered at once — size it against what the host can spare |

A bad value is a boot refusal (exit 78) with the key named in the log.

## Developer token issuance

`POST /internal/issue-token` is off unless you add the optional
`docker-compose.issuance.yml` overlay. The base stack passes no
`CORP_GATEWAY_ISSUE_*` key and mounts no config file, so a subscription deploy
without Keycloak is unaffected. To turn issuance on:

1. On the server, in the deploy directory: `cp gateway/config.toml.example
   gateway/config.toml` and edit the team map
   (`CORP_GATEWAY_ISSUE_OIDC_TEAM_MAP`). It is a TOML table, so it can only live
   in this file. Order matters: the first listed group the user belongs to wins.
   `gateway/config.toml` is gitignored, and `deploy.sh` never syncs it: the
   server's copy is the only copy.
2. Set the scalar keys — at least `CORP_GATEWAY_ISSUE_OIDC_ISSUER`, `_AUDIENCE`
   and `_CLIENT_ID` — in `.env` (the commented block in `.env.example`) or in
   `gateway/config.toml`. The overlay passes them by bare name, so an unset key
   in `.env` does not shadow the file; set each key in one place only.
3. Deploy with the overlay from your laptop:
   `scripts/deploy/deploy.sh --host user@server --issuance up`
   (or `DEPLOY_ISSUANCE=1`). It refuses before it changes anything if
   `gateway/config.toml` is missing on the server, and refuses `--mode
   virtual-keys`. Pass `--issuance` to every later `logs`/`status`/`down`/
   `restart` run too. Without the script, run this on the server:
   `docker compose -f docker-compose.yml -f docker-compose.oauth.yml -f docker-compose.issuance.yml up -d`.
   For the autostart unit, uncomment the three-file `COMPOSE_FILE` line in `.env`.

The overlay mounts `./gateway/config.toml` read-only at
`/etc/corp-llm-gateway/config.toml` and sets `CORP_LLM_GATEWAY_CONFIG_FILE` to
that path. A missing `gateway/config.toml` fails `up` rather than booting with
issuance off. Keys, defaults and ranges:
`docs/ops/configuration.md`, "Developer token issuance"; Keycloak setup:
`docs/ops/install.md`, "Developer onboarding". The `corp_tokens` schema staged
by `deploy.sh` carries the issuance columns, but init scripts run only on an
empty volume: on an existing one, re-run `tokens/schema.sql` first
(`docs/ops/upgrade.md`). `gateway-admin token issue` stays as break-glass.

## Operator CLI (gateway-admin)

`gateway-admin team create` / `token issue` and other RBAC-gated mutations
need either `CORP_GATEWAY_ADMIN_TOKEN` (an operator JWT) or
`CORP_GATEWAY_RBAC=0` (dev bypass) — **neither is set in the `litellm`
service's environment today**. Export one of them on the machine running
`gateway-admin` before issuing tokens or creating teams against this
deployment; see `docs/ops/admin-cli.md`.
