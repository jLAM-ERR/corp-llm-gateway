# Docker e2e environment

Spin up the gateway dependencies + a mock corp-LLM and run the e2e test
suite against real Redis, real Postgres, and a real network.

## Run

```
docker compose up --build --abort-on-container-exit e2e
```

This:
1. Starts Redis 7 with `allkeys-lru`.
2. Starts Postgres 16 and applies `tokens/schema.sql` on init.
3. Starts the corp-llm-mock (FastAPI) on port 8000.
4. Builds the e2e container, installs the gateway, and runs `pytest tests/e2e`.

The e2e container exits with the pytest exit code; `--abort-on-container-exit`
tears down the rest.

## Customize the mock

Set `MOCK_PAIRS` on the `corp-llm-mock` service to a JSON array — e.g.

```yaml
environment:
  MOCK_PAIRS: '[{"original":"foo","replacement":"[BAR]"}]'
```

The mock will report those `(original, replacement)` pairs in every
chat-completion tool call.

## Layer / what's NOT covered

- LiteLLM proxy itself is NOT spun up — the e2e test exercises the
  `SanitizationOrchestrator` directly. A future addition can mount
  the gateway as a LiteLLM callback against a LiteLLM image.
- Vector / Langfuse / S3 / SIEM — out of scope; tested by stubs in unit
  tests + production-deploy verification per `docs/remaining-steps.md`
  Stage 3.
- Real corp LLM — replaced by `docker/corp-llm-mock`. Real endpoint is
  swapped in via `CORP_LLM_ENDPOINT` env at production deploy time.

## Outside docker-compose

The e2e tests skip cleanly when `REDIS_URL` / `CORP_LLM_ENDPOINT` aren't
set, so they're safe to keep in the pytest run on a developer laptop:

```
PYTHONPATH=src .venv/bin/pytest tests/ -q   # 265 unit, 0 e2e (skipped)
```

To run e2e locally without docker compose:

```
# in one shell
docker run --rm -p 6379:6379 redis:7-alpine

# in another
docker run --rm -p 8000:8000 -v $PWD/docker/corp-llm-mock:/app python:3.12-slim \
  bash -c "pip install fastapi uvicorn && uvicorn --app-dir /app app:app --host 0.0.0.0"

# in a third
REDIS_URL=redis://localhost:6379/0 \
CORP_LLM_ENDPOINT=http://localhost:8000 \
PYTHONPATH=src .venv/bin/pytest tests/e2e -q
```
