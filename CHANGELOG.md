# Changelog

All notable changes to corp-llm-gateway are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Added — production compose deploy target (`compose/`)

- **A second production deploy target**, for hosts without Kubernetes, alongside the Helm chart:
  the data plane (`litellm` + `redis` + `postgres`), **self-hosted Langfuse v3** (web/worker,
  ClickHouse, MinIO, its own capped Redis — no host port published by any of them) and the
  **audit pipeline** (`vector`, reading a read-only bind of the container log directory rather
  than the docker socket, with the `never_fields_gate` / `audit_only` transforms byte-identical
  to the Helm chart's configmap). Every secret comes from `.env`; the four keys with no default make
  `docker compose up` refuse to start rather than boot half-configured.
- **Two mutually exclusive auth modes.** Mode A — corp API keys, developers hold a LiteLLM
  virtual key (per-person revocation + spend). Mode B (`docker-compose.oauth.yml`) — the
  developer's own Anthropic subscription OAuth bearer is forwarded upstream and **no corp
  `ANTHROPIC_API_KEY` exists at all**; it serves `claude-*` only, which is a binding control
  rather than a simplification (litellm resolves the deployment after the hook runs, so an
  Anthropic-only routing table is the only proof of where a request lands). A master key and the
  bridge cannot coexist — `build_guardrail()` refuses to boot and names the cause.
- **Server bootstrap + deploy scripts** — `scripts/deploy/bootstrap-server.sh` (idempotent day-0
  host prep + optional systemd unit) and `scripts/deploy/deploy.sh` (`up`/`down`/`restart`/
  `logs`/`status`, `--mode oauth`, `--dry-run`, `--yes`). The local `.env` is never uploaded and
  the server's is never read or overwritten.
- **`docker-compose.build.yml`** — build the current branch instead of the published tag, pinned
  to the `ru-en` NER profile (the `base` default ships no EN model and would 503 every request
  under `CORP_LLM_REQUIRE_NER=1`).

### Added — corp NER service (optional, off by default)

- **`CorpNerDetector` + `corp_ner/` client** — a remote NER detector appended to the local-first
  cascade, off unless `CORP_NER_ENABLED=1`, and requiring `CORP_NER_ENDPOINT` when on (enabled
  without an endpoint is a boot refusal, not a silent skip). Tuning:
  `CORP_NER_TIMEOUT_S` / `CORP_NER_MAX_TEXTS` / `CORP_NER_MAX_INPUT_CHARS` / `CORP_NER_CA_BUNDLE`.
- **Network-backed detectors are excluded from `CODE` segments** — shipping source to an external
  service is the leak this prevents. All *local* detectors keep scanning code; the exclusion is
  local-vs-network, not code-safe-vs-not.
- **The NER call carries raw user content**, so its TLS verification can never be disabled; an
  internal CA goes through `CORP_NER_CA_BUNDLE`.
- A readiness check for the service, and `gateway-admin config check` coverage of the new keys.

### Added — detection

- **`BANK_CARD` Luhn rule** — IIN-plausible, Luhn-valid PANs (13–19 digits, tolerating space and
  hyphen grouping), with length and offset enforced before Luhn is spent so that a longer
  incidental Luhn hit cannot evict a real PAN.

### Changed — Cache A key boundary

- The Cache A key now folds a **detector-policy fingerprint** as well as a coverage-version
  constant, so entries produced under different detector coverage (corp NER on/off, a widened
  profile, a different NER engine capability, a different gazetteer lemmatizer) can never be
  served to each other. Flipping either network toggle needs **no cache flush**.

### Docs

- `docs/ops/deployment-modes.md` + `.ru.md` — the mode matrix, both network toggles, every
  failure mode.
- `docs/ops/deploy-handoff.md` + `.ru.md` — the condensed step-by-step for whoever runs the
  deploy.
- `compose/README.md` + `.ru.md` — the full stack reference (routing, virtual keys, why BYOK is
  not available here, Langfuse, the audit pipeline and its recovery procedures, TLS to the corp
  vLLM, environment posture).
- README (EN/RU) now documents the deployment targets, the compose stack and the corp NER
  toggle; the RU README caught up on the local-compose section, the `replace.md` matching
  semantics and the licence section.

### Known limitations of the compose target

- **No TLS in front of the stack yet** — the only published port is `127.0.0.1:4000`; the nginx
  front door is a later revision.
- **In Mode B litellm's management endpoints are unauthenticated** (`/key/*`, `/model/*`,
  `/user/*`, the UI) — its proxy auth is skipped without a master key, which is what the mode
  requires. The LLM routes stay gated by `X-Corp-Auth`. Must be closed at nginx before the port
  leaves loopback.
- **Audit is buffered but not fail-closed** — a documented deviation from the `vectorBufferFull`
  default in `docs/security.md` §8. Durability is bounded by docker log rotation.
- **No untrusted `docker run` on the host** — Vector's container-label filter is a
  misconfiguration guard, not a security boundary (`docs/security.md` §8.2).

## [1.0.0] — GA (2026-07-09)

The first GA release — the **local-first detection cycle** (below) plus the **GA-readiness /
security & extensibility** build. Non-negotiable criterion: zero confirmed leak incidents in the
90 days post-GA.

### Added — Local mode (oracle on/off switch + compose quickstart)
- **`CORP_LLM_ORACLE_ENABLED`** — on/off switch for the LLM oracle (corp vLLM). Off = local-first
  cascade only (replace.md, regex+checksum, dual-NER, gazetteer, splitter); no oracle call ever
  attempted, `CORP_LLM_ENDPOINT` no longer required. Refuses to boot as a no-op sanitizer if
  `CORP_LLM_LOCAL_FIRST` is also off.
- **`CORP_LLM_DEV_TEAM_TOKEN`** — dev-only seam that seeds a working `X-Corp-Auth` token for team
  `local-dev` in the in-memory token store; ignored (with a warning) when a Postgres DSN or
  `CORP_ENV=prod` is set.
- **`examples/compose/`** — docker-compose quickstart running the published GHCR image as a local
  sanitizing proxy in front of Anthropic/OpenAI with the oracle off; documents the BYOK trade-off
  of native anthropic/openai routing (gateway-side shared key, not per-developer passthrough).
  (first published image: v1.0.0-rc.5)

### Added — GA-readiness, security & extensibility
- **Plugin / profile layer** — declarative `profiles/` bundles (country / division / regime),
  monotone-tightening `PolicyKnobs.merge`, hash-sealed integrity, SHA-256 cross-jurisdiction cache
  isolation, `TeamConfig.profile_ids` selection.
- **Extension seams** — keyed `extensions/` + `providers/` registries (fail-closed register +
  api-version gate; v1 anthropic / openai / corp-vllm, v2 gated), `DETECTOR_REGISTRY`, pluggable
  metrics exporter, `bootstrap.build_guardrail()` composition root; contributor guide
  `docs/extending.md`.
- **Security hardening** — 11 repro-first leak-surface fixes (oversize + NER fail-closed, OpenAI
  `tool_calls` + streaming, segmenter coverage, `X-Corp-Auth` stripping across all header locations,
  dev-proxy host-pin, error-body, TLS/RBAC, recursive NEVER-gate, RS256 + aud/iss).
- **Ops** — real `gateway-admin` (team / token / extensions / config check), production Helm chart
  (guardrail image + callback, config-check initContainer, NetworkPolicy, CoreDNS sinkhole), served
  healthz, ops docs.
- **`replace.md`** — `=` is now the canonical rule separator (legacy `→` still parsed).
- **Release tooling** — shared `scripts/release/{gates,ship,cut-rc}.sh` delivery scripts,
  `github-release` workflow (auto GitHub Release on `v*` tags, `--prerelease` for rc),
  `docs/ops/release.md`, least-privilege `dco.yml` permissions (closes CodeQL alert #1).
- **ChatGPT Codex Responses profile** (opt-in, `CORP_LLM_FORWARD_CHATGPT_AUTH`) — OpenAI
  Responses API sanitize/desanitize coverage (`input`/`instructions`, `custom_tool_call`,
  `reasoning.summary[]`, `local_shell_call`, `mcp_call`, function-call arguments, streaming
  events including a placeholder split across SSE chunks) and a header bridge that forwards
  the developer's live ChatGPT subscription OAuth to the Codex backend instead of a static
  provider key. Wired into `bootstrap.build_guardrail()`, so the flag is honored in every
  deployment (Helm/k8s included), not just the docker-compose demo overlay. See
  `docs/chatgpt-codex.md`.

### Changed — `replace.md` rule-matching semantics (behavior change for every existing dictionary)

- **Case-insensitive substring matching (previously case-sensitive).** A `replace.md` rule now
  matches as a plain case-insensitive substring, for a single-word source or a multi-word
  phrase alike — a rule `Acme = [X]` now also matches `acme` and `ACME`, not just `Acme`. This
  is the real widening on upgrade: review existing dictionaries for short or common sources
  that also occur as ordinary lowercase/uppercase text. See `docs/replace-md-authoring.md`.
- **Rules and findings now compete in one longest-span-wins pool.** `replace.md` rule matches
  and detector/NER/oracle findings are selected from a single candidate pool ordered by span
  length descending — whichever span is longer wins, regardless of source; a rule wins a tie
  only when its span is identical to a finding's span. This guarantees a shorter rule can never
  silently discard a longer overlapping finding (and its Cache-B mapping).

### Changed — other flag-off behavior changes

- **Unrecognized Anthropic content-block types are now scanned instead of passed through
  unchanged.** A block type this gateway doesn't recognize (e.g. a new
  `web_fetch_tool_result` shape) used to egress as-is; it's now walked as a generic JSON
  value tree so every string leaf is sanitized — this widens redaction coverage on unmodified
  Anthropic traffic. See `docs/security.md` ("Not sanitized / deferred").
- **A request carrying both `messages` and `input` is now rejected (HTTP 422).** Previously
  the ambiguous shape forwarded whichever field the request-item walker happened to pick,
  silently bypassing sanitize/Stage 0/Stage 5 for the other field; the gateway now fails
  closed instead of guessing which one is real.

### Local-first detection cycle (2026-06-30)

> Plan: `docs/plans/20260630-bilingual-local-first-detection.md`
> ADR: `docs/adr/ADR-003-ner-orchestration.md` — hand-roll dual-NER (Natasha RU + spaCy EN)
> over Presidio-as-orchestrator and DeepPavlov/BERT (rejected: install-time kill-shot on CPU,
> 1.44 GB model, no wheels for torch<1.14 on modern platforms).
> Compliance delta: ✅ 2 / 🟡 8 / ❌ 5 → **✅ 11 / 🟡 3 / ⚪ 1** vs the 15 ИБ requirements.

### Added — Detection (Track 1, tasks DP-0…DP-9)

- `RegexChecksumDetector` (`detectors/regex_checksum.py`) — algorithm-validated ИНН (10/12),
  КПП, ОГРН (13/15), БИК, СНИЛС, р/счёт, plus JWT, PEM private key, `sk-`/`AKIA`/`ghp_`/
  generic `password=`, IPv4/6 (via `ipaddress`), CIDR, internal hostnames
  (`*.corp.internal/.lan/.local`), DB-URLs. Near-zero false positives via checksum. (DP-1)
- Bilingual `DualNerDetector` (`detectors/dual_ner.py`) — Natasha/Slovnet RU + spaCy
  `en_core_web_md` EN, run-both-union with de-overlap by longest span and provenance labels;
  covers ФИО, organisations, addresses in mixed-language requests. (DP-2)
- Local-first detection pass merged with oracle in `sanitizer/engine.py` — additive; oracle
  remains unconditionally on at DP-3, narrowed at DP-4. (DP-3)
- Lemma-gazetteer (`rules/gazetteer.py`) with built-in word-lists for products/code-names
  (`rules/defaults/products.txt`), regulated ПОД-ФТ/AML-CFT terms (`rules/defaults/regulated.txt`),
  and confidentiality markings (`rules/defaults/markings.txt`). Lemma-matched so inflected forms
  (`легализации`) hit. Oracle invoked only on a gazetteer hit. (DP-4)
- Code-aware segmenter + identifier splitter (`sanitizer/segmenter/`) — splits camel/snake
  identifiers (`CompanynameabcService` → `Companynameabc`) and scans segments against the
  gazetteer. (DP-5)
- Stage 0 pre-egress payload classifier (`payload/classifier.py`) — `.env`, kubeconfig,
  nginx.conf, log-dump/stack-trace signatures → HTTP 422 `block_reason`; upstream never called.
  `block_reason` is a CONDITIONAL audit field, carried to Langfuse. (DP-6)
- Stage 5 DLP egress guard (`sanitizer/dlp_guard.py`) — independent second-layer re-scan of the
  sanitized outbound payload for canary strings and high-confidence secrets; blocks any survivor
  with HTTP 422. (DP-7)
- Test-data allowlist (`sanitizer/allowlist.py`) — deterministic exemption for test fixtures;
  designed so it cannot suppress actual secrets. (DP-8)
- NER imports are lazy; Natasha + spaCy in `[ner]` optional extra. Python 3.14 degrades
  gracefully (no NER wheels); authoritative test run on Python 3.12 (875 passed). (DP-2, DP-9)
- Thread-offload of local NER off the async event loop (`asyncio.get_event_loop().run_in_executor`)
  to avoid blocking LiteLLM's callback coroutine. (DP-9)
- Demo LiteLLM image baked with `[ner]` extra — bilingual NER live in the demo stack.

### Added — Compliance (Track 2, tasks CP-1…CP-4)

- `PostgresTokenStore` (`tokens/postgres_store.py`) — asyncpg-backed persistent token store;
  `make_auth_middleware()` selects it when `CORP_LLM_PG_DSN` is set; contract tests
  parametrised over in-memory + Postgres backends. (CP-1)
- `gateway:operator` RBAC gate on admin CLI — `verify_operator()` in `auth/rbac.py` checks
  JWT claim via PyJWT; `_enforce_rbac()` called at each `gateway-admin` mutating subcommand;
  failure → stderr + exit code 2. (CP-2)
- SIEM sink wired in Vector configmap (HTTP sink under `audit.sinks.siem.enabled`, inherits
  NEVER-VRL gate). Helm alerts `AuditVectorDropHigh` + `LeakAttemptDetected` in
  `helm/.../templates/siem-alerts.yaml` with CI render asserts. Endpoint remains placeholder
  pending open Q#3. (CP-3)
- `NetworkPolicy` + CoreDNS sinkhole enabled in `helm/.../values-prod.yaml`; egress constrained
  to upstream + corp CIDRs. (CP-4)

### Fixed

- Audit for Stage-0/Stage-5 blocks now emitted inline via `async_log_failure_event` (idempotent);
  `block_reason` appears in all audit sinks including Langfuse.
- Pre_call rejections (auth failure, bad request, corp-LLM-down) all audited inline.
- Dev-proxy upstream URL now rebuilt with `urlunsplit` — scheme+netloc pinned from config,
  client target confined to path+query; closes CodeQL `py/full-ssrf` alert #2 (critical).
- Helm chart now projects the litellm callback shim (`configMap.items`) into the mounted
  `/etc/litellm` dir alongside `config.yaml`, so a cluster deploy of the published GHCR image
  boots instead of failing with `ImportError: Could not import guardrail` (litellm resolves
  `callbacks:` as a file path, not a package import); render-tested in `tests/helm/`.

---

## [0.0.2] — v1 sanitization core + ops (2026-05-07, plan rev 7)

> Plan: `docs/plans/20260507-external-sanitizer-gateway-v1.md` (milestones M0–M8).
> Milestones M1–M6 + M8 code-complete. M0 provisioning, M5 cluster enforcement,
> rollout phases, and sign-offs remain (infra- and process-gated).

### Added

**M0 — Foundations**

- Repo scaffold: `corp_llm_gateway` package, `pyproject.toml` entry points, pre-commit hooks,
  CI skeleton.
- Helm chart (`helm/corp-llm-gateway/`) — Deployment (litellm + vector sidecar), Service,
  Ingress, ConfigMap, NetworkPolicy, CoreDNS sinkhole templates.
- Corp-LLM (vLLM) contract closed; `CorpLlmClient` (`corp_llm/`) speaking
  `/v1/chat/completions`.

**M1 — Sanitization core**

- `PIIDetector` ABC + `ShadowDetector` registry (`detectors/`); ADR-001 interface-registry
  pattern.
- `MappingStore` (`storage/`) with in-memory and Redis backends; contract-test parametrisation.
- `CorpLlmSanitizer` with original three-tier strategy: `FunctionCallStrategy → JsonStrategy →
  RegexStrategy` (first to succeed wins; regex is the floor).
- Length-descending placeholder substitution invariant (#5, M1-9).
- `StreamingDesanitizer` (`sanitizer/`) with rolling SSE-aware buffer for Anthropic and OpenAI
  streaming.
- `RequestPlaceholderAllocator` — per-request bijection preventing cross-segment placeholder
  collision.
- Content-block walker: sanitizes `tool_use.input`, `tool_result`, `document`, `system` blocks;
  streaming `tool_use` desanitize; `thinking` blocks passed through by design (Anthropic-signed).
- `litellm_hook.py` `CorpLlmGuardrail` — `async_pre_call_hook`, `async_post_call_success_hook`,
  streaming iterator hook, `async_log_*` audit callbacks. (M1-7)
- `replace.md` parser + 5-minute cached file loader (M1-10, M1-15).
- Payload size threshold + gzip + per-team quota helpers (`payload/`). (M1-11)

**M2 — Auth & multi-tenancy**

- `tokens/schema.sql` + `AuthMiddleware` with 60 s revocation cache.
- `TokenIssuer` with pluggable OIDC verifier (M2-3).
- `TeamConfigStore` with per-team retention config + fail-policy overrides (M2-4).
- `gateway-admin` CLI skeleton: `team create/update/delete`, `token issue/revoke` (M2-5).
- BYOK `Authorization: Bearer` passthrough invariant (#3).

**M3 — Audit pipeline**

- `AuditEvent` schema with ALWAYS / CONDITIONAL / NEVER field tiers; `docs/audit-schema.md`.
- Structured audit logger + NEVER-fields gate (`audit/invariants.py`); Vector VRL
  defense-in-depth for the same field set.
- Langfuse sink + e2e integration test + CI job (M3-4).
- S3 lifecycle-policy generator from team retention config (M3-7).
- `finding_label_counts` + distinct-secret counts in audit events.

**M4 — Failure modes & health**

- `/healthz/live`, `/healthz/ready`, `/healthz/sanitization` deep-check endpoints.
- Fail-policy matrix (M4) as source of truth; 503 `E_CORP_LLM_DOWN` + fail-closed paths;
  no ad-hoc fail-open paths in code.

**M5 — Egress / CoreDNS**

- Helm templates for `NetworkPolicy` egress lockdown + CoreDNS sinkhole.
- Corp-LLM TLS verified via `CORP_LLM_CA_BUNDLE` (Corp CA bundle; `SSL_CERT_FILE` for
  LiteLLM's aiohttp path).

**M6 — Onboarding**

- `scripts/install.sh` — bash/zsh/fish, macOS/Linux, Keycloak device-flow OAuth, idempotent
  rc-block updater, round-trip smoke test.
- `corp-llm-gateway status` CLI (dev diagnostics — token present, gateway live, version,
  update check).
- `corp-llm-gateway-proxy` localhost header-injecting proxy (Pattern 3, re-reads token file
  per request).
- Auto-update check + CI release job (M6-6…M6-8).

**M8 — Documentation**

- `docs/ops/runbook.md`, `docs/ops/capacity.md` (sizing alpha → GA at 1000 devs / 50 RPS).
- `docs/replace-md-authoring.md`, `docs/rbac-matrix.md`, ADR-001 (interface-registry).
- `docs/security.md` — sanitization coverage, audit-pipeline guarantees, known config gaps.
- TOML property-file fallback for all env vars (`config.py`, `config.example.toml`).
- Internal git mirror created; open Q#1 closed.

### Fixed

- Anthropic content-block leak — content walker now sanitizes block lists, `tool_result`,
  `system`.
- Cross-segment placeholder collision — `RequestPlaceholderAllocator` bijection.
- User-typed literal placeholder collision prevented (case-4 hardening).
- SSE-aware streaming desanitization for both Anthropic and OpenAI wire formats.
- Audit attribution keyed on `litellm_call_id`; audit records retain real identity +
  `redaction_count` across pre/post handoff.
- Production Vector configmap: duplicate `transforms:` key fixed; NEVER-gate complete;
  `audit_only` path added.
- Corp-LLM fail-closed 503 on `E_CORP_LLM_DOWN`; correct audit attribution restored.

---

## [0.0.1] — initial scaffold (2026-05-07)

### Added

- Repo scaffold, CI skeleton, `pyproject.toml` with CLI entry points
  (`corp-llm-gateway`, `corp-llm-gateway-proxy`, `gateway-admin`).
- `CorpLlmAuthProvider` pluggable auth interface (`auth/`) — Noop default; Bearer/mTLS/OIDC
  stubs raise `NotImplementedError` naming the blocking task.
- `PIIDetector` ABC + `ShadowDetector` stub.
