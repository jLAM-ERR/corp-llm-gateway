# Postgres init SQL (staged, not committed)

`docker-entrypoint-initdb.d` scripts for the `postgres` service are staged
here at deploy time, never committed — `*.sql` in this directory is
git-ignored so the schema cannot drift from source.

`scripts/deploy/deploy.sh` (Workstream D) copies
`src/corp_llm_gateway/tokens/schema.sql` into this directory before syncing
`compose/` to the remote host. For a local run, do the same by hand:

```
cp ../../src/corp_llm_gateway/tokens/schema.sql ./01-schema.sql
```
