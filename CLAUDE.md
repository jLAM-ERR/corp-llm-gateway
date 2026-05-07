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
  auth/         CorpLlmAuthProvider (Noop default; Bearer/mTLS/OIDC stubs)
  audit/        AuditEvent + Logger + Sinks + retention generator
  cli/          gateway-admin (operators) + corp-llm-gateway status (devs)
  corp_llm/     httpx client speaking vLLM /v1/chat/completions
  detectors/    PIIDetector + ShadowDetector
  healthz/      live / ready / sanitization deep-check
  payload/      size threshold + gzip + per-team quota
  rules/        replace.md parser + cached file loader
  sanitizer/    Three-tier strategies + engine + StreamingDesanitizer + orchestrator
  storage/      MappingStore (in-memory + Redis)
  team_config/  TeamConfig + store
  tokens/       schema.sql + AuthMiddleware + TokenIssuer
  litellm_hook.py  CorpLlmGuardrail — LiteLLM callback adapter (M1-7)
helm/corp-llm-gateway/   Helm chart (deployment, service, configmap, NetworkPolicy, CoreDNS sinkhole)
docs/                    plan + audit-schema + ops/* + rbac-matrix + replace-md-authoring + remaining-steps
scripts/install.sh       laptop installer (bash/zsh/fish, macOS/Linux)
tests/                   pytest, pytest-asyncio mode=auto
```

## Running tests

```
PYTHONPATH=src .venv/bin/pytest tests/ -q
```

265 tests should pass in ~16s. **Always run before committing.**

## Tooling

- Python 3.12+, mypy strict, ruff for lint+format
- Async-first (LiteLLM hooks are async); pytest-asyncio mode = "auto"
- Default branch: `master` (NOT main)
- CI: CI (`the CI config`); NOT other CI
- httpx for HTTP, Redis via `redis.asyncio`, fakeredis for tests

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
- Don't add internal git host references; we're on internal git host.
- Don't add GPU deps.
- Don't introduce a non-OpenAI/Anthropic provider in v1 (Bedrock /
  Gemini / Azure are explicit v2).
- Don't bypass the M1-14 invariant test by skipping or marking xfail.
- Don't commit secrets — `.gitignore` excludes `.env`, `.envrc`,
  `.claude/settings.local.json`. CI has `detect-private-key` as a
  pre-commit hook.

## Useful one-liners

```
# Quick sanity check (lint + tests)
PYTHONPATH=src .venv/bin/ruff check src tests && PYTHONPATH=src .venv/bin/pytest tests/ -q

# See remaining work
cat docs/remaining-steps.md

# See plan rev
head -3 docs/plans/20260507-external-sanitizer-gateway-v1.md
```
