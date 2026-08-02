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

litellm forwards an inbound request header to ANY upstream provider only
through `data["headers"]`, which is populated only when
`forward_client_headers_to_llm_api` is set — either on `general_settings` or
per model group (`litellm/proxy/litellm_pre_call_utils.py`
`add_litellm_data_for_backend_llm_call` / `add_headers_to_llm_call_by_model_group`)
— and even then it forwards only `x-*` headers plus `anthropic-beta`, never
`Authorization`. Neither setting appears anywhere in `litellm/config.yaml`.
Verified twice: against litellm 1.85.0 (the pinned image) and independently
against 1.90.1; see `examples/compose/README.md` "BYOK in local mode (SPIKE
finding)" for the native-provider half of the writeup.

**No inbound header — `Authorization` or `Host` — reaches any upstream on
this stack, on any route, including `corp-*`.** The `corp-*` /
`hosted_vllm/` route builds its upstream request the same way `anthropic/`
and `openai/` do: from the configured `api_key` and model params, never from
wire headers. An earlier revision of this README claimed `hosted_vllm/`
passed inbound headers through and that `CORP_LLM_STRIP_INBOUND_HEADERS=1`
was needed to stop a `Host` header 503ing the corp ingress — that was wrong;
without `forward_client_headers_to_llm_api`, the header the flag strips
never reaches litellm's provider layer in the first place, so the flag is
currently inert on this stack. It stays set (see `.env.example`) as a
reserved seam for a future deployment that does enable header forwarding.

BYOK (the developer's own key forwarded untouched — CLAUDE.md invariant #3)
is **not available on this compose stack at all**, on any route. Developers
authenticate with the litellm virtual key described above, full stop.

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
operator's `SSL_VERIFY=false` on the corp-LLM call is refused rather than
silently disabling TLS verification on the path carrying raw user content.
`CORP_LLM_REQUIRE_NER=1` makes a self-disabled NER engine fail closed
(`NerUnavailableError`) instead of silently returning no findings and
letting a PERSON/ORG egress (F2). Both are set by default in `.env.example`;
do not clear them for a real deploy.
