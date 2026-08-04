# Self-hosted Langfuse v3

Config for the Langfuse services declared in `compose/docker-compose.yml`:
`langfuse-web`, `langfuse-worker`, `langfuse-postgres`, `clickhouse`,
`minio`, `minio-init` (the bucket job — v3 does not auto-create it) and
`langfuse-redis`. Operator-facing docs live in `compose/README.md`
("Langfuse", "Two UIs — which one answers which question"); this file only
covers what is in this directory.

## `clickhouse-config.xml`

Mounted read-only at
`/etc/clickhouse-server/config.d/corp-hardening.xml`. It deletes the system
tables that store raw query text (`query_log`, `query_thread_log`,
`query_views_log`, `text_log`) and pins the server log level.

Langfuse writes trace bodies to ClickHouse as INSERT statements, so those
tables would become a second copy of trace content — on ClickHouse's own
retention, in a store neither the NEVER-fields gate (`audit/invariants.py`)
nor Vector's VRL gate ever sees. Invariant #1. `metric_log`,
`asynchronous_metric_log` and `part_log` are kept: no query text, and they
are what sizing the deployment needs.

`remove="1"` deletes the section inherited from the image's `config.xml`
rather than merging into it — this is a `config.d` overlay, not a
replacement config.

## What is NOT here

No secret, and no `.env`. Every credential (`LANGFUSE_POSTGRES_PASSWORD`,
`LANGFUSE_CLICKHOUSE_PASSWORD`, `MINIO_ROOT_PASSWORD`,
`LANGFUSE_NEXTAUTH_SECRET`, `LANGFUSE_SALT`, `LANGFUSE_ENCRYPTION_KEY`)
comes from `compose/.env`, which is git-ignored. The demo stack's
`change-in-production` placeholders are deliberately absent.

The admin-only LiteLLM instance this used to sit alongside is dropped — see
`compose/README.md` "Virtual keys"; `litellm` itself now owns
`LITELLM_MASTER_KEY`.
