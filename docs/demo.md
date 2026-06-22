# Demo (laptop walkthrough)

## Audience & Duration

This is a ~15-minute terminal-driven walkthrough for colleagues — security, ops, adjacent teams — who want to see the gateway in action without reading code. The demo runs on your laptop: Claude Code on the left, real Langfuse OSS audit UI in the browser on the right. Traffic flows through the real corp LLM (via the demo LiteLLM proxy), and every request is audited and visible in Langfuse.

## Prerequisites

- Docker 24+ and Docker Compose v2
- `jq` and `curl` (used by `scripts/demo.sh` for healthcheck polling and Langfuse setup; `brew install jq` on macOS, `apt install jq` on Debian/Ubuntu)
- ~3 GB free RAM
- Corp VPN connected (needed to reach the actual corp LLM endpoint)
- Claude Code installed on the laptop

## Setup

1. Clone the repo (or `git pull` if already cloned):
   ```bash
   git clone https://git.corp.lan/<group>/corp-llm-gateway.git
   cd corp-llm-gateway
   ```

2. Copy and configure the demo env file:
   ```bash
   cp .env.demo.example .env.demo
   # Edit CORP_LLM_ENDPOINT to point at the actual corp LLM URL
   # (e.g., https://corp-llm.corp.lan)
   ```

   TLS to the corp GLM is **verified**, not bypassed. The corp LLM presents a
   cert signed by the internal corporate CA, so the demo trusts that chain
   (`crt/corp-ca-bundle.pem`, committed in the repo) rather than disabling
   verification: the gateway's httpx client verifies against `CORP_LLM_CA_BUNDLE`,
   and the LiteLLM container builds a combined certifi-plus-corp-CA bundle and
   points `SSL_CERT_FILE` at it for its `aiohttp` upstream. Both are wired in
   `docker-compose.demo.yml` — no `.env.demo` change is needed. (The old
   `SSL_VERIFY=False` opt-out is gone.)

3. Cold-boot the stack (~3–5 minutes on first run):
   ```bash
   scripts/demo.sh up
   ```
   This pulls images, creates volumes, seeds Langfuse with demo credentials, and prints the URLs.

4. Note the printed URLs: http://localhost:4000 (LiteLLM proxy), http://localhost:3000 (Langfuse).

5. (Optional, useful in a third shell) Tail the redaction flow as it happens:
   ```bash
   scripts/demo.sh logs
   ```
   This follows the LiteLLM container's logs filtered to the sanitize/desanitize
   flow and audit lines, with healthcheck access-log spam suppressed — a
   terminal-side view of the same activity you'll watch land in Langfuse.

## Two-Shell Layout

**Left shell:** Run Claude Code here. This is where you send prompts.

**Right shell/browser:** Open a browser tab at http://localhost:3000. Log in with credentials printed by the `up` command (or look in `.env.demo` for `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`).

**To point Claude Code at the demo proxy**, run this in the left shell:
```bash
scripts/demo.sh presenter-env
```
Copy-paste the exported variables (`ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`) into your Claude Code session. Or manually:
```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_CUSTOM_HEADERS='X-Corp-Auth: demo-team-token'
```

## The 7 Prompts

Each prompt exercises a different part of the gateway. Full prompt text and setup details are in [`scripts/demo-prompts.md`](../scripts/demo-prompts.md).

| # | Prompt | What it demonstrates | Tier / Observable |
|---|--------|----------------------|-------------------|
| 1 | "What's the capital of France?" | Baseline—no PII, no sanitization | None; `redactions=0` |
| 2 | "Draft an email to the DRI@gmail.com…" | Email redaction and restoration | Regex; `cache_a_miss` |
| 3 | Validate JSON with an AWS API key | Structured API-key detection | JSON tier |
| 4 | Function call with embedded email | Redaction within tool arguments | FunctionCall tier |
| 5 | Prompt 2 again (exact repeat) | Content-keyed dedup (Cache A) | Regex; `cache_a_hit: true` |
| 6 | Paste ≥101 KB of logs with one email | Oversize skip (M1-11 threshold) | Skipped; `redaction_count: 0` despite PII |
| 7A | Same as Prompt 2, but stop Vector first | Fail-closed audit pipeline | HTTP 503 `audit pipeline unhealthy` |
| 7B | Restart Vector, re-send 7A | Recovery and normal flow | Regex; trace appears |

See [`scripts/demo-prompts.md`](../scripts/demo-prompts.md) for the exact text, expected Langfuse observations, and how to interpret each trace.

## Teardown

```bash
# Stop containers but preserve state (volumes stay, fast re-run next time)
scripts/demo.sh down

# OR completely reset (nuke volumes for a clean slate)
scripts/demo.sh reset
```

Both commands are safe; they do not touch the git working tree or source code.

## Troubleshooting

- **Corp LLM unreachable** — Check VPN connectivity. The `scripts/demo.sh up` command warns but does not fail; the stack will be healthy even if corp LLM is down. Actual prompt sends will fail with a gateway error. Verify: `curl -v https://<your-corp-llm-endpoint>/health`.

- **Langfuse traces not appearing** — The seed-langfuse step may have failed (rare, usually network timeouts). Run `scripts/demo.sh seed-langfuse` manually to retry idempotently. Then restart the vector service: `docker compose -f docker-compose.demo.yml restart vector`.

- **HTTP 503 with body "audit pipeline unhealthy"** — This is **intentional** in Prompt #7 (demonstrates fail-closed posture when Vector stops). If it appears unexpectedly, check Vector's status: `docker compose -f docker-compose.demo.yml ps vector` and logs: `docker compose -f docker-compose.demo.yml logs vector`.

- **LiteLLM container fails to start** — Usually a network issue during `pip install -e /pkg`. Check logs: `docker compose -f docker-compose.demo.yml logs litellm`. Retry `scripts/demo.sh up`.

- **First boot takes >5 minutes** — Expected on first run; ClickHouse and Langfuse images are large. Subsequent boots (~30s) are warm.

- **"Claude Code printed my real email / API key — did it leak?"** — No. Claude Code's answer shows the **restored original** value because the `post_call` desanitizer rebuilds it on the way back; the placeholder existed only on the gateway↔corp-LLM hop, and the corp LLM never saw the secret. To *see* the redaction, use the Stage 0 `sanitize` helper (before→after) or the Langfuse `placeholder_list`, not the rendered answer. Per-prompt detail in [`scripts/demo-prompts.md`](../scripts/demo-prompts.md).

- **Extra traces with `redaction_count: 0`** — Claude Code fires small warm-up / probe requests on its own; these land as separate traces and are neither your prompt nor a redaction failure. Identify your prompt's trace by `prompt_token_count` and expected `redaction_count`, not by "the latest trace." See [`scripts/demo-prompts.md`](../scripts/demo-prompts.md).

## Non-Goals

The demo intentionally does **not** cover:

- ❌ Real Anthropic/OpenAI upstream (only corp LLM)
- ❌ mTLS or OIDC corp-LLM auth (provider stays `noop`)
- ❌ CoreDNS sinkhole or NetworkPolicy (k8s-only constructs)
- ❌ S3 audit sink or SIEM audit sink (Langfuse is the only sink in the demo)
- ❌ Multi-team isolation (single team: `demo-team`)
- ❌ Per-team `replace.md` rules (one default rules file)
- ❌ CI `e2e:langfuse` job or the existing `docker-compose.yml` (demo stack is parallel, independent)

These are production concerns; the demo focuses on the core redaction→audit→recovery flow.
