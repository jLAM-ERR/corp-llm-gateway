# Harness integration

How developer-laptop AI coding harnesses (Claude Code, Codex, Cursor, etc.)
send `X-Corp-Auth` and other corp-specific headers to the gateway.

## The problem

The gateway expects two headers on every request:

| Header | Source | Purpose |
|---|---|---|
| `X-Corp-Auth: <corp-token>` | `~/.corp-llm-gateway/token` | corp identity / team resolution |
| `Authorization: Bearer <byok-key>` | dev's Anthropic / OpenAI key | BYOK passthrough to upstream |

Every harness already knows how to send `Authorization` — that's the
standard API key. The trick is `X-Corp-Auth`. Three patterns work, in
order of friction:

## Pattern 1 — env-var custom headers (Claude Code only)

Claude Code natively supports `ANTHROPIC_CUSTOM_HEADERS`:

```bash
export ANTHROPIC_BASE_URL='https://gateway.corp.lan'
export ANTHROPIC_CUSTOM_HEADERS="X-Corp-Auth: $(cat ~/.corp-llm-gateway/token)"
```

Caveat: the value snapshots at shell init. After token rotation
(installer rotates every 30 days at minimum), open a new shell so the
`$(cat …)` re-runs. `install.sh` writes a function that re-evaluates
on each shell start, which covers the common case.

## Pattern 2 — config-file headers (Codex)

The OpenAI Codex CLI reads `~/.codex/config.toml`:

```toml
[default]
api_base = "https://gateway.corp.lan/v1"

[default.headers]
X-Corp-Auth = "ct_xxxxxxxxxxxxxxxxxxxxxx"
```

The static value is the limitation. Re-run `install.sh` after token
rotation, or set up a cron / launchd job that rewrites this file from
the token file weekly.

For tools using the OpenAI Python SDK directly:

```python
from openai import OpenAI
client = OpenAI(
    base_url="https://gateway.corp.lan/v1",
    default_headers={"X-Corp-Auth": open("~/.corp-llm-gateway/token").read().strip()},
)
```

## Pattern 3 — `corp-llm-gateway proxy` (universal)

For harnesses that don't expose a custom-header mechanism, run a
localhost HTTP proxy that injects `X-Corp-Auth` on every request:

```bash
corp-llm-gateway proxy --listen 127.0.0.1:9999 --upstream https://gateway.corp.lan
```

Then point the harness at the proxy:

```bash
export ANTHROPIC_BASE_URL='http://127.0.0.1:9999'
export OPENAI_BASE_URL='http://127.0.0.1:9999/v1'
```

The proxy:

- Re-reads the token file on every request (no shell-restart on rotation).
- Streams SSE responses through unmodified.
- Forwards the developer's `Authorization: Bearer <byok-key>` untouched.
- Logs nothing about request bodies — the gateway is the audit boundary,
  not the proxy.

Recommended: add the proxy to your shell startup as a launchd / systemd
user-service so it's always running. Example launchd plist in
`scripts/launchd-proxy.plist` (TODO: check in if requested).

## Pattern matrix

| Harness | Recommended | Fallback |
|---|---|---|
| Claude Code | Pattern 1 | Pattern 3 |
| Codex CLI (OpenAI) | Pattern 2 | Pattern 3 |
| Cursor (IDE) | App settings UI (custom-header field) | Pattern 3 |
| Continue (VS Code) | Config UI | Pattern 3 |
| `curl`, raw scripts | env var + `--header` | Pattern 3 |

## What `install.sh` does today

```bash
ANTHROPIC_BASE_URL='https://gateway.corp.lan'
OPENAI_BASE_URL='https://gateway.corp.lan/v1'
CORP_GATEWAY_TOKEN_FILE='~/.corp-llm-gateway/token'
# Pattern 1: only useful for Claude Code; safe no-op for other harnesses
ANTHROPIC_CUSTOM_HEADERS="X-Corp-Auth: $(cat ~/.corp-llm-gateway/token)"
```

For Codex / Cursor / others, dev edits the relevant config file once
(token rotates only every 30 days; the friction is bounded).

## What still leaks vs. what doesn't

- `Authorization: Bearer <byok-key>` is the developer's personal
  Anthropic / OpenAI key. The gateway forwards it untouched to upstream
  (BYOK). It is **never logged** by the gateway and the proxy doesn't
  touch it either.
- `X-Corp-Auth` is a corp token, scoped per user, expires in 30 days,
  revocable in ≤60 s. The gateway strips it before forwarding upstream
  (`AuthMiddleware.strip_corp_token`) and never logs it.
- Both invariants are pinned by `tests/invariants/test_no_originals_leak.py`.
