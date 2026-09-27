# CLAUDE.md — corp-llm-gateway

Project conventions for future Claude Code sessions on this repo.

## What this repo is

A corporate LLM gateway plugged into LiteLLM that sanitizes traffic between
developer Claude Code instances and Anthropic / OpenAI before it leaves the
corp boundary. Replaces the per-laptop `data-sanitizer` plugin hook with a
centrally-enforced, auditable, multi-provider gateway. v1 plan is in
`docs/plans/20260507-external-sanitizer-gateway-v1.md` (currently rev 5).

The non-negotiable success criterion is **zero confirmed leak incidents**
in the 90 days post-GA.

## Repo layout (don't rearrange without a reason)

```
src/corp_llm_gateway/
  asgi.py       THE serve target: checks litellm's config (exit 78), runs its Prisma sequence, sets WORKER_CONFIG,
                imports litellm's app, wraps its lifespan (arm the gate or exit 70), wraps it in RouteGateMiddleware
  serve.py      `python -m corp_llm_gateway.serve` — uvicorn.run(asgi:app), workers=1; the image/compose/Helm ENTRYPOINT
  auth/         CorpLlmAuthProvider (Noop default; Bearer/mTLS/OIDC) + get_auth_provider factory
  audit/        AuditEvent + Logger + Sinks + get_sink factory + retention generator + NEVER-fields gate
  bootstrap.py  production composition root: build_guardrail() from config; lazy PEP-562 `guardrail` singleton
                (the LiteLLM callback target — importing the module is side-effect-free)
  cli/          gateway-admin (operators: team/token/extensions/config check) + corp-llm-gateway status (devs) + proxy
  config.py     env → $CORP_LLM_GATEWAY_CONFIG_FILE → ~/.config → /etc → default loader; get/get_required/get_table/validate
  corp_llm/     httpx client speaking vLLM /v1/chat/completions
  detectors/    PIIDetector + regex_checksum + dual_ner (RU Natasha + EN spaCy); NerUnavailableError (fail-closed)
  extensions/   ExtensionRegistry + ExtensionSpec (kind/api_version/fail_policy); fail-closed register; api-version gate
  healthz/      live / ready / sanitization / extensions checks + ASGI server (build_health_router, serves /healthz/*)
                + POST /internal/issue-token: gateway-owned, terminates locally (never reaches litellm), bounded
                (own in-flight cap + rate, header-only bearer, no body, one store bound); off unless
                CORP_GATEWAY_ISSUE_OIDC_ISSUER is set. `gateway-admin token issue` is break-glass only
  metrics/      MetricsExporter (noop default / prometheus); emits blocked_requests_total{block_reason} + gateway_failure{component}
  payload/      size threshold + gzip + per-team quota + oversize policy (fail-closed default)
  profiles/     plugin bundles — ProfileBundle/PolicyKnobs(merge) + loaders/resolver + DETECTOR_REGISTRY + manifest (hash-integrity) + defaults/
  providers/    ProviderRegistry + executable v1-guard (anthropic/openai/corp-vllm; v2 behind CORP_ALLOW_V2_PROVIDERS)
  route_gate/   default-deny gate: table.py (hand-classified against litellm's source, guarded by the
                collector test) + classify.py + middleware.py; litellm's admin/auth/spend/public/UI/non-probe
                health surfaces are REFUSE (litellm rows 26 PASSTHROUGH / 879 REFUSE / 8 REWRITTEN + 6 gateway rows)
                + inflight.py: the gateway-owned in-flight cap (CORP_LLM_MAX_INFLIGHT), single owner of `receive`,
                disconnect-aware cancellation, spawn_shared for tasks several requests await
  rules/        replace.md parser + gazetteer + cached file loader
  sanitizer/    local-first engine + segmenter + StreamingDesanitizer + DLP guard + orchestrator + ProfileAwareOrchestrator (live profiles)
                + identity_preamble (rewrite-vs-scan carve-out, see below)
  settings.py   single source of truth (typed KEYS registry + validate()); config.py delegates; backs `config check`;
                capacity() / serving_issuance() are the resolvers the boot and `config check` share
  pg_session.py bounded asyncpg use (cancel within 0.5 s, then terminate) + store_unavailable() + the boot-probe
                outcome table (BOOT_PROBE_OUTCOMES: refuse vs warn-and-boot)
  storage/      MappingStore (in-memory + Redis)
  team_config/  TeamConfig (+ profile_ids) + store (in-memory + Postgres) + schema.sql
  tokens/       schema.sql (+ oidc columns) + AuthMiddleware (single-flight lookup) + TokenIssuer + stores
                + oidc_verifier (Keycloak RS256) + per-(iss, sub) issuance policy
  litellm_hook.py  CorpLlmGuardrail — LiteLLM callback adapter (sanitize/desanitize incl. OpenAI tool_calls + streaming)
helm/corp-llm-gateway/   Helm chart (gateway image + guardrail callback + Secret + HPA/PDB/SA + ServiceMonitor + config-check
                          initContainer + env passthrough + NetworkPolicy + CoreDNS sinkhole)
docs/                    plans/ + audit-schema + security + ops/* (install/configuration/admin-cli/upgrade/profiles/runbook/capacity/release) + rbac-matrix + adr/*
scripts/install.sh       laptop installer (bash/zsh/fish, macOS/Linux)
tests/                   pytest, pytest-asyncio mode=auto (~3530 passed / ~360 skipped on .venv; ~4200 / 16 on .venv-bench with
                         Postgres, where NER, RS256 crypto, the Postgres contracts and the entrypoint/route-gate suites run)
```

The GA-readiness / security / extensibility build is `docs/plans/20260708-ga-readiness-security-extensibility.md`
(profiles/extensions/metrics/settings/bootstrap all landed there). Key new seams (see the `safe-extension-registry`
+ `lazy-entrypoint-singleton` skills): `extensions/` and `providers/` are keyed registries; `profiles/` is the
data-bundle plugin layer; `bootstrap.build_guardrail()` wires everything from config. Contributor how-to for
all of these seams (detector / sink / metrics / provider / profile, with the three extension styles):
`docs/extending.md`.

## Request lifecycle (read once, then you understand the engine)

The cascade was **inverted to local-first** (plan `docs/plans/20260630-bilingual-local-first-detection.md`,
decision `docs/adr/ADR-003-ner-orchestration.md`). Old order was LLM-oracle-first; now:

```
route gate (OUTERMOST, before litellm's router — `route_gate/middleware.py`):
           classify (METHOD, path) against the hand-classified table → PASSTHROUGH / REWRITTEN / REFUSE.
           Unlisted ⇒ 404, listed-REFUSE / websocket / malformed ⇒ 403, both E_ROUTE_BLOCKED;
           REWRITTEN while unarmed ⇒ 503. Only then does litellm's router run pre_call_hook.
           ↓
pre_call:  Stage 0 — payload classifier: config/log shape → refuse before egress (422 + block_reason)
           ↓
           local-first cascade (per text leaf, ~6ms p50 on CPU):
             replace.md → regex+checksum → dual-NER (Natasha RU + spaCy EN, run-both-union)
             → lemma-gazetteer (products/ПОД-ФТ/markings) → code-identifier splitter
           ↓
           LLM oracle — CONDITIONAL fallback: called ONLY on a deterministic gazetteer hit
             (no hit ⇒ oracle NOT called — the latency win; no confidence thresholds)
           ↓
           merge local+oracle pairs (M1-9 bijection preserved) → request allocator canonicalizes
           ↓
           Stage 5 — DLP egress guard: re-scan the SANITIZED request → block on canary/raw secret
           ↓
           upstream (api.anthropic.com / api.openai.com) with BYOK Authorization
           ↓
post_call: StreamingDesanitizer rebuilds originals using the per-conversation mapping
           ↓
           audit: Vector → Langfuse + S3 + SIEM (NEVER-fields gate; + block_reason)
```

The process that serves this is **ours**: `python -m corp_llm_gateway.serve` → `asgi.py`, which
checks litellm's config, then imports litellm at step 3 (`save_worker_config`) and its app + our
`bootstrap` at step 4. Neither the `litellm` CLI nor litellm's own `proxy_server` app may ever be a
serve target again — both run litellm's routers with no gate in front (`tests/test_launch_command.py`
greps the tree for either). Details: `docs/security.md` §14.

The old three tiers (FunctionCall → JSON → Regex, `sanitizer/engine.py`) still parse the oracle's
response when it IS called; they are no longer the primary detection path. Local detectors live in
`detectors/` (`regex_checksum`, `ner_ru`/`ner_en`/`dual_ner`) + `rules/gazetteer.py` +
`sanitizer/segmenter/`. NER runs on 3.12 and 3.14 (the `ner` extra has wheels for both, and brings
`pymorphy3` for the gazetteer's RU lemmas); lazy imports keep the package importable without the
extra, with graceful degradation.

Two caches:

- **Cache A** — content-keyed dedup, shared across conversations, TTL ~10h.
- **Cache B** — per-conversation mapping store (Redis or in-memory),
  sliding TTL ~1h, **required** for `post_call` to undo redactions.
  Today `conversation_id == request_id`, so Cache B doesn't reuse across
  sibling requests; see `docs/conversation-id.md`.

## Running tests

```
# Full unit suite. Local .venv is Python 3.14 with no extras and no litellm (graceful
# NER degradation): last known ~3530 passed + ~360 skipped, ~4min. The authoritative
# local run is .venv-bench = Python 3.14.7 with every extra (`ner` incl. pymorphy3,
# `postgres`, `oidc`, `asgi`, `metrics`) + litellm 1.101.0: ~4200 passed + 16 skipped
# with Postgres, ~8min — the entrypoint, route-guard, served-stack and container suites
# only RUN there. CI runs the same suite on Python 3.14 only — the same interpreter line as
# .venv-bench; nothing exercises 3.12 any more. Always run both before committing.
PYTHONPATH=src .venv/bin/pytest tests/ -q
NO_PROXY=127.0.0.1,localhost PYTHONPATH=src .venv-bench/bin/python -m pytest tests/ -q -rs

# NO_PROXY: on a machine with a system HTTP proxy the served-stack tests would talk to
# the proxy instead of the local server.
# Postgres-backed tests (token/team stores, issuance races) skip locally without a
# server and FAIL on CI (CI=true). To run them locally:
docker run --rm -d --name pg-test -e POSTGRES_USER=gateway -e POSTGRES_PASSWORD=gateway \
  -e POSTGRES_DB=gateway -p 55432:5432 postgres:16
CORP_TEST_PG_DSN=postgresql://gateway:gateway@localhost:55432/gateway NO_PROXY=127.0.0.1,localhost \
  PYTHONPATH=src .venv-bench/bin/python -m pytest tests/ -q -rs
# The route-gate container suite (tests/integration/test_route_gate_container.py) builds
# corp-llm-gateway:route-gate-test-<digest of the build inputs> on first use (CI's
# integration-container job does the same); CORP_GATEWAY_IMAGE=<image> uses a prebuilt one.

# Single test / file / node
PYTHONPATH=src .venv/bin/pytest tests/sanitizer/test_engine.py -q
PYTHONPATH=src .venv/bin/pytest tests/sanitizer/test_engine.py::test_name -q

# E2E (Langfuse + corp-llm-mock via docker compose; matches CI e2e:langfuse)
docker compose run --rm e2e pytest -q tests/e2e
```

## Tooling

- Python 3.12+ (`requires-python = ">=3.12"`; the package still supports 3.12,
  but CI runs only 3.14), ruff for lint and format (`ruff-pre-commit` v0.15.14 runs
  `ruff --fix` + `ruff-format` in pre-commit; CI's lint job runs `ruff check`
  AND `ruff format --check` on every PR). No type checker is configured.
- `Dockerfile.gateway`'s `python:3.12-slim` build stage tracks the litellm runtime
  image's interpreter, not CI — don't bump it with CI's Python.
- Async-first (LiteLLM hooks are async); pytest-asyncio mode = "auto"
- Default branch: `main`; the live release line is `release/1.0.x`
- CI: GitHub Actions (`.github/workflows/`)
- httpx for HTTP, Redis via `redis.asyncio`, fakeredis for tests
- First-time setup: `pip install -e ".[dev]" && pre-commit install` — `dev` pulls
  `asgi` (litellm[proxy] + fastapi + uvicorn, so `tests/test_asgi_entrypoint.py`
  runs instead of skipping) and `metrics` (prometheus-client). The full suite
  also wants `ner,postgres,oidc` — `ner` is natasha + spaCy + `pymorphy3` (the
  gazetteer's RU lemmatizer) — plus the `en_core_web_md` wheel CI installs
- `.github/workflows/ci.yml` gates every PR: a lint job (`ruff check` AND
  `ruff format --check` — running only `ruff check` locally can still leave
  you with a CI format failure), a single `test` job running the full pytest
  suite on Python **3.14** (no matrix) with the `ner`/`postgres`/`oidc`/`asgi`/`metrics`
  extras, a Postgres 16 service and `CORP_TEST_PG_DSN`, + helm render tests, and
  an `integration-container` job (route gate on the real image). All three run
  Python 3.14; `tests/test_ci_workflow.py` pins that

## CLI entry points

Wired in `pyproject.toml` `[project.scripts]`:

- `corp-llm-gateway` → `cli/status.py` (dev laptop diagnostics)
- `corp-llm-gateway-proxy` → `cli/proxy.py` (header-injecting localhost proxy, Pattern 3)
- `gateway-admin` → `cli/admin.py` (operator: team CRUD, retention, token issue/revoke)

## Config resolution

Every env var the app reads (`CORP_LLM_AUTH_PROVIDER`, `CORP_LLM_BEARER_TOKEN`,
`CORP_GATEWAY_URL`, `CORP_GATEWAY_TOKEN_FILE`, `CORP_LLM_CA_BUNDLE` (path to a
PEM CA bundle — verify corp-LLM TLS against an internal CA), …) resolves through:

1. env var
2. `$CORP_LLM_GATEWAY_CONFIG_FILE` → `~/.corp-llm-gateway/config.toml` →
   `/etc/corp-llm-gateway/config.toml` (first existing)
3. caller default

Loader: `src/corp_llm_gateway/config.py`. Template: `config.example.toml`.
When adding a new tunable, plumb it through this loader — don't read
`os.environ` directly at call sites.

## Critical invariants — never weaken these

1. **No originals leak** (M1-14): `tests/invariants/test_no_originals_leak.py`
   pins six surfaces (logger emissions, error bodies, exception traces,
   metric labels, forwarded headers, pod stdout). Any new code path that
   touches user content must be auditable against this gate.
2. **NEVER fields gate** (`audit/invariants.py`): the audit logger refuses
   to emit a record containing any NEVER field key (mapping/original/
   credentials). Vector VRL provides defense-in-depth for the same set.
3. **BYOK Authorization passthrough**: the developer's `Authorization:
   Bearer ...` header is forwarded untouched to upstream. Don't log it,
   don't rewrite it.
4. **X-Corp-Auth never logged**: corp tokens are stripped in pre_call
   (`AuthMiddleware.strip_corp_token`) and never appear in the audit
   pipeline.
5. **Length-descending placeholder substitution** (M1-9, lifted from
   `data-sanitizer/desanitize.py:18`): always sort placeholders longest
   first before replacement, otherwise short ones shadow long ones.
6. **Fail-policy matrix in M4** is the source of truth for component
   failure behavior. Don't add ad-hoc fail-open paths.
7. **Default-deny route gate**: no route reaches a provider unless
   `route_gate/table.py` says the hook rewrites its body, and the gateway
   does not serve unless that hook is registered. Adding a route to the
   table is a security decision, not a config change; there is no off
   switch (`CORP_LLM_ROUTE_GATE_EXTRA_PASSTHROUGH` adds PASSTHROUGH rows
   only). Startup exits 78 without litellm's config and 70 without a
   `CorpLlmGuardrail` in `litellm.callbacks` — `docs/security.md` §14,
   invariant row 7 in §9. Two rules under it (rows 7a/7b):
   - **No slot before the body is complete.** The in-flight limiter drains
     the whole body under `CORP_LLM_BODY_READ_SECONDS`, `CORP_LLM_MAX_DRAINING`
     and `CORP_LLM_MAX_DRAINING_BYTES` before it takes a slot; never acquire
     first. The cap is capacity, not authorization.
   - **Shared tasks via `inflight.spawn_shared`.** A task more than one
     request awaits (single-flight lookup, JWKS fetch, a shared refresh) must
     be started with `spawn_shared`, or one request's disconnect cancels it
     under every other waiter (`docs/extending.md`).

**Mode A (API keys / litellm virtual keys) is a test posture only; subscription
mode is production** (DRI decision 2026-09-27). The route gate refuses litellm's
whole management surface, so nothing can issue a virtual key; `deploy.sh`
defaults to `--mode oauth`. The container suite seeds a key straight into
litellm's DB to keep Mode A covered.

**Rewrite-vs-scan carve-out** (`sanitizer/identity_preamble.py`): a fixed client
protocol literal may be exempt from *rewriting* when the provider matches it
byte-exactly, but it must stay visible to the Stage-0 and Stage-5 scans. Claim
the exemption at the call site that knows the field and its position, never in
the per-leaf orchestrator — see `docs/security.md` §13.

## Conventions for new modules

When adding a new pluggable piece (storage backend, auth mode, sink, etc.),
follow the established interface-registry pattern:

1. ABC in `<module>/<base>.py` with the protocol
2. Real impls in `<module>/<impl_name>.py`
3. `<module>/__init__.py` re-exports the ABC + impls
4. Tests parametrize over impls where appropriate (see
   `tests/storage/test_mapping_store.py` for the contract-test pattern)
5. Stub impls raise `NotImplementedError` with a message naming the
   blocking task or env they're waiting on (see `auth/providers.py`)

## Things that are deliberately CPU-only / config-only

- **Pre-pass engine runs on CPU** (corp k8s has no GPU pods). If latency
  exceeds 4s p99, mitigate via M1-11 content-size threshold or
  scale-out — do NOT add GPU dependencies.
- **Corp LLM is currently auth-less** but the gateway is built behind
  `CorpLlmAuthProvider`. Switching to real auth is config-only (env
  var + k8s secret); never write inline auth at call sites.
- **Switching SIEM / Postgres / Vector backends is config-only.**
  Don't hardcode product names in src/.

## Plan + memory

- Plan revisions are tracked in the plan header (`rev N — what changed`).
  Bump rev N when changing plan content; the body is the source of
  truth, never duplicate decisions in CLAUDE.md.
- For any decision that affects future sessions, save to memory at
  `~/.claude/projects/.../memory/` per the auto-memory rules — not in
  this file.

## Things NOT to do

- Don't rename the default branch (`main`).
- CI is GitHub Actions (`.github/workflows/`); git hosting is GitHub. Keep CI on
  GitHub Actions — don't add other CI systems.
- Don't add GPU deps.
- Don't introduce a non-OpenAI/Anthropic provider in v1 (Bedrock /
  Gemini / Azure are explicit v2).
- Don't bypass the M1-14 invariant test by skipping or marking xfail.
- Don't commit secrets — `.gitignore` excludes `.env`, `.envrc`,
  `.claude/settings.local.json`. CI has `detect-private-key` as a
  pre-commit hook.

## Useful one-liners

```
# Quick sanity check (matches CI lint:python — both check and format-check)
PYTHONPATH=src .venv/bin/ruff check src tests \
  && PYTHONPATH=src .venv/bin/ruff format --check src tests \
  && PYTHONPATH=src .venv/bin/pytest tests/ -q

# Helm chart lint (matches CI lint:helm)
helm lint helm/corp-llm-gateway

# Coverage report
PYTHONPATH=src .venv/bin/pytest -q --cov=corp_llm_gateway --cov-report=term-missing

# See remaining work
cat docs/remaining-steps.md

# See plan rev
head -3 docs/plans/20260507-external-sanitizer-gateway-v1.md

# Cold-boot the colleague demo stack (~3-5 min first time)
scripts/demo.sh up

# Watch only the sanitize/desanitize flow (tails litellm, filtered)
scripts/demo.sh logs
```
