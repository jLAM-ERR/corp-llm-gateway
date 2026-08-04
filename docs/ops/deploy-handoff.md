# corp-llm-gateway — deployment handoff

A handoff document for whoever runs the deploy. This is the condensed sequence;
the full mode matrix and every failure mode live in `deployment-modes.md`, the
variable reference in `configuration.md`, and the on-call procedures in
`runbook.md`. Russian mirror: `deploy-handoff.ru.md`.

## What you are deploying

A gateway between the developers' Claude Code and the external LLMs (Anthropic /
OpenAI). It strips personal data and corporate secrets out of the request
**before** it leaves the perimeter and restores them in the response. Everything
that goes upstream is written to the audit trail.

The stack (docker compose, one host): `litellm` (the gateway), `postgres` (team
tokens + team config + litellm's virtual-key database), `redis` (the mappings
used to put the originals back), `langfuse` + `clickhouse` + `minio` (traces and
audit), `vector` (audit delivery).

---

## Step 0. Host requirements

- Debian/Ubuntu or the RHEL family. The bootstrap script refuses other
  distributions rather than guessing.
- Docker Engine + the compose v2 plugin. The script installs both from Docker's
  official repository if they are missing.
- **CPU-only.** No GPU is needed and none is supported.
- Disk: headroom for docker's logs and Vector's buffer — by default ~1 GiB of
  litellm log rotation plus a 1 GiB Vector disk buffer.
- Egress: `api.anthropic.com` and/or `api.openai.com`; plus the corp vLLM and the
  corp NER service if you enable them.

## Step 1. Prepare the server (day 0, once, on the server itself)

```
sudo scripts/deploy/bootstrap-server.sh
```

What it does: checks for / installs Docker, creates `/opt/corp-llm-gateway`,
drops a `0600` `.env` there from the template — **and exits 1** so that you fill
the `.env` in first. That exit is the expected behaviour, not a failure.

**Do not pass `--systemd` on this run:** the autostart unit is only installed
once the compose files are already in the directory, otherwise it would fail on
every boot. Come back to it after step 5:

```
sudo scripts/deploy/bootstrap-server.sh --systemd
```

The script is idempotent: an existing `.env`, directory, package repository and
systemd unit are left alone.

## Step 2. Fill in `.env`

The file is `/opt/corp-llm-gateway/.env`, mode `0600`. It exists **only on the
server** — the deploy script never uploads, reads or prints it.

Without these keys `docker compose` refuses to start with an explicit
"set X in .env":

| Key | How to get it |
|---|---|
| `POSTGRES_PASSWORD` | `openssl rand -hex 32` |
| `GATEWAY_IMAGE_TAG` | already filled in (`1.0.0-rc.6`), see step 3 |
| `LANGFUSE_POSTGRES_PASSWORD` | `openssl rand -hex 32` |
| `LANGFUSE_CLICKHOUSE_PASSWORD` | `openssl rand -hex 32` |
| `MINIO_ROOT_PASSWORD` | `openssl rand -hex 32` |
| `LANGFUSE_NEXTAUTH_SECRET` | `openssl rand -base64 32` |
| `LANGFUSE_SALT` | `openssl rand -base64 32` |
| `LANGFUSE_ENCRYPTION_KEY` | `openssl rand -hex 32` — **exactly 64 hex characters**; changing it later makes already-encrypted rows unreadable |
| `CORP_LANGFUSE_PUBLIC_KEY` | a Langfuse **project** key `pk-lf-…`, see below |
| `CORP_LANGFUSE_SECRET_KEY` | a Langfuse **project** key `sk-lf-…`, see below |

The rest depends on the mode — see step 4.

### About the Langfuse key pair — read this before the first start

`CORP_LANGFUSE_PUBLIC_KEY` / `CORP_LANGFUSE_SECRET_KEY` are **Langfuse project
keys**, not the `LANGFUSE_*` infrastructure secrets in the table above. They are
what Vector authenticates with when it delivers audit records.

Empty values are safe: `docker compose config` fails, Vector never starts, and
nothing is lost — on its first run it reads the log from the beginning.
**Wrong values are not safe:** Vector starts, Langfuse answers `401`, and its
HTTP sink does not retry an auth failure (only 408/429/5xx) — those audit records
are **lost**.

The easiest way around it is headless init: uncomment the `LANGFUSE_INIT_*` block
in `.env` and set `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `_SECRET_KEY` to the same
values as `CORP_LANGFUSE_*`. The project, the first user and the keys are then
created on the very first start.

If you provision the project through the UI instead, keep Vector out of the stack
until the real keys are in place: `docker compose up -d --scale vector=0`.

## Step 3. Choose the image

`GATEWAY_IMAGE_TAG=1.0.0-rc.6` is the published image. It is **older** than the
branch that added the corp NER integration, the Luhn rule for bank cards and the
Cache A boundary fixes.

- Corp NER **not needed** → leave the tag as it is, the image is pulled ready.
- Corp NER **needed** → build from source:

```
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

The build overlay deliberately selects the `ru-en` NER profile: the default
profile (`base`) has no English model, and the stack ships with
`CORP_LLM_REQUIRE_NER=1`, so that image would answer 503 to every request.

## Step 4. Choose the authentication mode

There are two modes, they are **mutually exclusive**, and both are production.
The decision is made before you fill in `.env`.

### Mode A — corporate API keys

The gateway holds the provider keys. Developers call with a **LiteLLM virtual
key** issued from the admin UI; that gives per-person revocation and spend
accounting.

Additionally in `.env`:

| Key | Value |
|---|---|
| `LITELLM_MASTER_KEY` | `sk-` + `openssl rand -hex 32` |
| `UI_USERNAME`, `UI_PASSWORD` | the admin-UI account |
| `ANTHROPIC_API_KEY` | needed for the `claude-*` route |
| `OPENAI_API_KEY` | needed for the `gpt-*` route |

Provider keys are **optional**: set only the ones you actually call. If you use
the corp vLLM (`corp-*`) alone, you need neither.

Start:

```
cd /opt/corp-llm-gateway
docker compose up -d
```

If `LITELLM_MASTER_KEY`, `UI_USERNAME` or `UI_PASSWORD` are unset, the container
refuses to start and names the variable. That is a protection, not pedantry:
without a master key litellm skips its authorization check entirely, so a missing
key would silently remove this mode's only per-developer credential.

### Mode B — the developer's subscription (OAuth)

The developer's own Anthropic subscription token (Max/Pro) is forwarded upstream
untouched. **There is no corporate `ANTHROPIC_API_KEY` in this mode at all.**

In `.env`:

- **delete three lines entirely**: `LITELLM_MASTER_KEY`, `UI_USERNAME`,
  `UI_PASSWORD`;
- **uncomment** `COMPOSE_FILE=docker-compose.yml:docker-compose.oauth.yml`.

The second line is what makes autostart correct: the systemd unit runs a bare
`docker compose up -d`, and without it the stack would try to come up in Mode A
after a host reboot. It fails loudly rather than serving the wrong mode, but it
stays down until someone starts it by hand.

> **Do not leave them empty.** A blank `LITELLM_MASTER_KEY=` line counts as set:
> litellm keeps the empty value and enables proxy auth for anything that is not
> `None`. The gateway then refuses to boot and names the cause — better than
> silently answering 401 to every request. Delete the line, not the value.

Start:

```
cd /opt/corp-llm-gateway
docker compose -f docker-compose.yml -f docker-compose.oauth.yml up -d
```

What the overlay changes: it turns on the `CORP_LLM_FORWARD_ANTHROPIC_AUTH=1`
bridge and swaps litellm's config for an Anthropic-only one. Postgres, Redis,
Langfuse and Vector are untouched — audit works exactly as in Mode A.

**Only the `claude-*` route is served in this mode.** That is a load-bearing
security control, not a simplification: the hook gates the OAuth lift on the
client-visible model alias, but litellm resolves the actual upstream *after* the
hook runs, so the only guarantee that a subscription token cannot reach a
different provider is the absence of anything but `anthropic/` in the routing
table. Do not add a second route there.

The corp vLLM oracle still works: the gateway reaches it with its own HTTP
client, not through a litellm route.

## Step 5. Deploying from the operator's laptop (day N)

Rather than running `docker compose` by hand on the server, use the script — it
stages the token-store SQL schema, syncs `compose/`, runs `pull` + `up -d` and
waits for the healthchecks.

```
# mode A
scripts/deploy/deploy.sh --host user@server up

# mode B
scripts/deploy/deploy.sh --host user@server --mode oauth up
```

**The same `--mode` must be passed to every later run against that host** —
`logs`, `status`, `down` and `restart` all resolve the stack through this file
list. A run without `--mode oauth` reports on (or recreates) a different stack.

Other subcommands: `down` (volumes survive, asks for confirmation), `restart`,
`logs`, `status`. Useful flags: `--dry-run`, `--yes`, `--dir PATH`,
`--force-unlock`.

The local `.env` is never uploaded; the server's `.env` is never touched. Keys
and certificates are excluded from the sync.

> If you deploy by hand, without the script, stage the schema before the first
> start:
> `cp src/corp_llm_gateway/tokens/schema.sql compose/postgres/initdb/01-schema.sql`.
> Postgres init scripts only run once, against an empty volume.

## Step 6. Toggle — the corp vLLM oracle

The oracle is the **conditional fallback** at the end of the local detection
cascade. It is called only on a deterministic gazetteer hit, not on every
request.

**Off by default** (`CORP_LLM_ORACLE_ENABLED=0`). In that state the call is never
made and `CORP_LLM_ENDPOINT` is not needed for detection.

To enable:

```
CORP_LLM_ORACLE_ENABLED=1
CORP_LLM_ENDPOINT=https://<corp-vllm-host>/v1    # required once the oracle is on
```

Three things that bite:

- **Enabling it without a reachable endpoint fails requests on every route**, not
  just `corp-*`: a gazetteer hit falls through to a non-routable placeholder host
  and the request fails closed. Check the vLLM is reachable first.
- The `corp-*` route needs `CORP_LLM_ENDPOINT` **regardless** of this flag.
- **You cannot turn off the oracle and the local cascade at the same time.** With
  `CORP_LLM_ORACLE_ENABLED=0` and `CORP_LLM_LOCAL_FIRST=0` the gateway refuses to
  start — it does not run as a no-op sanitizer. `CORP_LLM_LOCAL_FIRST` is unset
  on this stack and defaults to `1`; just don't set it to `0`.

If the corp vLLM sits behind an internal CA, put the chain in
`compose/certs/corp-ca-bundle.pem` and uncomment `CORP_LLM_CA_BUNDLE`.

## Step 7. Toggle — the corp NER service

A remote NER detector appended to the local cascade. It goes **over the
network**, so it is excluded from code segments (otherwise source code would be
shipped to an external service); local detectors keep scanning code.

**Off by default** (`CORP_NER_ENABLED=0`) — no detector, no readiness probe, the
stack behaves as if the feature did not exist.

To enable — both variables together:

```
CORP_NER_ENABLED=1
CORP_NER_ENDPOINT=http://<ner-host>:<port>     # BASE url; the client appends /v1/analyze
```

- **Enabled without an endpoint is a boot refusal**, not a silent skip.
- **It needs a source build** (step 3): on the published `1.0.0-rc.6` these
  variables do nothing.
- Tuning (`CORP_NER_TIMEOUT_S`, `CORP_NER_MAX_TEXTS`, `CORP_NER_MAX_INPUT_CHARS`,
  `CORP_NER_CA_BUNDLE`) — leave the lines **commented**, not empty: an empty env
  var beats the config file. Defaults: 30 s, 256 texts, 200000 chars, no CA.
- The NER call carries **raw user content**, so TLS verification for it is never
  disabled. Use `CORP_NER_CA_BUNDLE` for an internal CA.

Flipping either toggle (oracle, NER) is **safe for the cache**: the key folds a
fingerprint of the effective detector policy, so entries made under different
settings cannot collide. No flush is needed.

Do not confuse `CORP_NER_ENABLED` (this remote service) with
`CORP_LLM_REQUIRE_NER` (the in-process RU/EN engines) — the latter stays `1` in
production either way.

## Step 8. Post-start checks

```
docker compose ps                                   # every service healthy
curl -fsS http://127.0.0.1:4000/health/liveliness    # the gateway answers
docker compose logs --tail=100 litellm
docker compose logs --tail=50 vector                 # audit is being delivered
```

Port `4000` is published **on loopback only** (`127.0.0.1:4000`). Langfuse
publishes no host port at all — reach the UI through an SSH tunnel (forward a
local `3000`).

> `/health/liveliness` is litellm's own probe. The `/healthz/*` endpoints
> (`ready`, `sanitization`) are **not mounted** on this stack; they belong to the
> k8s variant. Do not build monitoring for a compose deployment around them.

## Step 9. Issuing team tokens (`X-Corp-Auth`)

Team identity is the `X-Corp-Auth` header. It decides which rules, profile and
audit identity apply, and it is required **in both modes**. A request without a
valid token is refused.

```
gateway-admin team create --team-id team-x --name "Team X"
gateway-admin token issue --user alice --team team-x --ttl-days 30
gateway-admin token revoke --user alice
```

⚠️ RBAC-gated mutations need either `CORP_GATEWAY_ADMIN_TOKEN` (an operator JWT)
or `CORP_GATEWAY_RBAC=0` (a debugging bypass), and **neither is set in the
container's environment**. Set one on the machine you run the CLI from. Details:
`docs/ops/admin-cli.md`.

## Step 10. Connecting a developer

**Mode A:** two headers on every request —
`Authorization: Bearer <litellm virtual key>` (who may call this proxy at all)
and `X-Corp-Auth: <team token>` (whose rules and audit identity apply). The base
URL is the gateway's address.

**Mode B:**

```
export ANTHROPIC_AUTH_TOKEN='sk-ant-oat...'    # the developer's subscription token
export ANTHROPIC_BASE_URL='http://<gateway>:4000'
export ANTHROPIC_CUSTOM_HEADERS='X-Corp-Auth: <team token>'
unset ANTHROPIC_API_KEY                        # otherwise it shadows the subscription
claude
```

Only `sk-ant-oat…` tokens are accepted; anything else is `401 E_PROVIDER_AUTH`.

---

## What to know before going live

- **There is no TLS in front of the gateway yet.** The port listens on
  `127.0.0.1` without TLS; the nginx front door is a separate, unfinished task.
  Until it lands, developers connect through an SSH tunnel rather than over the
  network.
- **In Mode B litellm's management endpoints are unauthenticated.** Without a
  master key its proxy auth is skipped entirely. That covers `/key/*`,
  `/model/*`, `/user/*` and the UI — **not** the LLM routes, which the
  `X-Corp-Auth` check still gates. Today the surface is reachable only from the
  host itself (loopback), but it is reachable by anyone with a shell there. When
  nginx lands, that surface must be blocked there **before** the port is exposed
  beyond loopback. Mode A does not have this gap.
- **No untrusted `docker run` on this host.** The container label Vector selects
  audit records by is public: anyone who can start containers on this host can
  forge audit records. That is a hard requirement of the deployment model, not a
  recommendation.
- **Audit is buffered, but not fail-closed.** Vector's disk buffer (1 GiB by
  default) survives a Langfuse outage; once it fills, records wait in docker's
  log files, so the real durability boundary is log rotation (100 MB × 10 by
  default). If Langfuse can be down for longer, raise both values together.
- **Never run `FLUSHDB` against the gateway's redis.** It holds the mappings
  without which the response cannot be restored to its original form.
- **Virtual-key revocation exists only in Mode A.** Mode B has neither
  per-developer revocation nor per-person spend accounting.

## Where to look next

| Topic | File |
|---|---|
| Modes, every toggle, every failure mode | `docs/ops/deployment-modes.md` |
| On-call procedures and incident response | `docs/ops/runbook.md` |
| Reference for every variable | `docs/ops/configuration.md` |
| Operator CLI | `docs/ops/admin-cli.md` |
| How the compose stack is put together | `compose/README.md` |
| Version upgrades | `docs/ops/upgrade.md` |
| Capacity planning | `docs/ops/capacity.md` |
