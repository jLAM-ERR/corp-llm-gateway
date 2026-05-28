# Demo: 7-Prompt Walkthrough

A curated set of prompts that exercise each sanitizer tier, Cache A, the oversize skip (M1-11), and the fail-closed audit pipeline.

## Stage 0 — Presenter Setup

**Two-shell layout:**
- **Left shell:** your laptop, running Claude Code
- **Right shell/browser:** http://localhost:3000 (Langfuse OSS UI), login with `demo@corp.lan` / `demo`

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
- Confirm: the email address is replaced with a placeholder like `<EMAIL_1>`
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
- Confirm: the `api_key` field's value is redacted (e.g., `"api_key": "<TOKEN_1>"`), but the **field name** `api_key` is preserved (JSON tier does not redact keys)
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
- Confirm: the email in `query=` is redacted to `<EMAIL_2>` (or similar)
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

## Prompt 7 — Fail-Closed Audit Pipeline

**Text (Part A):**
```
Draft a follow-up email to the DRI@gmail.com about the Q3 plan.
```

**Before you send (Part A):**
1. In a **third shell** (or in a separate window), run:
   ```bash
   docker compose -f docker-compose.demo.yml stop vector
   ```
2. Wait a few seconds for Vector to fully stop.

**Send Prompt 7A:**
- In Claude Code, send the prompt
- **Expected:** The gateway returns HTTP **503 Service Unavailable** with a body message containing `audit pipeline unhealthy`

**This is intentional — it demonstrates the fail-closed posture:** when the audit pipeline (Vector → Langfuse) is down, we reject traffic. No data leaves the corp without an audit trail.

**Text (Part B — Recovery):**

1. Restart Vector:
   ```bash
   docker compose -f docker-compose.demo.yml start vector
   ```
2. Wait until `docker compose -f docker-compose.demo.yml ps vector` reports `(healthy)` — don't rely on a fixed sleep; Vector boot can race with its own healthcheck. Typically 5–15s on a warm laptop.

3. **Re-send Prompt 7A** (the same text):
   - **Expected:** The request **succeeds** this time
   - Check Langfuse: a new trace appears for this re-attempt
   - The original email is redacted upstream, restored in the response

**What this demonstrates:**
- Pre-call sanitation still works (Regex tier catches the email).
- Post-call desanitization restores the email in the response.
- The gateway correctly fails closed when audit infra is unhealthy.
- Recovery is automatic once audit infra recovers.

**Expected tier:** Regex (same as Prompt 2).

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
