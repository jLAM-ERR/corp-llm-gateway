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
docker compose up -d
docker compose ps                              # all healthy
curl http://localhost:4000/health/liveliness
```

`GATEWAY_IMAGE_TAG`, `POSTGRES_PASSWORD`, `LITELLM_MASTER_KEY`, `UI_USERNAME`
and `UI_PASSWORD` have no default — `docker compose up` refuses to start with
a clear "set X in .env" error rather than silently booting half-configured.

## Upstream routing

`litellm/config.yaml` routes by model-name prefix: `claude-*` → native
`anthropic/`, `gpt-*` → native `openai/`, `corp-*` → `hosted_vllm/` at
`CORP_LLM_ENDPOINT` (optional — see below). Provider credentials for the
first two come from `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` in `.env`, held by
the gateway, not the developer — see "Virtual keys" for why.

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

## Why not BYOK against Anthropic/OpenAI

Native `anthropic/` and `openai/` litellm providers build their own upstream
credential header purely from the `api_key` configured in
`litellm/config.yaml` and never read the inbound client's `Authorization`
header — verified twice (litellm source, and a live capture server standing
in for `api.anthropic.com`; see `examples/compose/README.md` "BYOK in local
mode (SPIKE finding)" for the full writeup). `forward_client_headers_to_llm_api`
only forwards `x-*` headers and explicitly excludes `Authorization`. There is
no config flag that changes this for these two providers.

BYOK (the developer's own key forwarded untouched — CLAUDE.md invariant #3)
is retained only on the `corp-*` (`hosted_vllm/`) route, which does a
low-level passthrough of inbound request headers. That's also why
`CORP_LLM_STRIP_INBOUND_HEADERS=1` is the default here: `hosted_vllm/`
forwards `proxy_server_request.headers` (incl. `Host: 127.0.0.1:4000`)
upstream, and the corp ingress 503s on the unknown vhost unless those wire
headers are stripped first (`settings.py` `CORP_LLM_STRIP_INBOUND_HEADERS`,
plumbed through `bootstrap.build_guardrail()`).

## TLS to the corp vLLM

See `compose/certs/README.md`. Two different HTTP clients in the `litellm`
container each read their own trust-store env var: `CORP_LLM_CA_BUNDLE`
(our `CorpLlmClient`, httpx) and `SSL_CERT_FILE` (litellm's `hosted_vllm/`
upstream, aiohttp). Both use the compose bare-name environment form, so an
unset value is absent from the container entirely, not set to an empty
string.

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

`redis`, `postgres` and `litellm` all use named volumes, so `docker compose
down` (without `-v`) keeps mappings, tokens/team-config and virtual keys
across a restart — unlike `docker-compose.demo.yml`'s laptop-walkthrough
posture. Redis is configured `noeviction` (`compose/redis/redis.conf`): Cache
B is CLAUDE.md's *required* per-conversation mapping store for `post_call`
desanitization, so an under-memory Redis must fail loudly (`OOM` errors)
rather than silently evict live mappings and return placeholder text to a
developer.

## Environment posture

`CORP_ENV=production` arms the F9 guard (`config.py` `is_prod()`): an
operator's `SSL_VERIFY=false` on the corp-LLM call is refused rather than
silently disabling TLS verification on the path carrying raw user content.
`CORP_LLM_REQUIRE_NER=1` makes a self-disabled NER engine fail closed
(`NerUnavailableError`) instead of silently returning no findings and
letting a PERSON/ORG egress (F2). Both are set by default in `.env.example`;
do not clear them for a real deploy.
