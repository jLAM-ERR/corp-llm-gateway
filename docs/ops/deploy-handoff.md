# corp-llm-gateway — deployment handoff

A handoff document for whoever runs the deploy. This is the condensed sequence;
the providers and every failure mode live in `deployment-modes.md`, the
variable reference in `configuration.md`, and the on-call procedures in
`runbook.md`. Russian mirror: `deploy-handoff.ru.md`.

## What you are deploying

A gateway between the developers' Claude Code / Codex and the LLMs (Anthropic,
ChatGPT Codex, the corp vLLM). It strips personal data and corporate secrets out of the request
**before** it leaves the perimeter and restores them in the response. Everything
that goes upstream is written to the audit trail.

The stack (docker compose, one host): one gateway per provider —
`gateway-anthropic`, `gateway-codex`, `gateway-corp`, each switched on by its
compose profile — plus `profile-guard` (refuses a stack with no provider),
`postgres` (team tokens + team config, shared by every gateway), `redis` (the
mappings used to put the originals back), `langfuse` + `clickhouse` + `minio`
(traces and audit), `vector` (audit delivery). Upgrading a 1.0 host:
`upgrade.md`, "1.0 → 1.1.0".

---

## Step 0. Host requirements

- Debian/Ubuntu or the RHEL family. The bootstrap script refuses other
  distributions rather than guessing.
- Docker Engine + the compose v2 plugin. The script installs both from Docker's
  official repository if they are missing.
- **CPU-only.** No GPU is needed and none is supported.
- Disk: headroom for docker's logs and Vector's buffer — by default ~1 GiB of
  log rotation per gateway plus a 1 GiB Vector disk buffer.
- Egress, per provider: `api.anthropic.com` (`anthropic`), `chatgpt.com`
  (`codex`), the corp vLLM (`corp`, and the oracle if enabled); plus the corp
  NER service if you enable it.

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
| `COMPOSE_PROFILES` | the providers this host serves, see step 4 (refused by `profile-guard` without one) |
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

No `LITELLM_MASTER_KEY`, `UI_USERNAME`, `UI_PASSWORD`, `ANTHROPIC_API_KEY` or
`OPENAI_API_KEY`: 1.1.0 removed Mode A, and any `LITELLM_MASTER_KEY` line, blank
included, refuses to boot.

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

## Step 4. Choose the providers

Each provider is its own gateway container. Pick at least one, in `.env`:

```
COMPOSE_PROFILES=anthropic            # e.g. anthropic,codex or anthropic,codex,corp
```

| Provider | Serves | Upstream credential | Port on `127.0.0.1` |
|---|---|---|---|
| `anthropic` | Claude Code on the developer's Anthropic subscription (`claude-*`) | the developer's `sk-ant-oat…` token | 4000 (`GATEWAY_ANTHROPIC_PORT`) |
| `codex` | Codex on the developer's ChatGPT subscription | the developer's ChatGPT token | 4001 (`GATEWAY_CODEX_PORT`) |
| `corp` | the corp vLLM (`corp-*`); set `CORP_LLM_ENDPOINT` | `CORP_LLM_AUTH_TOKEN` (empty for an auth-less vLLM) | 4002 (`GATEWAY_CORP_PORT`) |

**There is no corporate provider API key anywhere.** The subscription providers
forward the developer's own token; the corp provider sends its configured one.

`COMPOSE_PROFILES` is the only switch. `deploy.sh` has no flag for it and never
sets it, and the autostart unit reads the same `.env`, so a reboot brings back
the same providers. Without a provider, `profile-guard` exits 64 naming the key
and `up` fails (`postgres` waits for it). Only exact names count (`corporate`
is not `corp`), and `--profile` on the command line is not supported.

**Each provider's litellm config has one route, and that is a load-bearing
security control.** The hook lifts the subscription token based on the
client-visible model alias, but litellm picks the actual upstream *after* the
hook runs, so the only guarantee that a token cannot reach another provider is
the routing table itself: `anthropic/` only on `gateway-anthropic`, the literal
ChatGPT Codex URL on `gateway-codex`. Do not add a route to either.

The corp vLLM oracle works on every provider: the gateway reaches it with its
own HTTP client, not through a litellm route.

## Step 4a. The HTTPS front door (optional)

Without it, each gateway listens on its `127.0.0.1` port only, in plain HTTP,
and developers need an SSH tunnel. With it, an nginx publishes one gateway and
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
   COMPOSE_PROFILES=anthropic,nginx   # the providers, plus nginx or nginx-ports; never both
   NGINX_GATEWAY_UPSTREAM=gateway-anthropic   # the default; or gateway-codex / gateway-corp
   NGINX_TLS_MODE=terminate        # or behind-proxy
   GATEWAY_DOMAIN=corp.example     # nginx only
   NGINX_BIND_ADDR=10.1.2.3        # the NIC clients (or the LB) reach; default 127.0.0.1
   NGINX_TLS_CERT=gateway.crt      # terminate only
   NGINX_TLS_KEY=gateway.key       # terminate only
   LANGFUSE_PUBLIC_URL=https://langfuse.corp.example
   ```

   `COMPOSE_PROFILES` is the only switch. `deploy.sh` has no flag for it, and
   the autostart unit reads the same `.env`, so a reboot brings back what a
   deploy started. nginx proxies to one gateway, `NGINX_GATEWAY_UPSTREAM`; its
   provider must be in `COMPOSE_PROFILES`, or nginx exits 64. The other
   providers stay on their loopback ports (SSH tunnel). `NGINX_BIND_ADDR` defaults to loopback: leave it and nothing
   outside the host reaches nginx. `LANGFUSE_PUBLIC_URL` must be the public
   `https://` origin (`https://<address>:8443` under `nginx-ports`), in
   `behind-proxy` too. To switch or drop the profile later, run `deploy.sh up`:
   it removes the container of the front door the `.env` no longer selects. A
   bare `docker compose up -d` does NOT, so the old one keeps the port; remove
   it by hand with `docker rm -f <container>`.
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
# the providers in the server's COMPOSE_PROFILES
scripts/deploy/deploy.sh --host user@server up

# plus developer token issuance (docker-compose.issuance.yml)
scripts/deploy/deploy.sh --host user@server --issuance up
```

There is no `--mode` (removed in 1.1.0; any value is refused). `up` refuses a
stack whose `COMPOSE_PROFILES` enables no provider, removes the container of a
provider or front door the `.env` no longer selects, and runs
`up -d --remove-orphans`. Those checks run after the sync: when one refuses,
`compose/` is already synced and nothing was pulled, started or removed. `up`
also checks what the autostart unit would start after a reboot: it refuses
when the bare `docker compose` cannot resolve the stack, and warns when a
reboot would start other gateways, or the same ones with another configuration
(usually a `COMPOSE_FILE` line that does not match `--issuance`).

**Pass `--issuance` to every later run against a host that issues tokens** —
`logs`, `status`, `down` and `restart` all resolve the stack through this file
list (`DEPLOY_ISSUANCE=1` works too). It needs `gateway/config.toml` in the
server's deploy directory (start from `compose/gateway/config.toml.example`);
`up` refuses before it syncs or starts anything if the file is missing. The
sync never uploads or overwrites that file. For the autostart unit, uncomment
`COMPOSE_FILE=docker-compose.yml:docker-compose.issuance.yml` in `.env`. Setup
and the manual two-file command: `compose/README.md`, "Developer token
issuance".

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
- The `corp` provider's `corp-*` route needs `CORP_LLM_ENDPOINT` **regardless**
  of this flag.
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
curl -fsS http://127.0.0.1:4000/healthz/live         # gateway-anthropic (4001 codex, 4002 corp)
docker compose logs --tail=100 gateway-anthropic     # or gateway-codex / gateway-corp
docker compose logs --tail=50 vector                 # audit is being delivered
```

The gateway ports (4000, 4001, 4002) are published **on loopback only**. Langfuse
publishes no host port at all — reach the UI through the front door
(`https://langfuse.<domain>`) or an SSH tunnel (forward a local `3000`).

With the front door on, check it from a developer's machine too:
`curl -fsS https://gateway.<domain>/healthz/live` (add
`--cacert <the CA>` for a self-signed certificate, never `-k`). Only
`/healthz/live` is reachable there; `/healthz/ready`, `/healthz/sanitization`
and `/metrics` answer on the loopback ports only.

## Step 9. Issuing team tokens (`X-Corp-Auth`)

Team identity is the `X-Corp-Auth` header. It decides which rules, profile and
audit identity apply, and it is required **on every provider**. A request
without a valid token is refused. A token works on every gateway of the host:
they share one database.

```
gateway-admin team create --team-id team-x --name "Team X"
gateway-admin token issue --user alice --team team-x --ttl-days 30
gateway-admin token revoke --user alice
```

⚠️ RBAC-gated mutations need either `CORP_GATEWAY_ADMIN_TOKEN` (an operator JWT)
or `CORP_GATEWAY_RBAC=0` (a debugging bypass), and **neither is set in any
gateway container's environment**. Set one on the machine you run the CLI from. Details:
`docs/ops/admin-cli.md`.

## Step 10. Connecting a developer

**Claude Code on the `anthropic` provider:**

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
`127.0.0.1:4000` and uses `http://localhost:4000`. The front door serves only
the `NGINX_GATEWAY_UPSTREAM` gateway; the others are reached through a tunnel. With issuance on,
`scripts/install.sh` gets the team token through the same origin
(`CORP_GATEWAY_URL`, `docs/ops/install.md`). Token counting
(`/v1/messages/count_tokens`) answers 404 at the front door (and 403 from the
gateway on the tunnel): it is advisory for Claude Code.

**Codex on the `codex` provider:** the profile in `docs/chatgpt-codex.md`, with
`base_url` on port 4001 (tunnel) or on the front door when
`NGINX_GATEWAY_UPSTREAM=gateway-codex`, and the developer's corp token in
`X-Corp-Auth`. Codex supplies the ChatGPT token itself.

**The `corp` provider:** point the client at port 4002 (tunnel), with a
`corp-…` model name and `X-Corp-Auth`. The client's `Authorization` is never
forwarded to the corp vLLM; do not send a subscription token there anyway.

---

## What to know before going live

- **HTTPS comes only from the front door.** The gateway ports (4000-4002)
  listen on `127.0.0.1` without TLS. Developers reach a gateway over the
  network only through the nginx front door (step 4a); without it they use an
  SSH tunnel. Never publish those ports beyond loopback instead.
- **litellm's management endpoints are refused, on every provider.** Without a
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
- **There are no litellm virtual keys** (Mode A was removed in 1.1.0), so no
  per-person spend accounting through litellm. Per-developer revocation is the corp token:
  `gateway-admin token revoke`.

## Where to look next

| Topic | File |
|---|---|
| Providers, every toggle, every failure mode | `docs/ops/deployment-modes.md` |
| On-call procedures and incident response | `docs/ops/runbook.md` |
| Reference for every variable | `docs/ops/configuration.md` |
| Operator CLI | `docs/ops/admin-cli.md` |
| How the compose stack is put together | `compose/README.md` |
| Version upgrades | `docs/ops/upgrade.md` |
| Capacity planning | `docs/ops/capacity.md` |
