# compose/nginx — the HTTPS front door

Configuration for the opt-in `nginx` / `nginx-ports` services in
`../docker-compose.yml`, switched on by `COMPOSE_PROFILES` in the server's
`.env`. Operator reference — the two TLS modes, the env keys, what the front
door admits, logs and the entrypoint's exit codes: `../README.md`, "HTTPS front
door (nginx)" (RU: `../README.ru.md`, «HTTPS-фронт (nginx)»).

| Path | What |
|---|---|
| `nginx.conf` | static; its `http {}` includes only `/etc/nginx/rendered/*.conf` |
| `entrypoint.sh` | validates the `NGINX_*` keys (exit 64-69), renders the templates, runs `nginx -t`, then serves |
| `templates/00-http.conf.template` | http context: access-log format, trusted-peer `geo`, `real_ip`, edge-limit zones, the loopback health listener |
| `templates/listeners/<mode>.<routing>.conf.template` | one per `NGINX_TLS_MODE` × routing; the entrypoint renders exactly one |
| `templates/snippets/gateway-locations.inc.template` | the gateway allow-list — the only copy |
| `templates/snippets/langfuse-locations.inc.template` | the Langfuse origin |
| `certs/` | the certificate and key for `terminate`, installed on the server by hand: `certs/README.md` |

Tests: `tests/compose/test_nginx_profile.py` (static), `test_nginx_runtime.py`
(a real nginx container), `test_nginx_allowlist_routes.py` (the allow-list
against litellm's route table and the gateway's route gate).
