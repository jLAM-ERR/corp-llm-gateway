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

## Upgrading a pre-existing deployment

`docker-entrypoint-initdb.d` scripts run only the first time
`gateway-postgres-data` is empty. If you already started this stack before
`00-create-litellm-db.sh` existed (i.e. before the litellm virtual-key
rework), your volume has no `litellm` database and the `litellm` container
will fail at boot. Two ways to fix it:

- **Recreate** (loses `$POSTGRES_DB` data too — fine for a throwaway/test
  deploy): `docker compose down -v && docker compose up -d`, so initdb runs
  again on an empty volume.
- **Migrate in place** (keeps existing tokens/team-config): run
  `00-create-litellm-db.sh`'s `CREATE DATABASE litellm;` by hand against the
  running `postgres` service, then `docker compose up -d litellm`:

```
docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE litellm;"
```
