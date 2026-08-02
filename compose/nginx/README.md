# nginx (optional entry point) — not yet built

Lands in `docs/plans/20260802-production-compose-corp-ner.md` Task 5 (C1) /
Task 6 (C2): `nginx.conf`, `conf.d/gateway.conf`, `certs/README.md`, plus a
`log_format` that omits `Authorization`/`X-Corp-Auth`.

Gated behind `docker compose --profile nginx up`: the service this directory
configures will carry `profiles: ["nginx"]` in `compose/docker-compose.yml`,
so it stays off by default until you opt in.
