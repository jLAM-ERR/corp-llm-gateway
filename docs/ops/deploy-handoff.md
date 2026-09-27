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

There are two modes and they are **mutually exclusive**. **Mode B is the
production mode; Mode A is a test posture only** (DRI decision, 2026-09-27). The
decision is made before you fill in `.env`.

### Mode A — corporate API keys (test posture only)

The gateway holds the provider keys, and requests carry a **LiteLLM virtual
key**. There is no way to issue one: the route gate refuses `/key/*`, the admin
UI and the rest of litellm's management surface (`403 E_ROUTE_BLOCKED`,
`docs/security.md` §14). The container tests seed a key straight into litellm's
database; do not deploy this mode for developers. The rest of this subsection
describes what the stack still requires if you run it for testing.

Additionally in `.env`:

| Key | Value |
|---|---|
| `LITELLM_MASTER_KEY` | `sk-` + `openssl rand -hex 32` |
| `UI_USERNAME`, `UI_PASSWORD` | required by the entrypoint; the admin UI itself is refused at the gate |
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

## Step 4a. The HTTPS front door (optional)

Without it, the gateway listens on `127.0.0.1:4000` only, in plain HTTP, and
developers need an SSH tunnel. With it, an nginx publishes the gateway and
Langfuse over HTTPS and admits only the LLM routes, `/v1/models`,
`/healthz/live` and `POST /internal/issue-token` — everything else is 404. Full
reference: `compose/README.md`, "HTTPS front door (nginx)".

1. **Pick the routing.** `nginx` routes by name and needs two DNS records,
   `gateway.<domain>` and `langfuse.<domain>`, pointing at this host (or at the
   load balancer in front of it). `nginx-ports` needs no DNS: the gateway on
   `NGINX_PORT` (443), Langfuse on `NGINX_LANGFUSE_PORT` (8443).
2. **Pick the TLS mode — required, no default.** Your admins already publish
   HTTPS in front of this host and forward HTTP to it internally →
   `NGINX_TLS_MODE=behind-proxy`, plus `NGINX_TRUSTED_PROXIES` = the load
   balancer's address as nginx sees it. Every other peer gets no response at
   all, so a wrong value is an outage: the refused address is in
   `docker compose logs nginx` (or `nginx-ports`; status `444`). Otherwise →
   `NGINX_TLS_MODE=terminate`, and nginx needs a certificate (point 4).
3. **Fill in `.env` on the server** (the commented "nginx front door" block):

   ```
   COMPOSE_PROFILES=nginx          # or nginx-ports; never both
   NGINX_TLS_MODE=terminate        # or behind-proxy
   GATEWAY_DOMAIN=corp.example     # nginx only
   NGINX_BIND_ADDR=10.1.2.3        # the NIC clients (or the LB) reach; default 127.0.0.1
   NGINX_TLS_CERT=gateway.crt      # terminate only
   NGINX_TLS_KEY=gateway.key       # terminate only
   LANGFUSE_PUBLIC_URL=https://langfuse.corp.example
   ```

   `COMPOSE_PROFILES` is the only switch. `deploy.sh` has no flag for it, and
   the autostart unit reads the same `.env`, so a reboot brings back what a
   deploy started. `NGINX_BIND_ADDR` defaults to loopback: leave it and nothing
   outside the host reaches nginx. `LANGFUSE_PUBLIC_URL` must be the public
   `https://` origin (`https://<address>:8443` under `nginx-ports`), in
   `behind-proxy` too.
4. **Install the certificate on the server** (`terminate` only): directly in
   `/opt/corp-llm-gateway/nginx/certs/`, never through the sync (as
   `compose/certs/corp-ca-bundle.pem` in step 6, which the sync also
   excludes). One certificate with both names as SANs (or the IP clients dial,
   under `nginx-ports`), the full chain in PEM, the key unencrypted and `0600`.
   The deploy script never uploads anything there. SANs, a CSR example and a
   throwaway self-signed helper: `compose/nginx/certs/README.md`.
5. **Rotating it:** replace both files in the same directory, then
   `docker compose restart nginx` (or `nginx-ports`) on the server. nginx reads
   the certificate only at start, and does not warn before it expires.

A misconfigured front door does not start: its entrypoint names the bad key in
one log line and exits 64-69 (`compose/README.md`, "Troubleshooting"), and
`deploy.sh up` fails at once and names the service. It also refuses a `.env`
that enables both profiles, before it pulls anything.

## Step 5. Deploying from the operator's laptop (day N)

Rather than running `docker compose` by hand on the server, use the script — it
stages the token-store SQL schema, syncs `compose/`, runs `pull` + `up -d` and
waits for the healthchecks.

```
# mode B — subscription, the production mode (the default)
scripts/deploy/deploy.sh --host user@server up

# mode B + developer token issuance (docker-compose.issuance.yml)
scripts/deploy/deploy.sh --host user@server --issuance up

# mode A — API keys, a test posture only
scripts/deploy/deploy.sh --host user@server --mode virtual-keys up
```

**The same `--mode` must be passed to every later run against that host** —
`logs`, `status`, `down` and `restart` all resolve the stack through this file
list. On a mode A host, a run without `--mode virtual-keys` reports on (or
recreates) a different stack. The same holds for `--issuance`
(`DEPLOY_ISSUANCE=1`).

`--issuance` works in mode B only. It needs `gateway/config.toml` in the
server's deploy directory (start from `compose/gateway/config.toml.example`);
`up` refuses before it syncs or starts anything if the file is missing. The
sync never uploads or overwrites that file. Setup and the manual three-file
command: `compose/README.md`, "Developer token issuance".

Other subcommands: `down` (volumes survive, asks for confirmation), `restart`,
`logs`, `status`. Useful flags: `--dry-run`, `--yes`, `--dir PATH`,
`--force-unlock`.

The local `.env` is never uploaded; the server's `.env` is never touched. Keys,
certificates and `gateway/config.toml` are excluded from the sync.

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
curl -fsS http://127.0.0.1:4000/healthz/live         # the gateway answers
docker compose logs --tail=100 litellm
docker compose logs --tail=50 vector                 # audit is being delivered
```

Port `4000` is published **on loopback only** (`127.0.0.1:4000`). Langfuse
publishes no host port at all — reach the UI through the front door
(`https://langfuse.<domain>`) or an SSH tunnel (forward a local `3000`).

With the front door on, check it from a developer's machine too:
`curl -fsS https://gateway.<domain>/healthz/live` (add
`--cacert <the CA>` for a self-signed certificate, never `-k`). Only
`/healthz/live` is reachable there; `/healthz/ready`, `/healthz/sanitization`
and `/metrics` answer on `127.0.0.1:4000` only.

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

**Mode A:** not for developers — there is no way to issue the virtual key it
needs (test posture only, see Step 4).

**Mode B:**

```
export ANTHROPIC_AUTH_TOKEN='sk-ant-oat...'    # the developer's subscription token
export ANTHROPIC_BASE_URL='https://gateway.<domain>'
export ANTHROPIC_CUSTOM_HEADERS='X-Corp-Auth: <team token>'
unset ANTHROPIC_API_KEY                        # otherwise it shadows the subscription
claude
```

Only `sk-ant-oat…` tokens are accepted; anything else is `401 E_PROVIDER_AUTH`.

`ANTHROPIC_BASE_URL` is the front door's HTTPS origin (step 4a):
`https://gateway.<domain>` under `nginx`, `https://<address>:<NGINX_PORT>`
under `nginx-ports` (the port may be left out when it is 443). A certificate
from a CA the laptop does not trust needs `NODE_EXTRA_CA_CERTS=<the CA file>`.
Without the front door, the developer opens an SSH tunnel to the server's
`127.0.0.1:4000` and uses `http://localhost:4000`. With issuance on,
`scripts/install.sh` gets the team token through the same origin
(`CORP_GATEWAY_URL`, `docs/ops/install.md`). Token counting
(`/v1/messages/count_tokens`) answers 404 at the front door (and 403 from the
gateway on the tunnel): it is advisory for Claude Code.

---

## What to know before going live

- **HTTPS comes only from the front door.** Port `4000` listens on
  `127.0.0.1` without TLS. Developers reach the gateway over the network only
  through the nginx front door (step 4a); without it they use an SSH tunnel.
  Never publish `4000` beyond loopback instead.
- **litellm's management endpoints are refused, in both modes.** Without a
  master key litellm accepts any caller as an internal user, so much of
  `/key/*`, `/model/*`, `/user/*`, `/policies*`, `/guardrails*` and the UI would
  otherwise answer anyone who reaches the port. The route gate answers all of them with `403
  E_ROUTE_BLOCKED` before litellm sees the request, on every path in
  (`docs/security.md` §14). Operators use `gateway-admin`, not litellm's UI.
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
- **There are no litellm virtual keys.** Neither mode has per-person spend
  accounting through litellm. Per-developer revocation is the corp token:
  `gateway-admin token revoke`.

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
