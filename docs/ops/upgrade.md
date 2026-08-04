# Upgrade notes

Read before upgrading an existing deployment. Two items need operator action:
the `team_config` schema change, and the RS256 operator-token breaking change.

## Database schema

Two SQL files define the Postgres schema. Both are idempotent
(`CREATE ... IF NOT EXISTS`, `CREATE OR REPLACE`).

- `src/corp_llm_gateway/tokens/schema.sql` — `corp_tokens` + the original
  `team_config` (no `profile_ids` column).
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

**Action required.** The `team_config` table already exists (without
`profile_ids`), so `CREATE TABLE IF NOT EXISTS` in `team_config/schema.sql` is a
no-op and does **not** add the column. But `PostgresTeamConfigStore` now
`SELECT`s and upserts `profile_ids` — so every `team get` / `list` / `upsert`
fails with `column "profile_ids" does not exist` until you add it:

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

## Rolling deploy

Follow `runbook.md`: tag → CI builds image + Helm artifacts → `helm upgrade` to
staging → `/healthz/ready` + `/healthz/sanitization` green → promote to prod.
Run `gateway-admin config check` against the target env first (see
`admin-cli.md`).
