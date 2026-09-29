# Upgrade notes

Read before upgrading an existing deployment. Items that need operator action:
the `team_config` schema change, the issuance columns on `corp_tokens` (before
enabling developer token issuance), the RS256 operator-token breaking change,
the new launch command (`python -m corp_llm_gateway.serve`) that the route gate
requires, the refused litellm management surface, the in-flight cap now on by
default, `deploy.sh` defaulting to subscription mode, and litellm DEBUG output
now refusing to boot.

## Database schema

Two SQL files define the Postgres schema. Both are idempotent
(`CREATE ... IF NOT EXISTS`, `CREATE OR REPLACE`).

- `src/corp_llm_gateway/tokens/schema.sql` — `corp_tokens` + `team_config`.
  It now creates `profile_ids` in the `CREATE TABLE` **and** carries its own
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS profile_ids`, so it converges an
  older table on its own.
- `src/corp_llm_gateway/team_config/schema.sql` (task B5) — `team_config` **with**
  a `profile_ids TEXT[]` column. Applied by
  `PostgresTeamConfigStore.init_schema()`.

### Fresh database

Apply both files (order does not matter — they are idempotent):

```
psql "$CORP_LLM_PG_DSN" -f src/corp_llm_gateway/tokens/schema.sql
psql "$CORP_LLM_PG_DSN" -f src/corp_llm_gateway/team_config/schema.sql
```

`corp_tokens` and a `team_config` with `profile_ids` are created. No further
action.

### Upgrading a database that already ran `tokens/schema.sql`

**Usually handled for you.** Both schema files now carry an idempotent
`ALTER TABLE ... ADD COLUMN IF NOT EXISTS profile_ids`, so re-running either one
converges a `team_config` created before the column existed.

The manual statement below is a **recovery command**, needed only if your
deployed schema files predate those ALTERs. The symptom is every `team get` /
`list` / `upsert` failing with `column "profile_ids" does not exist`, because
`PostgresTeamConfigStore` selects and upserts that column. Running it against an
already-converged database is harmless:

```
ALTER TABLE team_config
  ADD COLUMN IF NOT EXISTS profile_ids TEXT[] NOT NULL DEFAULT '{}'::text[];
```

The default `'{}'` means existing teams keep today's behavior (no profile
layers). `corp_tokens` is unchanged — no token migration needed.

Run this before rolling out the new image, or the team CLI and any team-config
read on the request path will error.

### Before enabling developer token issuance: re-run `tokens/schema.sql`

`POST /internal/issue-token` records the Keycloak identity each token was minted
for. `tokens/schema.sql` adds three nullable columns to `corp_tokens`
(`oidc_issuer`, `oidc_subject`, `oidc_jti`), a unique index
`corp_tokens_oidc_jti_key` on `oidc_jti` and a lookup index — all
`IF NOT EXISTS`. Re-run it **before** setting `CORP_GATEWAY_ISSUE_OIDC_ISSUER`:

```
psql "$CORP_LLM_PG_DSN" -f src/corp_llm_gateway/tokens/schema.sql
```

Existing rows and CLI-issued tokens keep NULLs; nothing else changes. The index
builds are plain `CREATE INDEX`, which blocks writes to `corp_tokens` while they
run — seconds on a token table, but run it outside peak. With issuance on, the
entrypoint checks this at boot and exits 78 naming the fix when a column or the
index is missing.

**An INVALID index.** If a `CREATE INDEX CONCURRENTLY` or a `REINDEX` of
`corp_tokens_oidc_jti_key` was interrupted, the index exists but is INVALID, and
re-running `schema.sql` skips it (`IF NOT EXISTS` sees the name). The boot
refuses with exit 78 and says so. Fix:

```
DROP INDEX corp_tokens_oidc_jti_key;          -- or: REINDEX INDEX corp_tokens_oidc_jti_key;
\i src/corp_llm_gateway/tokens/schema.sql
```

The same `DROP INDEX` then `schema.sql` applies when an index of that name exists
but is not a UNIQUE index on `oidc_jti` alone.

## RS256 operator-token breaking change (F11)

**Breaking.** `gateway-admin` operator-token verification (`auth/rbac.py`) is now
pinned to **RS256** and checks `aud` / `iss`. Previously it honored
`CORP_GATEWAY_OIDC_ALG` (which permitted HS256). The change closes a forgeable
path: HS256 with an empty or leaked symmetric key.

### Impact

Any deployment that issued operator tokens signed with **HS256** stops
validating on upgrade. Every RBAC-gated mutation (`team create` / `set-*`,
`token issue` / `revoke`, `extensions enable` / `disable`) is **denied** until
you migrate. Read verbs are unaffected (ungated). The gateway's request path is
unaffected — this is operator auth only.

### Migrate

1. Issue operator tokens as **RS256** (e.g. a Keycloak realm signing key), with
   `aud` and `iss` claims set.
2. Set `CORP_GATEWAY_OIDC_KEY` to the RS256 **public** key.
3. Set `CORP_GATEWAY_OIDC_AUDIENCE` and `CORP_GATEWAY_OIDC_ISSUER` to match the
   token's `aud` / `iss`. If either is unset, RBAC fails closed (denies).
4. Install the `oidc` extra (`pip install 'corp-llm-gateway[oidc]'`) — it pulls
   `pyjwt[crypto]` → `cryptography`. Without `cryptography`, RS256 verification
   raises `RuntimeError` and refuses rather than falling back to a weaker
   algorithm.
5. `CORP_GATEWAY_OIDC_ALG` is now **ignored** — you can leave or remove it.

### Dev bypass (unchanged)

`CORP_GATEWAY_RBAC=0` still bypasses RBAC entirely — **local dev only**. Do not
set it in staging or prod: it disables the operator claim check.

## litellm base image pin: v1.101.0

The litellm version is pinned in **seven** places, all now on **`v1.101.0`** (the
release GitHub marks Latest, 2026-09-15):

| Pin site | Previous | Now |
|---|---|---|
| `Dockerfile.gateway` (`ARG LITELLM_VERSION`) — the **published** image Helm and production compose run | `v1.95.0` | `v1.101.0` |
| `.github/workflows/build-image.yml` (`litellm_version` input default + the `LITELLM_VERSION` build-arg fallback) | `v1.95.0` | `v1.101.0` |
| `scripts/release/gates.sh` (`LITELLM_VERSION`) | `v1.95.0` | `v1.101.0` |
| `docker/demo-litellm/Dockerfile` | `v1.95.0` | `v1.101.0` |
| `docker/chatgpt-codex/Dockerfile` | `v1.95.0` | `v1.101.0` |
| `docker/anthropic-oauth/Dockerfile` | `v1.95.0` | `v1.101.0` |
| `pyproject.toml` (`litellm==1.101.0`) — **new seventh site** | `>=1.40,<2.0` | `==1.101.0` |

**The floating tags no longer match the pin.** At the v1.95.0 bump, `v1.95.0`,
`main-stable` and `latest` resolved to byte-identical manifests. Re-checked with
`docker manifest inspect` on 2026-09-22, that is no longer true: `main-stable`
and `latest` are identical to each other (amd64 `sha256:9d60771c…`) but resolve
to a **different** manifest than `v1.101.0` (amd64 `sha256:266180fb…`). Use the
explicit tag; do not assume the floating ones are equivalent.

**`pyproject.toml` is now an exact pin, not a floor** — the route gate's table is
generated from ONE litellm's route source and guarded against it
(`tests/route_gate/`). With the old `>=1.40,<2.0` range, CI's `pip install -e .`
resolved whatever PyPI called latest, so the guard read a different litellm than
the image ships and would have passed against routes the gateway never serves.
Move it with the other six; `tests/test_litellm_pin.py` fails if the sites
disagree. The one litellm symbol the request path imports,
`ANTHROPIC_OAUTH_TOKEN_PREFIX`, has an in-tree fallback (`litellm_hook.py`) and
is unchanged since v1.85.0.

Note that litellm 1.101.0 raises its own dependency floors (`openai>=2.20`,
`pydantic>=2.10`, `httpx>=0.28`, `pydantic-settings>=2.14.1`) and pulls in
`boto3`/`botocore` transitively.

`Dockerfile.gateway`'s default is now a fixed tag rather than `main-stable`, so
two builds of the same commit produce the same proxy. Override per build with
`--build-arg LITELLM_VERSION=<tag>`.

### Rollback

No data or schema migration is involved — the pin is a base-image tag, so
rollback is a redeploy of the previous image or a rebuild with the old tag:

```
# redeploy the previously published gateway image (fastest)
helm upgrade corp-llm-gateway helm/corp-llm-gateway --set image.tag=<previous-tag>

# or rebuild against the old base
docker build -f Dockerfile.gateway --build-arg LITELLM_VERSION=v1.95.0 .
```

Previous pins, for a per-site revert: all six sites were `v1.95.0`.

Earlier lineage, if you need to go back further than one step: before the
v1.95.0 bump the sites were `Dockerfile.gateway` `main-stable`, release-workflow
default `v1.85.0`, `scripts/release/gates.sh` `v1.85.0`, demo `v1.85.0`,
chatgpt-codex `v1.89.3`, anthropic-oauth `v1.89.3`.

Revert `build-image.yml` and `gates.sh` **together** — `gates.sh` names the
workflow as the source of truth for the pin, and `tests/test_litellm_pin.py`
fails if the sites disagree.

### What was checked before the bump

For **v1.101.0** the checks below were re-run against the installed 1.101.0 wheel
(`.venv-bench`, Python 3.12) via `tests/litellm_hook/test_litellm_route_assumptions.py`
and the rest of the suite — 2346 passed, 69 skipped, no failures. They were
**not** re-probed inside the built v1.101.0 image: the outbound-capture suite
(`tests/integration/test_anthropic_oauth_outbound.py`) needs a docker pull of the
pinned image and skipped locally. CI arms it with `CORP_REQUIRE_PROXY_CAPTURE=1`,
so that run is the one that proves the image itself — treat it as the gate that
is still outstanding, together with `scripts/release/gates.sh`.

Not a security fix in itself. Three routes bypass the guardrail hook in v1.101.0
— `/v1/messages/count_tokens`, the `/v1/responses` WebSocket, and the new
`/v1/responses/input_tokens` — and none of them references `pre_call_hook`.
**They are closed by the route gate that ships in the same release line** (see
"Route gate" below and `../security.md` §14), not by the bump.

The behavioural assertions themselves, originally probed in-image at v1.95.0:

- litellm's Anthropic OAuth branch is unchanged: an `sk-ant-oat…` `api_key`
  yields `authorization: Bearer …` + `anthropic-beta: oauth-2025-04-20` +
  `anthropic-dangerous-direct-browser-access`, and **no** `x-api-key`; a plain
  `sk-ant-api…` key still yields `x-api-key` (which is why the gateway's
  selector accepts OAuth tokens only).
- The `/v1/messages` pass-through transformer (the route Claude Code uses) still
  preserves `system`, and still lists `metadata` as unsupported. That list is
  declarative only — nothing filters the request against it, so a caller-supplied
  `metadata` still transforms through intact. The gateway's own scrub is what
  removes it.
- The chat-completions adapter still maps a top-level `user` to
  `metadata.user_id` and copies it into the outbound body — the reason the
  Anthropic bridge drops both fields.
- `CallTypes` still carries `aspeech` / `pass_through_endpoint` / `aresponses`
  (the hook's non-chat `input` denylist), and the router still treats `api_key`
  as a clientside credential (what the Codex and Anthropic bridges rely on).

## Route gate: the launch command changed (breaking for custom `command`)

**Breaking for any deployment that overrides the container command.** The image
`ENTRYPOINT` is now `python -m corp_llm_gateway.serve`; it was
`litellm --config /etc/litellm/config.yaml --port 4000`. compose runs the same
module, and the Helm chart sets no `command`, so it inherits the new ENTRYPOINT
with no values change. A stack that pins its own `command`/`entrypoint` to the
`litellm` CLI keeps running **without the route gate** — fix it before rolling
out, or the bypass routes stay open.

The entrypoint took over what litellm's CLI did and nothing more: it validates
the config file (litellm's own lifespan skips a missing one *in silence*, which
starts the proxy with no guardrail at all), runs the Prisma schema sequence with
the CLI's four guards, sets `WORKER_CONFIG`, and passes the same uvicorn
arguments. It then puts a default-deny route gate in front of litellm's router.
Rationale and the full refused set: [`../security.md`](../security.md) §14.

### What changes for operators

- **New exit codes at boot**, all fail-closed: **78** (litellm's config missing /
  not YAML / unreadable / configuring `pass_through_endpoints` or
  `general_settings.database_url`), **70** (litellm started but no
  `CorpLlmGuardrail` in `litellm.callbacks`), **2** / **1** (Prisma schema setup,
  same conditions as litellm's CLI). `runbook.md` has the triage table.
- **`general_settings.database_url` in litellm's YAML is now refused** (exit 78),
  and the `DATABASE_HOST`/`DATABASE_USERNAME`/… env composition is not read. The
  Prisma step reads `DATABASE_URL` / `DIRECT_URL` only — move the DSN there.
- **`general_settings.pass_through_endpoints` is refused** (exit 78): litellm
  registers those routes at runtime from config, the table cannot know them, and
  default-deny would 404 every one. Better a boot error than dead routes.
- **Pre-flight token counting is gone.** `POST /v1/messages/count_tokens`,
  `/v1/responses/input_tokens` and `/utils/token_counter` answer 403. Clients use
  `usage.input_tokens` from each real turn; Claude Code falls back to its own
  estimate for the context indicator. `../security.md` §11 (i) records the trade.
- **litellm's admin UI is no longer served.** `ast` cannot see inside a mounted
  ASGI app, so `/ui`, `/swagger`, `/docs` and `/openapi.json` get no table entry
  and are refused as unlisted. The JSON admin API (`/key/*`, `/team/*`, …) was
  pinned route by route and still answered in that release; it is refused now —
  see "litellm's management surface is refused" below. The escape hatch is
  `CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH` — but it
  admits one exact `(method, path)` per item and has no prefix form, so it fits a
  single operator route, not a mounted SPA.
- **`/healthz/*` and `/metrics` are now served by the gateway itself**, ahead of
  litellm's app. Helm's probes and the ServiceMonitor answer without a litellm
  credential (litellm's `PrometheusAuthMiddleware` 401s any path containing
  `/metrics` when a master key is set). compose's healthcheck moved to
  `/healthz/live`.
- **`HEAD` on a litellm route answers 405**, not a refusal: the gate admits it
  (HEAD inherits its path's GET verdict) but FastAPI's `APIRoute` does not
  register HEAD for a GET route. The gateway's own `HEAD /healthz/*` answers 200.
- **Scale with replicas, never uvicorn workers.** `serve.py` pins `workers=1`:
  each worker would re-run the Prisma sequence against one database and build its
  own Prometheus registry.

No data migration. Rollback is a redeploy of the previous image tag, which
carries the old ENTRYPOINT — and the open bypass routes with it.

## litellm's management surface is refused

**Breaking for anyone who used litellm's admin API or UI.** Every litellm route
that is not generation, model listing, a stored response by id or one of the
probes `/health/liveliness`, `/health/liveness`, `/health/readiness` now answers
`403 E_ROUTE_BLOCKED` — `/key/*`, `/team/*`, `/user/*`, `/model/*` writes,
`/policies*`, `/guardrails*`, spend, login/SSO, the public catalogue, `GET /`,
`GET /health` and the other `/health/*` routes. Rationale and the health-row
review: `../security.md` §14, "The management surface is refused".

- **API-key mode (virtual keys) is a test posture only.** Nothing can mint a
  virtual key any more. Move developers to subscription mode
  (`deployment-modes.md`); corp tokens come from `scripts/install.sh` and are
  revoked with `gateway-admin token revoke`.
- **A probe or dashboard on `GET /health` breaks.** Use `/healthz/live` and
  `/healthz/ready`, which the shipped Helm and compose probes already do.
- **`CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH` naming a refused route now exits 78
  at boot** (and `config check` reports it), as does a malformed item — it used
  to crash the import. Remove any such item before rolling out.

No data migration. Rollback is a redeploy of the previous image tag.

## The in-flight cap is on by default (64 per pod)

**Behaviour change.** Every pod now admits at most `CORP_LLM_MAX_INFLIGHT` (64)
concurrent LLM requests, a slot held for the whole stream; the next gets
`429 E_CAPACITY` with `Retry-After: 1`. Bodies must arrive within 30 s (408
`E_BODY_TIMEOUT`), and body memory is capped at 512 MiB per pod
(`CORP_LLM_MAX_DRAINING_BYTES`). Before rolling out, size replicas by concurrent
streams (`capacity.md`, "Sizing formula") and check that every capacity value you
set is in range: the entrypoint exits 78 on a bad one, and on
`CORP_LLM_MAX_INFLIGHT=0` under `CORP_ENV=prod`. Watch
`corp_llm_gateway_blocked_requests_total{block_reason="capacity"}` after the
rollout.

**Behind PgBouncer**, add the keepalive parameters to
`ignore_startup_parameters` before this rollout (`configuration.md`,
"Backends"): the gateway's Postgres pools now send them, and PgBouncer refuses
the connection otherwise — every LLM request would answer 503.

## `deploy.sh` defaults to subscription mode

`scripts/deploy/deploy.sh` without `--mode` now deploys `--mode oauth`
(`docker-compose.yml` + `docker-compose.oauth.yml`), the production mode. A host
deployed in API-key mode must now be driven with `--mode virtual-keys` on
**every** run — `up`, `logs`, `status`, `down`, `restart`. A bare run against
such a host would recreate the stack in subscription mode, which then refuses to
boot because its `.env` still has `LITELLM_MASTER_KEY`.

## `CORP_LLM_STRIP_INBOUND_HEADERS` now defaults to `1`

**Behaviour change, no action needed unless you set it explicitly.** The flag was
absent from the Helm chart and defaulted to `0`, so the guardrail carried the
client's inbound wire headers into litellm's upstream call — including the
client's `Content-Length` beside litellm's own, longer, **sanitized** body. The
provider then read the request truncated at the client's length. Wire-captured on
both `/v1/messages` and `/v1/chat/completions`: a `"stream": true` cut off that
way came back non-streamed, and any request whose sanitized body grew (a
placeholder longer than the original) was corrupted. Every Helm deploy was
affected.

It now defaults **on** (`bootstrap.build_guardrail`) and the chart sets it
explicitly. The dropped set (`_WIRE_HEADERS_TO_DROP`) is hop-by-hop / wire-level
only and never contains `authorization`, so BYOK passthrough is untouched, and
`X-Corp-Auth` was already stripped unconditionally one step earlier. Set it to
`0` only to reproduce the old behaviour. Recorded as `../security.md` §11 (h).

## The `ner` extra now carries pymorphy3: RU gazetteer terms are lemmatised

The `ner` extra now installs `pymorphy3` and `pymorphy3-dicts-ru`, and the
production image is built with it (`Dockerfile.gateway`). The gazetteer uses it
to lemmatise Russian terms and text; before, it matched Russian surface forms
only. No config change is needed.

- **Detection widens.** Inflected forms of a gazetteer term (other cases,
  numbers) now match, so more Russian text is redacted. With the oracle on and
  the default `gazetteer_hit` trigger, more hits also mean more oracle calls.
- **Cache A entries from the old build do not match.** The gazetteer's
  lemmatiser identity, pymorphy3 version included, is part of the policy
  fingerprint in the Cache-A key. Entries written without pymorphy3 (or with
  another version) are never served to the new pods; expect fewer cache hits
  until the new keys fill.
- An image built without the `ner` extra keeps surface matching.

## Cache A is invalidated by this release (no action required *for the cache*)

This release widens detector coverage: the Luhn-validated `BANK_CARD` label is
new, local NER's participation in CODE segments changed, and corp NER can add a
whole detector when enabled. A Cache-A hit applies the stored mapping **without
running detectors**, so an entry written by the previous build would replay as
unredacted for the rest of its ~10h TTL — a card number cached before the
upgrade would egress in the clear.

`_CACHE_A_ALGORITHM_VERSION` (`sanitizer/orchestrator.py`) is therefore bumped to
`coverage-v2`. It is mixed into the Cache-A key, so old pods write and read
`span-aware-v1` keys, new pods write and read `coverage-v2` keys, and the two key
sets are disjoint: neither version can serve the other's entries, in either
direction, even while both run against the same Redis. There is no cross-version
contamination, so **the cache needs no zero-overlap cutover, no egress block and
no operator action.** The same boundary covers the profile path: profile
sanitization delegates to `SanitizationOrchestrator.sanitize()`, which is the only
caller of `_content_hash`.

Alongside the constant, each orchestrator folds a fingerprint of its **effective
redaction policy** into the same key (`_POLICY_FINGERPRINT_VERSION`, currently
`policy-v3`). It covers: detector class identity **plus each detector's optional
`policy_signature()`**, the code-safe subset, gazetteer terms **and the
gazetteer's lemmatizer capability**, allowlist entries, and the oracle trigger.

The two capability inputs matter operationally: a pod **without** the `ner` extra
lemmatizes and NER-detects differently from one that has it, while configuring
identical terms and the same detector classes. Folding capability keeps those
pods on disjoint keys instead of letting a model-less pod's empty mapping be
served to a model-backed one. `dual_ner` reports its engines' real load state
this way, so a partially-installed pod cannot poison the shared cache.

**If the fingerprint cannot be computed, Cache A is switched off entirely** for
that orchestrator — both reads and writes — and the gateway logs
`cache_a_disabled reason=policy_fingerprint_failed` with the exception type only.
That is deliberate fail-closed behaviour: dedup is a latency optimisation, and
serving a key that ignores coverage is a leak. Operationally it looks like a
sudden loss of cache hits, not an outage.

The constant covers
build-time changes made *inside* a component — adding `BANK_CARD` changed a rule
table inside `RegexChecksumDetector` and nothing else. The fingerprint covers
config-time changes to the *set* of components, which the constant cannot see:
pods with different `CORP_NER_ENABLED` values run the same image and the same
algorithm version, and they now derive disjoint keys and cannot serve each
other's entries. A mid-rollout `CORP_NER_ENABLED` flip is therefore safe for the
cache in the same way a version bump is.

### What the cache guarantee does *not* cover

Scope the claim above to Cache A. During a rolling deploy, requests routed to
pods that have not been replaced yet are handled by the **old detectors**, so
they are not covered by the new rules (no `BANK_CARD`, for example). That is
inherent to rolling out any detection change and has nothing to do with Cache A —
the old pods are not replaying stale entries, they are correctly applying the
policy they were built with. If a specific detection rule must apply to 100% of
traffic from a known instant, drain or scale the old ReplicaSet to zero before
serving on the new one; otherwise accept the normal rollout window.

### Optional: reclaim the orphaned memory

The retired entries stay resident until their TTL expires (up to ~10h). Redis is
configured `maxmemory-policy noeviction` (`compose/redis/redis.conf`), so on a
tight `maxmemory` that dead set can push the instance to refuse writes. Deleting
it is housekeeping only.

Key prefixes, as defined in `src/corp_llm_gateway/storage/redis_store.py`:

| Prefix | Cache | Safe to delete? |
|---|---|---|
| `dedup:` | A — content-keyed dedup | **Yes** |
| `conv:o2p:` | B — original → placeholder | **No** |
| `conv:p2o:` | B — placeholder → original | **No** |
| `…:ttl` (suffix on Cache-B keys) | B — sliding-TTL bookkeeping | **No** |

The prefixes separate cleanly: `dedup:` matches Cache A and nothing else.

```
docker compose exec redis sh -lc \
  'redis-cli --scan --pattern "dedup:*" | xargs -r -n 500 redis-cli unlink'
```

**Never `FLUSHDB` / `FLUSHALL` here.** The gateway keeps both caches in the same
Redis (`REDIS_URL=redis://redis:6379/0`). Dropping the `conv:*` keys destroys the
per-conversation mappings that `post_call` desanitization requires, and every
in-flight conversation then returns `[LABEL_NNN]` placeholders to the developer
instead of the original text.

## Responses are restored outside litellm; litellm DEBUG refuses to boot

The response reversal moved from the guardrail callback into the gateway's ASGI
layer, so nothing inside litellm sees an original (`../security.md` §15).
What changes for operators:

- **litellm DEBUG output exits 70 at boot** (`arm refused (litellm_debug_logging)`
  or `(litellm_set_verbose)`): `LITELLM_LOG=DEBUG`, `DETAILED_DEBUG`,
  `--detailed_debug` or `litellm_settings.set_verbose`. litellm prints the
  original request, the corp token and the BYOK `Authorization` before any
  pre-call hook. Remove the setting before rolling out. For a test environment
  only, `CORP_LLM_ALLOW_LITELLM_DEBUG=1` lets it through; under
  `CORP_ENV=prod|production` that key itself is exit 78.
- **A rewritten route refuses a body that is not UTF-8 JSON** (415
  `E_ROUTE_BLOCKED`, `route_gate_body_not_json`: not `application/json`, a
  `charset` other than `utf-8`, a BOM, UTF-16/32, bytes that do not decode as
  UTF-8) or a JSON body with a top-level `policies` key (403,
  `route_gate_body_policies`). A custom client that posts a form body, or JSON
  in another encoding, breaks.
- **Keep `general_settings.supported_db_objects: ["models"]`** in any litellm
  config of your own (the shipped ones carry it; `configuration.md`).
- **Audit:** one terminal record per request, written when the response ends.
  A new `error_code`, `E_SERVER_SHUTDOWN` (`status` `cancelled`), marks requests
  the server cancelled at shutdown or pod drain; dashboards that count
  `cancelled` as client disconnects should split on `error_code`
  (`../audit-schema.md`). `gateway_failure{component="desanitize"}` is a new
  label value (`runbook.md`).
- **OpenAI chat-completions streaming now gets its originals back**; it used to
  get placeholders.

No data migration. Rollback is a redeploy of the previous image tag, which
brings back the chat-streaming placeholders and the logging-snapshot and corp
token exposures fixed here (`../../CHANGELOG.md`, Security).

## Rolling deploy

Follow `runbook.md`: tag → CI builds image + Helm artifacts → `helm upgrade` to
staging → `/healthz/ready` + `/healthz/sanitization` green → promote to prod.
Run `gateway-admin config check` against the target env first (see
`admin-cli.md`).
