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

**What to highlight in Langfuse:**
- Open the trace
- Find the **"Request"** section or upstream payload view (e.g., the JSON body sent to the gateway)
- Confirm: the email address is replaced with a placeholder like `[EMAIL_001]` (the corp LLM is instructed to emit `[LABEL_NNN]` shape — see `src/corp_llm_gateway/sanitizer/orchestrator.py:_build_system_prompt`)
- Switch to the **"Response"** or **"Rendered response"** section in Claude Code
- Confirm: the original email `the DRI@gmail.com` is restored in the model's output (our post-call desanitizer worked)

**Expected tier:** Regex — the email pattern matched the regex tier's PII detector.

---

## Prompt 3 — JSON Tier

**Text:**
```
Validate this JSON config and explain any issues:

{"endpoint": "https://api.internal", "api_key": "sk_live_AKIAIOSFODNN7EXAMPLE"}
```

**What to highlight in Langfuse:**
- Open the trace
- Find the request payload
- Confirm: the `api_key` field's value is redacted (e.g., `"api_key": "[TOKEN_001]"`), but the **field name** `api_key` is preserved (JSON tier does not redact keys)
- The endpoint URL may also be redacted depending on your regex rules

**Expected tier:** JSON — the detector recognized a structured AWS/cloud API key pattern.

---

## Prompt 4 — FunctionCall Tier

**Text:**
```
Use the search_kb tool with query='customer email j.doe@corp.lan asked about X'
```

**What to highlight in Langfuse:**
- Open the trace
- Find the **function call arguments** section (the JSON args to the tool)
- Confirm: the email in `query=` is redacted to `[EMAIL_002]` (or similar — same `[LABEL_NNN]` shape as Prompt 2)
- After the model executes the tool, check the **tool result** in the response
- Confirm: the desanitizer rebuilt the original email in the rendered response shown in Claude Code

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

**What to highlight in Langfuse:**
- Open the trace; the **upstream payload still contains `j.doe@corp.lan` in plain text** — pre-pass was skipped per the M1-11 size threshold, so the redaction tiers never ran.
- The egress still proceeded (we do not fail-closed on oversize; the policy is "deliver and flag")
- `redaction_count: 0` despite the email being present — this is the smoking-gun signal of the skip, not a dedicated `skipped` field. (`AuditEvent` does not currently expose a `payload_skipped` boolean — the observable is the absence of redactions on content that visibly contained PII.)

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
