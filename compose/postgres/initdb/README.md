# Postgres init scripts

`docker-entrypoint-initdb.d` scripts for the `postgres` service run once, in
filename order, the first time the `gateway-postgres-data` volume is empty.

- `01-schema.sql` — **staged at deploy time, never committed** (`*.sql` in
  this directory is git-ignored so it cannot drift from source).
  `scripts/deploy/deploy.sh` (Workstream D) copies
  `src/corp_llm_gateway/tokens/schema.sql` here before syncing `compose/` to
  the remote host. For a local run, do the same by hand:

```
# from compose/postgres/initdb (this directory) — three levels up to the repo root
cp ../../../src/corp_llm_gateway/tokens/schema.sql ./01-schema.sql
```

Or, equivalently, from `compose/` (the form used in `compose/README.md`):

```
cp ../src/corp_llm_gateway/tokens/schema.sql postgres/initdb/01-schema.sql
```

## litellm has no database (1.1.0)

Until 1.0.x, `00-create-litellm-db.sh` created a second database, `litellm`,
for litellm's virtual keys and admin UI (Mode A). 1.1.0 removed Mode A and
gives litellm no database at all, so the script is gone. On a volume created
before 1.1.0 the `litellm` database is still there but nothing connects to it.
It is inert; to drop it (the single quotes make the container's shell, not
yours, expand the two variables):

```
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DROP DATABASE IF EXISTS litellm"'
```

See `docs/ops/upgrade.md`, "1.0 → 1.1.0", for the other upgrade steps.
