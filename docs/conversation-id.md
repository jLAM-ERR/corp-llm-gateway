# Conversation identity (`conversation_id`)

How the gateway scopes per-conversation state today, and what it would
take to scope it across a real multi-turn session.

## TL;DR

- `conversation_id` is **not** read from any incoming header or body
  field today.
- It is fabricated inside the gateway, fresh per HTTP request:
  `conversation_id == request_id == uuid4()`.
- This means Cache A (content-keyed dedup) works, but Cache B
  (per-conversation mapping store) is written-and-never-reused across
  requests. Multi-turn restoration of the *same* original→placeholder
  binding across sibling requests does not happen.

## Where it's set

`src/corp_llm_gateway/litellm_hook.py`:

```python
@staticmethod
def _ensure_request_id(data: dict[str, Any]) -> str:
    rid = data.get("_corp_gateway_request_id")
    if isinstance(rid, str) and rid:
        return rid
    rid = str(uuid.uuid4())
    data["_corp_gateway_request_id"] = rid
    return rid
```

```python
result = await self._orch.sanitize(
    content,
    team_id=ctx.team_id,
    conversation_id=request_id,   # same UUID, just renamed
)
```

`_corp_gateway_request_id` is an internal scratch key set by the hook
itself; it lets pre/post on the *same* HTTP request agree on the ID.
Nothing reads it from `headers`, `metadata`, `proxy_server_request`, or
LiteLLM's `litellm_session_id`.

## What's keyed by what

`src/corp_llm_gateway/storage/mapping.py` defines two caches:

| Cache | Key | Purpose | Status today |
|---|---|---|---|
| **A — dedup** | `sha256(team_id + rules + text)` | reuse a mapping when the same text recurs across requests | ✅ working — content-derived, not conversation-derived |
| **B — per-conv** | `(conversation_id, original) ↔ placeholder` | keep `[EMAIL_001]` stable for the same original across all turns of one conversation | ⚠️ inert — every request gets a fresh `conversation_id`, so writes are never read by sibling requests |

Post-call desanitization currently relies on the in-process
`_RequestState.mapping`, not on Cache B, so the inertness is invisible
*within* a single request. It only matters across requests in one
session.

## Concrete consequence

```
Turn 1:  "email me at alice@corp.example"
         → sanitized: "email me at [EMAIL_001]"
         → Cache B[(uuid-A, "alice@corp.example")] = "[EMAIL_001]"

Turn 2:  "send the recap to alice@corp.example"     ← same string, new HTTP request
         → conversation_id = uuid-B  (fresh)
         → Cache B miss for (uuid-B, "alice@corp.example")
         → goes to Cache A, hits on content-hash
         → still gets "[EMAIL_001]" because the text hash is stable
         → so today, *placeholder stability* survives via Cache A even though Cache B is inert
```

Where this falls apart: when text *paraphrases* the same entity
("Alice's email" vs. "alice@corp.example") between turns. Cache A keys
on raw bytes, so it won't dedup. Cache B *would* — if there were a
shared `conversation_id` to find both originals under.

## How to wire a real conversation ID

Three plausible sources, none currently used:

1. **Anthropic-style request metadata.** Claude Code can populate
   `metadata.user_id` (or a sibling field) per session. Pre_call would
   read `data["metadata"]["user_id"]` (or whatever the harness agrees
   to send) and prefer it over the UUID fallback.
2. **A header from the harness installer.** `install.sh` could mint a
   stable per-session ID and have the harness send `X-Conversation-Id`.
   The localhost proxy in `docs/harness-integration.md` (Pattern 3) is
   the natural place to inject it.
3. **LiteLLM session ID.** LiteLLM exposes `litellm_session_id` in
   `kwargs` for proxy callbacks. Pre_call could read it from
   `data.get("litellm_session_id")` if the proxy is configured to pass
   it through.

Whichever lane is picked, the change is local to
`_ensure_request_id` (or, more cleanly, a sibling
`_resolve_conversation_id` that falls back to the request UUID when
nothing upstream supplies one). `request_id` and `conversation_id`
should become **separate fields** at that point — one identifies an
HTTP call, the other a session.

## Privacy considerations when this is wired

- `conversation_id` becomes a join key across audit records. It must
  not embed PII (no raw user email, no IP). A random per-session UUID
  minted by the harness is the safe shape.
- It is allowed in audit (it's a join key, not a NEVER field), but
  document it explicitly when added so SIEM dashboards can pivot on it.
- Cache B's TTL is sliding (`cache_b_ttl_seconds`, default 1 h). A
  long-lived session that goes idle will start fresh on resume — by
  design.

## What changes when this lands

- Cache B starts paying off → fewer corp-LLM calls on long sessions
  with stable entities.
- Mappings persist across the natural conversational paraphrase, not
  only across literal duplicates.
- No change to the M1-14 invariants. `conversation_id` is not a NEVER
  field; originals/placeholders/credentials still are.

## Status

Not on the v1 critical path. Cache A is sufficient for the launch
success criterion. Track as a v1.1 follow-up if early production data
shows multi-turn paraphrase as a meaningful miss source.
