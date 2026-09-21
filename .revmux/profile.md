# Project profile — corp-llm-gateway

## What it is

- A corporate LLM gateway, plugged into LiteLLM as a guardrail callback. It sanitizes traffic between
  developer Claude Code / Codex clients and Anthropic / OpenAI **before any bytes leave the corp
  boundary**, then restores the originals on the way back.
- A security control first and a service second. The non-negotiable success criterion is **zero
  confirmed leak incidents** in the 90 days after GA.
- Python 3.12+, async-first. Ships as a container image, deployed by Helm (k8s, CPU-only pods) or by
  the production compose stack in `compose/` driven by `scripts/deploy/deploy.sh`.
- One DRI, small team, open-core (Apache 2.0 core, DCO sign-off required on every commit).

## What a real failure looks like

- An original value (PII, regulated term, secret) reaching an upstream provider unsanitized.
- An original, a BYOK `Authorization` value, or an `X-Corp-Auth` token appearing on any of the six
  pinned surfaces: logger emissions, error bodies, exception traces, metric labels, forwarded
  headers, pod stdout. Also in any audit record (NEVER-fields gate).
- A path that **fails open**: a component outage, malformed input, missing profile or missing NER
  model that lets a request continue unsanitized instead of returning the documented 4xx/5xx.
- A control-plane or admin route (LiteLLM UI, key minting, `/internal/issue-token`, SSO routes)
  reachable from the public side of a proxy or ingress.
- A placeholder bijection break: two originals sharing one token, one original getting two, or a
  short placeholder shadowing a long one on substitution (must be length-descending).
- A Cache-A hit that skips detectors whose coverage has since changed.
- TLS verification switched off anywhere, or a proxy URL / credential baked into image metadata.
- A deploy script or compose change that works on the author's laptop and silently drops a file on
  the real host (rsync excludes, profile flags missing on some Compose invocations).

## Blast radius

- Every developer in the corporation routes LLM traffic through this. A leak is a regulatory
  incident, not a bug ticket; an outage blocks all AI-assisted work but leaks nothing.
- So: **an availability defect is major at worst; a confidentiality defect is critical by default.**

## The reporting bar

- Report what would cause a leak, a fail-open, an exposed control plane, a broken deploy, or an
  implementer building the wrong thing from a plan. Those are material.
- A deviation from a documented rule (below) is always worth reporting, with the rule cited.
- Noise here: style ruff already enforces, naming taste, "could add a type hint" (no type checker is
  configured, deliberately), requests for GPU/perf rewrites, suggestions to add a new CI system or a
  non-OpenAI/Anthropic provider, generic "add more tests" without naming the untested case.
- For a **plan** under review: a finding must say what an implementer executing the task literally
  would build wrong, and quote the plan line plus the repo file that contradicts it.

## Where the rules live

- `CLAUDE.md` — layout, request lifecycle, the six critical invariants, conventions, do-not list.
- `docs/security.md` — threat model (§1), coverage (§2), placeholder model (§3), fail-policy matrix
  (§8, source of truth), invariants (§9), known gaps (§11), subscription-auth bridges (§13).
- `docs/extending.md` — the extension seams (detector / sink / metrics / provider / profile).
- `docs/ops/*` — install, configuration, deploy handoff, deployment modes, runbook (EN + `.ru.md`).
- `CONTRIBUTING.md` — DCO sign-off, legal terms.
- `tests/invariants/test_no_originals_leak.py` — the executable form of the no-leak invariant.
- `pyproject.toml` `[tool.ruff]` — line length 100, rule set E,F,W,I,N,UP,B,C4,SIM,RUF.

## Conventions that are deliberate — do not file as defects

- **Fail-closed everywhere on the sanitization path.** A 503 instead of degraded service is the
  design. The only `continue` rows are the ones listed in `docs/security.md` §8.
- **Local-first detection; the LLM oracle is a conditional fallback**, called only on a
  deterministic gazetteer hit. "The oracle is not called" is the latency win, not a gap.
- **Local detectors are code-safe and DO scan CODE segments.** Only network-backed corp NER is kept
  off CODE segments.
- **NER is lazily imported and degrades gracefully on Python 3.14** (no wheels). Python 3.12 / CI is
  the authoritative run; `CORP_LLM_REQUIRE_NER` makes absence fail-closed in production.
- **BYOK `Authorization` is forwarded untouched** and never logged or rewritten.
- **Thinking blocks pass through un-desanitized** — Anthropic signs them.
- **Rewrite-vs-scan carve-out**: a byte-exact client protocol literal may skip rewriting but stays
  visible to the Stage-0 and Stage-5 scans, and is claimed at the call site, never per-leaf.
- **Config goes through `config.py` / `settings.py`**; no `os.environ` reads at call sites. Backends
  (SIEM, Postgres, Vector, Redis) switch by config only; no product names hardcoded in `src/`.
- **Pluggable pieces follow the ABC + registry pattern**; stubs raise `NotImplementedError` naming
  what they wait on.
- **Corp LLM is auth-less today** behind `CorpLlmAuthProvider`; that is a known, config-only seam.
- **`conversation_id == request_id` today**, so Cache B does not reuse across sibling requests.
- **`docs/plans/` and `docs/adr/` are gitignored.** A plan or ADR cited by path may exist only on the
  DRI's machine; its absence from git is not a finding.
- **`crt/` is build-time egress trust baked into the image; `compose/certs/` is runtime trust mounted
  into the container; neither is a certificate a server presents.** They have been confused before.
- **Docs come in EN/RU twins.** A change to one without the other is worth reporting.
- **Topic branches (`fix/*`, `feature/*`, `chore/*`) merge only into `release/*`**; linear history.
  The default branch is `main`; the live release line is `release/1.0.x`.
- **No GPU dependencies, no CI system other than GitHub Actions, no v2 providers** (Bedrock, Gemini,
  Azure) in v1.

## Languages in play

- Python (src, tests) — the ruff bar applies.
- YAML — Helm chart, Docker Compose, GitHub Actions. Bash — `scripts/` (installer, deploy, demo).
- Dockerfiles, nginx / Vector config, Markdown. Judge each by its own conventions, not Python's.
