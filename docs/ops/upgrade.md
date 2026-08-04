# Upgrade notes

Read before upgrading an existing deployment. Two items need operator action:
the `team_config` schema change, and the RS256 operator-token breaking change.

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

## litellm base image pin: v1.95.0

The litellm base image is pinned in six places, all now on **`v1.95.0`** (the
latest stable — `v1.95.0`, `main-stable` and `latest` resolve to byte-identical
manifests, verified 2026-08-04):

| Pin site | Previous | Now |
|---|---|---|
| `Dockerfile.gateway` (`ARG LITELLM_VERSION`) — the **published** image Helm and production compose run | `main-stable` | `v1.95.0` |
| `.github/workflows/build-image.yml` (`litellm_version` input default + the `LITELLM_VERSION` build-arg fallback) | `v1.85.0` | `v1.95.0` |
| `scripts/release/gates.sh` (`LITELLM_VERSION`) | `v1.85.0` | `v1.95.0` |
| `docker/demo-litellm/Dockerfile` | `v1.85.0` | `v1.95.0` |
| `docker/chatgpt-codex/Dockerfile` | `v1.89.3` | `v1.95.0` |
| `docker/anthropic-oauth/Dockerfile` | `v1.89.3` | `v1.95.0` |

`pyproject.toml` still declares `litellm>=1.40,<2.0` — a floor, deliberately not
raised: nothing in `src/` needs a v1.95 API. The one litellm symbol the request
path imports, `ANTHROPIC_OAUTH_TOKEN_PREFIX`, has an in-tree fallback
(`litellm_hook.py`) and is unchanged since v1.85.0.

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
docker build -f Dockerfile.gateway --build-arg LITELLM_VERSION=v1.85.0 .
```

Previous pins, for a per-site revert: `Dockerfile.gateway` `main-stable`,
release-workflow default `v1.85.0`, `scripts/release/gates.sh` `v1.85.0`,
demo `v1.85.0`, chatgpt-codex `v1.89.3`, anthropic-oauth `v1.89.3`.

Revert `build-image.yml` and `gates.sh` **together** — `gates.sh` names the
workflow as the source of truth for the pin, and `tests/test_litellm_pin.py`
fails if the sites disagree.

### What was checked before the bump

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

## Rolling deploy

Follow `runbook.md`: tag → CI builds image + Helm artifacts → `helm upgrade` to
staging → `/healthz/ready` + `/healthz/sanitization` green → promote to prod.
Run `gateway-admin config check` against the target env first (see
`admin-cli.md`).
