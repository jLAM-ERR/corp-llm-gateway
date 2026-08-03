# Production compose stack

A production deploy target for non-k8s hosts, alongside `helm/corp-llm-gateway/`
(the k8s target). This directory currently ships the **data plane** only:
`litellm` (the guardrail-fronted proxy) + `redis` (Cache B, the per-conversation
mapping store) + `postgres` (the token/team-config store, plus litellm's own
virtual-key/UI database). Self-hosted Langfuse, production Vector config and
the optional nginx front door land in later revisions of this stack — see
`docs/plans/20260802-production-compose-corp-ner.md` for the full build order.

## Quickstart

```
cd compose
cp .env.example .env
chmod 0600 .env
# edit .env: GATEWAY_IMAGE_TAG, POSTGRES_PASSWORD, LITELLM_MASTER_KEY,
# UI_USERNAME, UI_PASSWORD, and at least one of ANTHROPIC_API_KEY/OPENAI_API_KEY
cp ../src/corp_llm_gateway/tokens/schema.sql postgres/initdb/01-schema.sql
docker compose up -d
docker compose ps                              # wait ~30-60s (start_period) for "healthy"
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
cleartext.

## Durability

`redis` and `postgres` use named volumes, so `docker compose down` (without
`-v`) keeps mappings, tokens/team-config and virtual keys across a restart —
unlike `docker-compose.demo.yml`'s laptop-walkthrough posture. `litellm`
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
