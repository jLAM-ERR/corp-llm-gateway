# Self-hosted Langfuse v3 — not yet built

Lands in `docs/plans/20260802-production-compose-corp-ner.md` Task 2 (A2):
`langfuse-web`, `langfuse-worker`, `langfuse-postgres`, `clickhouse`, `minio`
+ `minio-init`, ported from `docker-compose.demo.yml:140-278` and hardened
(real secrets from `.env`, named volumes, no host port publishing —
reachable only via nginx or an SSH tunnel).

The admin-only LiteLLM instance this used to sit alongside is dropped — see
`compose/README.md` "Virtual keys" for why; `litellm` itself now owns
`LITELLM_MASTER_KEY`.
