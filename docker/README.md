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
3. Starts the corp-llm-mock (FastAPI) on port 8000 and the langfuse-mock on port 3000.
4. Builds the e2e container (Python 3.14), installs the gateway, and runs `pytest tests/e2e`.

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

The e2e tests skip cleanly when their env vars aren't set, so they're safe to
keep in the pytest run on a developer laptop. CI's `e2e` job
(`.github/workflows/ci.yml`) runs them the way below, on Python 3.14, with
`CORP_REQUIRE_E2E=1` so that a skip fails the job.

To run e2e locally without docker compose:

```
# Redis
docker run --rm -p 6379:6379 redis:7-alpine

# the two mocks, from a venv with fastapi + uvicorn (the [dev] extra)
python -m uvicorn --app-dir docker/corp-llm-mock app:app --port 8000
python -m uvicorn --app-dir docker/langfuse-mock app:app --port 3000

# the suite
REDIS_URL=redis://localhost:6379/0 CORP_LLM_ENDPOINT=http://localhost:8000 \
CORP_LLM_AUTH_PROVIDER=noop LANGFUSE_URL=http://localhost:3000 \
LANGFUSE_PUBLIC_KEY=pk-test-ci LANGFUSE_SECRET_KEY=sk-test-ci RUN_PROXY_E2E=1 \
CORP_REQUIRE_E2E=1 NO_PROXY=127.0.0.1,localhost \
PYTHONPATH=src .venv/bin/pytest tests/e2e -q -rs
```
