# Production compose stack

A production deploy target for non-k8s hosts, alongside `helm/corp-llm-gateway/`
(the k8s target). This directory currently ships the **data plane** only:
`litellm` (the guardrail-fronted proxy) + `redis` (Cache B, the per-conversation
mapping store) + `postgres` (the token + team-config store). The admin-only
LiteLLM UI, self-hosted Langfuse, production Vector config and the optional
nginx front door land in later revisions of this stack — see
`docs/plans/20260802-production-compose-corp-ner.md` for the full build order.

## Quickstart

```
cd compose
cp .env.example .env
chmod 0600 .env
# edit .env: GATEWAY_IMAGE_TAG, POSTGRES_PASSWORD, CORP_LLM_ENDPOINT at minimum
docker compose up -d
docker compose ps                              # all healthy
curl http://localhost:4000/health/liveliness
```

`GATEWAY_IMAGE_TAG`, `POSTGRES_PASSWORD` and `CORP_LLM_ENDPOINT` have no
default — `docker compose up` refuses to start with a clear "set X in .env"
error rather than silently booting half-configured.

## Why `hosted_vllm/`, not native `anthropic/`/`openai/` routing

`litellm/config.yaml` routes every inbound model name to the corp vLLM over
litellm's `hosted_vllm/` provider. That is the only litellm provider that does
a low-level passthrough of the inbound `Authorization` header — the developer's
own key rides untouched to the upstream (CLAUDE.md invariant #3). Native
`anthropic/`/`openai/` routing builds its own upstream credential header from a
gateway-configured `api_key` and never reads the client's `Authorization`
header at all (see `examples/compose/README.md` "BYOK in local mode (SPIKE
finding)" for the verified detail) — that tradeoff is fine for the solo/local
quickstart in `examples/compose/`, but not for this production target.

## No `LITELLM_MASTER_KEY` on this instance

Setting `LITELLM_MASTER_KEY` makes litellm intercept the `Authorization`
header on every API endpoint and reject anything that isn't one of its own
virtual keys — breaking BYOK passthrough. This data-plane `litellm` service
never sets it. A separate admin-only instance with its own UI and master key
is a later addition to this stack, not this one, and stays off the request
path.

## TLS to the corp vLLM

See `compose/certs/README.md`. Two different HTTP clients in the `litellm`
container each read their own trust-store env var: `CORP_LLM_CA_BUNDLE`
(our `CorpLlmClient`, httpx) and `SSL_CERT_FILE` (litellm's `hosted_vllm/`
upstream, aiohttp).

## Postgres schema

`compose/postgres/initdb/*.sql` is git-ignored on purpose — see
`compose/postgres/initdb/README.md`. The deploy script
(`scripts/deploy/deploy.sh`, a later addition to this stack) stages
`src/corp_llm_gateway/tokens/schema.sql` there before syncing to a remote
host; for a manual local run, copy it yourself before `docker compose up`.

## Durability

Both `redis` and `postgres` use named volumes, so `docker compose down`
(without `-v`) keeps the mapping store and token/team-config data across a
restart — unlike `docker-compose.demo.yml`'s laptop-walkthrough posture.
