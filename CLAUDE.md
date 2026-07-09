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
  auth/         CorpLlmAuthProvider (Noop default; Bearer/mTLS/OIDC) + get_auth_provider factory
  audit/        AuditEvent + Logger + Sinks + get_sink factory + retention generator + NEVER-fields gate
  bootstrap.py  production composition root: build_guardrail() from config; lazy PEP-562 `guardrail` singleton
                (the LiteLLM callback target — importing the module is side-effect-free)
  cli/          gateway-admin (operators: team/token/extensions/config check) + corp-llm-gateway status (devs) + proxy
  config.py     env → $CORP_LLM_GATEWAY_CONFIG_FILE → ~/.config → /etc → default loader; get/get_required/get_table/validate
  corp_llm/     httpx client speaking vLLM /v1/chat/completions
  detectors/    PIIDetector + regex_checksum + dual_ner (RU Natasha + EN spaCy); NerUnavailableError (fail-closed)
  extensions/   ExtensionRegistry + ExtensionSpec (kind/api_version/fail_policy); fail-closed register; api-version gate
  healthz/      live / ready / sanitization / extensions checks + ASGI server (build_health_router, serves /healthz/* + /internal/issue-token)
  metrics/      MetricsExporter (noop default / prometheus); emits blocked_requests_total{block_reason} + gateway_failure{component}
  payload/      size threshold + gzip + per-team quota + oversize policy (fail-closed default)
  profiles/     plugin bundles — ProfileBundle/PolicyKnobs(merge) + loaders/resolver + DETECTOR_REGISTRY + manifest (hash-integrity) + defaults/
  providers/    ProviderRegistry + executable v1-guard (anthropic/openai/corp-vllm; v2 behind CORP_ALLOW_V2_PROVIDERS)
  rules/        replace.md parser + gazetteer + cached file loader
  sanitizer/    local-first engine + segmenter + StreamingDesanitizer + DLP guard + orchestrator + ProfileAwareOrchestrator (live profiles)
  settings.py   single source of truth (typed KEYS registry + validate()); config.py delegates; backs `config check`
  storage/      MappingStore (in-memory + Redis)
  team_config/  TeamConfig (+ profile_ids) + store (in-memory + Postgres) + schema.sql
  tokens/       schema.sql + AuthMiddleware + TokenIssuer + stores
  litellm_hook.py  CorpLlmGuardrail — LiteLLM callback adapter (sanitize/desanitize incl. OpenAI tool_calls + streaming)
helm/corp-llm-gateway/   Helm chart (gateway image + guardrail callback + Secret + HPA/PDB/SA + ServiceMonitor + config-check
                          initContainer + env passthrough + NetworkPolicy + CoreDNS sinkhole)
docs/                    plans/ + audit-schema + security + ops/* (install/configuration/admin-cli/upgrade/profiles/runbook/capacity) + rbac-matrix + adr/*
scripts/install.sh       laptop installer (bash/zsh/fish, macOS/Linux)
tests/                   pytest, pytest-asyncio mode=auto (~1392 passed / 91 skipped on 3.14; full NER + RS256 crypto run on 3.12/CI)
```

The GA-readiness / security / extensibility build is `docs/plans/20260708-ga-readiness-security-extensibility.md`
(profiles/extensions/metrics/settings/bootstrap all landed there). Key new seams (see the `safe-extension-registry`
+ `lazy-entrypoint-singleton` skills): `extensions/` and `providers/` are keyed registries; `profiles/` is the
data-bundle plugin layer; `bootstrap.build_guardrail()` wires everything from config.

## Request lifecycle (read once, then you understand the engine)

The cascade was **inverted to local-first** (plan `docs/plans/20260630-bilingual-local-first-detection.md`,
decision `docs/adr/ADR-003-ner-orchestration.md`). Old order was LLM-oracle-first; now:

```
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

The old three tiers (FunctionCall → JSON → Regex, `sanitizer/engine.py`) still parse the oracle's
response when it IS called; they are no longer the primary detection path. Local detectors live in
`detectors/` (`regex_checksum`, `ner_ru`/`ner_en`/`dual_ner`) + `rules/gazetteer.py` +
`sanitizer/segmenter/`; NER needs Python 3.12 (no 3.14 wheels — lazy imports keep the package
importable on 3.14 with graceful degradation).

Two caches:

- **Cache A** — content-keyed dedup, shared across conversations, TTL ~10h.
- **Cache B** — per-conversation mapping store (Redis or in-memory),
  sliding TTL ~1h, **required** for `post_call` to undo redactions.
  Today `conversation_id == request_id`, so Cache B doesn't reuse across
  sibling requests; see `docs/conversation-id.md`.

## Running tests

```
# Full unit suite. Local .venv is Python 3.14 (graceful NER degradation): last known
# 817 passed + 39 skipped, ~20s. Authoritative NER run is Python 3.12 (.venv-bench,
# with the `ner`/`postgres`/`oidc` extras): 875 passed. Always run before committing.
PYTHONPATH=src .venv/bin/pytest tests/ -q

# Single test / file / node
PYTHONPATH=src .venv/bin/pytest tests/sanitizer/test_engine.py -q
PYTHONPATH=src .venv/bin/pytest tests/sanitizer/test_engine.py::test_name -q

# E2E (Langfuse + corp-llm-mock via docker compose; matches CI e2e:langfuse)
docker compose run --rm e2e pytest -q tests/e2e
```

## Tooling

- Python 3.12+, mypy strict, ruff for lint+format
- Async-first (LiteLLM hooks are async); pytest-asyncio mode = "auto"
- Default branch: `master` (NOT main)
- CI: CI (`the CI config`); NOT other CI
- httpx for HTTP, Redis via `redis.asyncio`, fakeredis for tests
- First-time setup: `pip install -e ".[dev]" && pre-commit install`
- CI lint runs both `ruff check` AND `ruff format --check` — running only
  `ruff check` locally can still leave you with a CI format failure

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

- Don't rename `master` to `main`.
- Don't add `a non-standard CI config` — CI stays on internal git host (`the CI config`).
  Git hosting IS internal git host (`git.corp.lan`); that split
  is intentional, not a migration artefact.
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
