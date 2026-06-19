# Demo: 6-Prompt Walkthrough

A curated set of prompts that exercise each sanitizer tier, Cache A, and the oversize skip (M1-11). The fail-closed audit posture is described last as a *planned* behavior (not yet wired in the demo build) so you can speak to the v1 design without live-firing a feature that isn't there.

## Stage 0 — Presenter Setup

**Two-shell layout:**
- **Left shell:** your laptop, running Claude Code
- **Right shell/browser:** http://localhost:3000 (Langfuse OSS UI), login with `demo@corp.lan` / `demo-password-12345` (pre-seeded via `LANGFUSE_INIT_*` in `docker-compose.demo.yml`)

**Second shell environment:**
Run this in the second shell (the one where you'll interact with Claude Code) to route traffic through the demo gateway:

```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_CUSTOM_HEADERS='X-Corp-Auth: demo-team-token'
```

Alternatively, run `scripts/demo.sh presenter-env` and copy-paste the output.

**Seeing redaction before→after — the `sanitize` helper.** Langfuse stores
audit **metadata only**; it never stores prompt/response text (by design — no
original content in the audit store, so a trace's **Input/Output are always
empty**). To show the actual sanitization, use the gateway's `sanitize` CLI,
which runs the same three-tier sanitizer against the corp LLM and prints
before→after:

```bash
docker compose -f docker-compose.demo.yml exec litellm \
  gateway-admin sanitize "Draft a follow-up email to the DRI@gmail.com about the Q3 plan."
```

```
BEFORE: Draft a follow-up email to the DRI@gmail.com about the Q3 plan.
AFTER : Draft a follow-up email to [EMAIL_001] about the Q3 plan.
redactions: 1
  the DRI@gmail.com -> [EMAIL_001]
```

Add `--json` for scripted output. This is a **side tool**: it does not egress
to the upstream model and does not write to the audit pipeline, so it's safe
to run live. The prompts below say "run the Stage 0 `sanitize` helper" — that
means this command with the prompt's own text.

---

## Prompt 1 — Baseline (No Sanitization)

**Text:**
```
What's the capital of France?
```

**What to highlight in Langfuse:**
- Open the trace for this prompt
- Navigate to the "Metadata" or "Redactions" section
- Confirm: `redactions=0` (no PII detected, no tier triggered)

**Expected tier:** None — this is a clean, non-sensitive query.

---

## Prompt 2 — PII / Regex Tier

**Text:**
```
Draft a follow-up email to the DRI@gmail.com about the Q3 plan.
```

**What to show:**
- **Redaction (before→after)** — run the Stage 0 `sanitize` helper with this prompt's text; confirm the email becomes `[EMAIL_001]` in the `AFTER` line (the corp LLM emits `[LABEL_NNN]` shape — see `src/corp_llm_gateway/sanitizer/orchestrator.py:_build_system_prompt`).
- **Audit (Langfuse)** — open the trace → **Metadata** tab → confirm `redaction_count: 1` and that `placeholder_list` contains `[EMAIL_001]`. The trace's **Input/Output are intentionally empty** — the audit store holds metadata only, never prompt/response content.
- **Restored output (Claude Code)** — confirm the original email `the DRI@gmail.com` is rebuilt in the model's answer (the post-call desanitizer worked).

**Expected tier:** Regex — the email pattern matched the regex tier's PII detector.

---

## Prompt 3 — JSON Tier

**Text:**
```
Validate this JSON config and explain any issues:

{"endpoint": "https://api.internal", "api_key": "sk_live_AKIAIOSFODNN7EXAMPLE"}
```

**What to show:**
- **Redaction** — run the Stage 0 `sanitize` helper with this prompt's text; confirm the `api_key` **value** is redacted (e.g. `"api_key": "[TOKEN_001]"`) while the **field name** `api_key` is preserved. The endpoint URL may also be redacted depending on your rules.
- **Audit (Langfuse)** — trace → **Metadata** → `redaction_count` ≥ 1 and the token placeholder appears in `placeholder_list`. (Input/Output empty by design — metadata-only audit store.)

**Expected tier:** JSON — the detector recognized a structured AWS/cloud API key pattern.

---

## Prompt 4 — FunctionCall Tier

**Text:**
```
Use the search_kb tool with query='customer email j.doe@corp.lan asked about X'
```

**What to show:**
- **Redaction** — run the Stage 0 `sanitize` helper with this prompt's text; confirm the email inside `query=...` is redacted to `[EMAIL_002]` (same `[LABEL_NNN]` shape as Prompt 2).
- **Restored output (Claude Code)** — after the model executes the tool, the desanitizer rebuilds the original email in the rendered response.
- **Audit (Langfuse)** — trace → **Metadata** → the redaction is reflected in `redaction_count` / `placeholder_list`. (Input/Output empty by design.)

**Expected tier:** FunctionCall — the tier successfully detected and redacted sensitive data in structured function-call arguments.

---

## Prompt 5 — Cache A Hit

**Text:**
```
Draft a follow-up email to the DRI@gmail.com about the Q3 plan.
```

(This is **Prompt 2 verbatim**. Send it again.)

**What to highlight in Langfuse:**
- Open the second trace (the re-run)
- Navigate to metadata
- Confirm: `cache_a_hit: true` (the gateway recognized this is the same content and reused the cached redaction; the field is emitted by `src/corp_llm_gateway/audit/event.py:30`)
- Confirm: `redaction_count` is identical to Prompt 2's value (1)
- (Optional, if exposed) `pre_pass_latency_ms` should be visibly lower than Prompt 2's — the tier engine did not re-run

**Expected tier:** Regex (same tier as Prompt 2, but this time cached).

---

## Prompt 6 — Oversize Skip (M1-11 Threshold)

**Text:**
```
Paste ≥101 KB of repetitive log lines, with one email embedded:

[2026-05-28 10:00:01] INFO: Processing batch
[2026-05-28 10:00:02] INFO: Processing batch
[2026-05-28 10:00:03] INFO: Processing batch
... (repeat to exceed 100 KB total; threshold is 100 * 1024 bytes per
     src/corp_llm_gateway/payload/size_threshold.py:DEFAULT_THRESHOLD_BYTES)
[2026-05-28 10:04:59] ERROR: Alert from j.doe@corp.lan regarding incident
... (more lines)

Summarize this log.
```

(One easy way: `yes "[2026-05-28 10:00:01] INFO: Processing batch" | head -2000` produces ~108 KB. Insert the email line somewhere in the middle.)

**What to show:**
- **The skip, made explicit** — run the Stage 0 `sanitize` helper on an oversize input (≥100 KB). Instead of redactions it prints `redactions: SKIPPED — payload over size threshold; content sent UNREDACTED`. The pre-pass was bypassed per M1-11 (threshold is `100 * 1024` bytes — see `src/corp_llm_gateway/payload/size_threshold.py`), so the email would egress in plain text.
- **Egress still proceeded** — we do not fail-closed on oversize; the policy is "deliver and flag".
- **Audit (Langfuse)** — trace → **Metadata** → `redaction_count: 0` despite the email being present. That's the smoking-gun signal of the skip (no dedicated `payload_skipped` field; the observable is zero redactions on content that visibly contained PII). NOTE: the audit store does **not** carry the payload, so confirm the skip via the CLI above — not by reading the prompt in Langfuse.

**Expected tier:** Skipped — content size exceeded the pre-configured 100 KB threshold; sanitization was bypassed for latency; the trace is flagged for security review (out-of-band, not via a dedicated audit field).

---

## Talking Point — Fail-Closed Audit (planned for GA, not live in this build)

The remaining v1 commitment that *this demo build does not yet exercise live* is the fail-closed posture on the audit pipeline. Walk colleagues through it as a design promise, not a live click-through, because the runtime guard hasn't been wired into `CorpLlmGuardrail` yet.

**The promise (per `docs/plans/20260507-external-sanitizer-gateway-v1.md` M4 fail-policy matrix):**

| Component down | Default behavior | Team-overridable |
|---|---|---|
| Corp LLM (sanitizer) down | **fail-closed, 503 `E_CORP_LLM_DOWN`** | no |
| Redis (mapping store) down | **fail-closed, 503 `E_REDIS_DOWN`** | no |
| Postgres (tokens) down | **fail-closed for new auth; cached auth tolerated 60s** | no |
| Vector buffer full | **fail-closed, 503 by default** | yes — `audit_buffer_full=continue` |
| S3 audit sink down | **fail-closed, 503** (S3 is the durable sink) | no |

**Where it's enumerated in code today:**
- Policy enums: `src/corp_llm_gateway/team_config/models.py` (`AuditSinkDownPolicy`, `AuditBufferFullPolicy`, `PrePassDownPolicy`).
- Health check shape: `src/corp_llm_gateway/healthz/checks.py` (live / ready / sanitization-deep-check).
- Runbook for operators: `docs/ops/runbook.md` (search for "fail-closed").

**What ships before GA:**
- A Vector / sink readiness probe wired into the LiteLLM `pre_call` path.
- 503 short-circuit with a structured error body (`error_code`, `component`, `request_id`) so the developer's Claude Code can surface "the gateway is down for audit, not for you."
- Audit-buffer overflow detection in the Vector sink.

**Why we don't live-demo it today:** the runtime guard would return 503 in `pre_call` based on a sink-health snapshot. None of that code exists yet — searching for `"audit pipeline unhealthy"` in `src/` returns no matches. Demoing it via a doctored response would mislead the room about what's actually built. The design is correct; the wiring is the milestone after this one (M4-3 / M4-5 / M4-6 in the plan).

---

## Cleanup

When you're done with the demo:

```bash
# Preserve state (volumes kept, containers stopped)
scripts/demo.sh down

# OR nuke everything (clean slate for next demo)
scripts/demo.sh reset
```

Both commands preserve the git working tree and do not affect the codebase.
