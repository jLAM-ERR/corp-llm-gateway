# Postgres init scripts

`docker-entrypoint-initdb.d` scripts for the `postgres` service run once, in
filename order, the first time the `gateway-postgres-data` volume is empty.

- `00-create-litellm-db.sh` — **committed**. Creates the `litellm` database
  (litellm's own virtual-key store + UI accounts — see `compose/README.md`
  "Virtual keys"), on the same postgres instance as `$POSTGRES_DB`. It
  creates structure only, no data, so committing it carries no drift risk.
- `01-schema.sql` — **staged at deploy time, never committed** (`*.sql` in
  this directory is git-ignored so it cannot drift from source).
  `scripts/deploy/deploy.sh` (Workstream D) copies
  `src/corp_llm_gateway/tokens/schema.sql` here before syncing `compose/` to
  the remote host. For a local run, do the same by hand:

```
cp ../../src/corp_llm_gateway/tokens/schema.sql ./01-schema.sql
```
