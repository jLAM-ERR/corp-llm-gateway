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

## Rolling deploy

Follow `runbook.md`: tag → CI builds image + Helm artifacts → `helm upgrade` to
staging → `/healthz/ready` + `/healthz/sanitization` green → promote to prod.
Run `gateway-admin config check` against the target env first (see
`admin-cli.md`).
