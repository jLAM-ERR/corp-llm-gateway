# `X-Corp-Auth` — how the corp token actually moves

How the corp token gets from Keycloak onto the wire and into the
gateway's auth middleware. This complements
[`harness-integration.md`](harness-integration.md), which is
"which pattern do I use per harness." This doc is the deeper "what is
read, when, and what the harness puts on the wire."

## TL;DR

- **The header is sent on every HTTP request** — that's how HTTP
  custom headers work, the harness attaches it to every call.
- **The value is not re-read from disk per request** in the default
  Claude Code setup. It's read once when the shell starts (Pattern 1)
  or once when the harness starts (Pattern 2). After rotation you
  need a new shell — *or* use the localhost proxy (Pattern 3), which
  *does* re-read the file per request.

## The full lifecycle

```
[Keycloak device flow]   ──issue──►   ~/.corp-llm-gateway/token  (chmod 600, 30-day TTL)
        │                                        │
   install.sh                              read by ONE of:
                                                 │
                       ┌─────────────────────────┼──────────────────────────┐
                       ▼                         ▼                          ▼
            Pattern 1 (env var)        Pattern 2 (config.toml)     Pattern 3 (localhost proxy)
            $(cat ...) at rc init       static value, edited        re-read per request
                       │                         │                          │
                       ▼                         ▼                          ▼
         export ANTHROPIC_CUSTOM_HEADERS    [default.headers]    proxy injects header
         "X-Corp-Auth: <token>"             X-Corp-Auth = "..."  on each forwarded request
                       │                         │                          │
                       └─────────────────────────┼──────────────────────────┘
                                                 ▼
                       Harness HTTP client adds X-Corp-Auth to every request
                                                 │
                                                 ▼
                                gateway.corp.lan / LiteLLM proxy
                                                 │
                                                 ▼
                       AuthMiddleware.authenticate_headers(headers)
                          → ctx{user_id, team_id}                  (uses token)
                       AuthMiddleware.strip_corp_token(headers)
                          → header removed before egress           (never logged, never forwarded)
```

## What's stored where

| Artifact | Path | Mode | Set by |
|---|---|---|---|
| Corp token (30-day) | `~/.corp-llm-gateway/token` | `0600` | `install.sh` (Keycloak device flow → exchange at `/internal/issue-token`) |
| Token-file pointer | `$CORP_GATEWAY_TOKEN_FILE` | env | `install.sh` writes it into your rc file |
| Header binding (Pattern 1) | `$ANTHROPIC_CUSTOM_HEADERS` | env | `install.sh` rc block, computed via `$(cat …)` at shell init |

`install.sh` rewrites the rc block between `# >>> corp-llm-gateway >>>`
markers idempotently — re-running it rotates the token *and* the rc
block.

## Per-pattern freshness

| Pattern | When token file is read | When header is sent | Effect of rotation |
|---|---|---|---|
| **1 — env var** (Claude Code) | once, at shell init (`$(cat …)` snapshots) | every request (HTTP default header) | open a new shell after rotation; old shells keep using the old value until restarted |
| **2 — `config.toml`** (Codex) | once, at harness start (TOML parse) | every request | restart harness; for unattended rotation re-run `install.sh` to rewrite the file |
| **3 — localhost proxy** | every request (`_read_token` is called inside the request handler — see `cli/proxy.py:71-81`) | every request | takes effect on the very next request, no restart |

The relevant proxy code:

```python
# src/corp_llm_gateway/cli/proxy.py
def _handle(self) -> None:
    try:
        corp_token = _read_token(self.token_file)   # ← per request
    except FileNotFoundError:
        self._send_error(401, "corp token file not found; run install.sh")
        return
    ...
    headers["X-Corp-Auth"] = corp_token             # ← injected fresh
```

## What Claude Code actually does on the wire

Claude Code does **not** re-evaluate `$ANTHROPIC_CUSTOM_HEADERS` per
message. The harness reads it once during HTTP-client initialization
and registers the parsed `Header: Value` lines as default headers.
Every subsequent `/v1/messages` call carries them as part of the
request — including reconnects within a streaming session.

This is why Pattern 1 needs a fresh shell after token rotation: a
running shell-and-harness pair is holding the old snapshot in
process memory, even though the token file on disk has changed.

## What the gateway does with the header

```python
# pre_call (litellm_hook.py)
ctx = await self._auth.authenticate_headers(_extract_headers(data))
data["headers"] = self._auth.strip_corp_token(_extract_headers(data))
```

1. `authenticate_headers` validates the token, resolves user_id and
   team_id, and raises a typed exception on failure
   (`MissingTokenError`, `ExpiredTokenError`, `RevokedTokenError`,
   `InvalidTokenError`). Each maps to a stable `error_code` recorded
   in audit (`E_MISSING_TOKEN`, `E_TOKEN_EXPIRED`, `E_TOKEN_REVOKED`,
   `E_TOKEN_INVALID`, `E_AUTH`).
2. `strip_corp_token` removes the header from the dict that will be
   forwarded upstream. The token never reaches Anthropic / OpenAI and
   never appears in the audit pipeline (invariant #4).

The developer's `Authorization: Bearer <byok-key>` is *not* touched —
it passes through to upstream untouched (invariant #3).

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `401 E_MISSING_TOKEN` | shell wasn't restarted, or `ANTHROPIC_CUSTOM_HEADERS` wasn't set | open a new shell, or `source ~/.zshrc`; verify with `echo $ANTHROPIC_CUSTOM_HEADERS` |
| `401 E_TOKEN_EXPIRED` | token older than 30 days, shell holding old value | re-run `install.sh` (rotates), then open a new shell |
| `401 E_TOKEN_REVOKED` | admin revoked the token (≤60 s propagation) | re-run `install.sh` to re-auth via Keycloak |
| token file missing | installer never ran, or file deleted | `install.sh` |

For Pattern 3 users, none of the "open a new shell" advice applies —
the proxy re-reads on every request, so rotation lands on the next
call.

## Operational guarantees

- The token file is `0600` (`install.sh` sets it explicitly).
- The token never leaves the laptop (Pattern 1, 2) or the localhost
  proxy (Pattern 3) in any form other than the `X-Corp-Auth` header.
- The gateway never logs `X-Corp-Auth` — pinned by
  `tests/invariants/test_no_originals_leak.py`.
- The proxy in Pattern 3 ignores `HTTP_PROXY` and friends
  (`ProxyHandler({})` in `cli/proxy.py:32`) so the token cannot be
  rerouted through a system-level proxy.

## Quick-look: is my header reaching the gateway?

```bash
echo "$ANTHROPIC_CUSTOM_HEADERS"
# → X-Corp-Auth: ct_xxxxxxxxxxxxxxxxxxxxxx

cat "$CORP_GATEWAY_TOKEN_FILE"
# → ct_xxxxxxxxxxxxxxxxxxxxxx   (must match)

corp-llm-gateway status
# → Validates the token round-trip against the gateway's /healthz/auth.
```

If `echo $ANTHROPIC_CUSTOM_HEADERS` is empty after rotating the token,
that's the "stale shell" problem — a new shell will fix it.
